"""引用申请审阅、声明核发与更正/撤回。

权利规则（任务要求）：
- 独占认领（exclusive）：同一规范设备的服务区间互斥，已独占区间不能再发给别人；
- 共同披露（joint）：允许多方引用同一服务，但项目总计只计一次，绝不重复累加；
- 独占与共同在重叠区间也互斥；先获确认的申请优先（按提交时间、身份排序），
  后到申请只能被驳回或裁剪到不重叠区间；
- 申请对象是服务期间与用途，不拥有设备全部生命周期效益；
- 不同候选情景的最大收益不能拼在一张声明里，声明锁定单一情景与计算快照；
- 已核发声明原件不可变；退回、因子修订、身份合并通过更正/撤回记录关联，
  尚未受影响的范围继续有效。
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from .contract import ClaimMode
from .timeutil import parse_date, parse_ts

DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"
DECISION_PARTIAL = "partially_approved"

CORRECTION = "correction"
WITHDRAWAL = "withdrawal"


def _overlap(a_from: dt.date, a_to: dt.date, b_from: dt.date, b_to: dt.date) -> bool:
    return a_from < b_to and b_from < a_to  # service_to 为不含终点


def _intersection(
    a_from: dt.date, a_to: dt.date, b_from: dt.date, b_to: dt.date
) -> tuple[dt.date, dt.date] | None:
    lo = max(a_from, b_from)
    hi = min(a_to, b_to)
    if lo < hi:
        return lo, hi
    return None


def _subtract_span(
    span: tuple[dt.date, dt.date],
    blocked: tuple[dt.date, dt.date],
) -> list[tuple[dt.date, dt.date]]:
    """从 span 中扣除 blocked 区间，返回剩余零或多个不相交区间。"""
    lo, hi = span
    b_lo, b_hi = blocked
    if not _overlap(lo, hi, b_lo, b_hi):
        return [span]
    pieces: list[tuple[dt.date, dt.date]] = []
    if lo < b_lo:
        pieces.append((lo, b_lo))
    if b_hi < hi:
        pieces.append((b_hi, hi))
    return pieces


def _subtract_many(
    spans: list[tuple[dt.date, dt.date]],
    blocked_list: list[tuple[dt.date, dt.date]],
) -> list[tuple[dt.date, dt.date]]:
    remaining = list(spans)
    for blocked in blocked_list:
        nxt: list[tuple[dt.date, dt.date]] = []
        for span in remaining:
            nxt.extend(_subtract_span(span, blocked))
        remaining = nxt
    return remaining


@dataclass
class ReviewContext:
    """审阅所需的只读上下文。"""

    engine: object
    analysis: dict
    as_of: dt.datetime


def _device_point(ctx: ReviewContext, canonical_id: str) -> dict:
    for d in ctx.analysis["devices"]:
        if d["device_id"] == canonical_id:
            return d
    raise KeyError(f"分析结果中缺少设备 {canonical_id}")


def _blocked_spans_from_claims(store, canonical_id: str) -> list[tuple[dt.date, dt.date, str]]:
    """仍有效声明中，会阻止后来者的区间（独占声明全阻；共同声明阻独占申请）。

    返回 (from, to, blocking_mode)。共同-共同共存由调用方另行判断。
    """
    withdrawn = {c["claim_id"] for c in store.read("corrections") if c["kind"] == WITHDRAWAL}
    blocked: list[tuple[dt.date, dt.date, str]] = []
    for claim in store.read("claims"):
        if claim["id"] in withdrawn or claim["canonical_device_id"] != canonical_id:
            continue
        modes = {p["mode"] for p in claim["parties"]}
        blocking_mode = "exclusive" if ClaimMode.EXCLUSIVE.value in modes else "joint"
        for span in claim["approved_spans"]:
            blocked.append((parse_date(span[0]), parse_date(span[1]), blocking_mode))
    return blocked


def review_all(
    store,
    applications: list[dict],
    ctx: ReviewContext,
    canonical_map: dict[str, str],
) -> list[dict]:
    """按提交顺序审阅一批申请；同批内先决定者占用区间，后到者被裁剪或驳回。"""
    # 本轮已核准区间（与账本声明共同构成权利占用）
    # canonical -> list[(from,to,mode)]
    granted: dict[str, list[tuple[dt.date, dt.date, str]]] = {}
    reviews: list[dict] = []

    ordered = sorted(applications, key=lambda a: (a["known_at"], a["id"]))
    decided = {d["application_id"] for d in store.read("applications")}

    for application in ordered:
        if application["id"] in decided:
            continue
        if parse_ts(application["known_at"]) > ctx.as_of:
            # as-of 之后才提交的申请本轮不可见，留待后续时点审阅
            continue
        reviews.append(
            _review_one(store, application, ctx, canonical_map, granted)
        )
    return reviews


def _review_one(
    store,
    application: dict,
    ctx: ReviewContext,
    canonical_map: dict[str, str],
    granted: dict[str, list[tuple[dt.date, dt.date, str]]],
) -> dict:
    reasons: list[str] = []
    app_id = application["id"]
    device_id = application["device"]
    canonical_id = canonical_map[device_id]
    req_from = parse_date(application["service_from"])
    req_to = parse_date(application["service_to"])
    mode = ClaimMode(application["mode"])

    if parse_ts(application["known_at"]) > ctx.as_of:
        reasons.append("申请在审阅时点之后才提交，当前不可见")

    point = _device_point(ctx, canonical_id)

    # 1) 申请区间与设备实际服务片段求交；退回/拆解之后不存在可声明服务。
    # 已关闭片段只有产生过有记录支持的观测，才存在可声明的已实现服务；
    # 无观测的退回/拆解片段不产生任何环境效益，不得核发。
    service_spans: list[tuple[dt.date, dt.date]] = []
    for ep in point["episodes"]:
        ep_from = parse_date(ep["opened_on"])
        ep_to = parse_date(ep["closed_on"]) if ep["closed_on"] else req_to
        if ep["close_reason"] in ("returned", "dismantled") and ep.get(
            "observed_years", 0.0
        ) <= 0.0:
            continue
        inter = _intersection(req_from, req_to, ep_from, ep_to)
        if inter:
            service_spans.append(inter)
    if not service_spans:
        reasons.append(
            "申请区间内没有有证据支持的服务片段（退回/拆解且无在用观测，或尚未售出）"
        )

    # 2) 因子适用范围：有效期之外的部分不可声明（裁剪并记录）
    coverage = ctx.engine.registry.coverage_window(point["kind"], point["region"])
    factor_blocked: list[tuple[dt.date, dt.date]] = []
    if coverage and service_spans:
        cov_from, cov_to = coverage
        for span in list(service_spans):
            outside = _subtract_span(span, (max(span[0], cov_from), min(span[1], cov_to)))
            factor_blocked.extend(outside)
        if factor_blocked:
            reasons.append(
                f"因子适用范围为 {cov_from}~{cov_to}，超出部分预计收益不可声明"
            )

    # 3) 反事实单元支持程度
    if point["cell_support"] in ("contradicted", "insufficient"):
        reasons.append(
            f"反事实单元支持程度={point['cell_support']}，预计收益不可声明"
        )

    # 4) 权利冲突：已核发声明 + 本批次先核准区间
    blockers = [
        (a, b, m)
        for (a, b, m) in _blocked_spans_from_claims(store, canonical_id)
        + granted.get(canonical_id, [])
    ]
    rights_blocked: list[tuple[dt.date, dt.date]] = []
    for b_from, b_to, b_mode in blockers:
        # 共同披露之间允许共存；其余组合在重叠区间互斥
        if mode == ClaimMode.JOINT and b_mode == ClaimMode.JOINT.value:
            continue
        if _overlap(req_from, req_to, b_from, b_to):
            inter = _intersection(req_from, req_to, b_from, b_to)
            rights_blocked.append(inter)
            reasons.append(
                f"区间 {inter[0]}~{inter[1]} 与在先 {b_mode} 权利互斥"
            )

    # 5) 未来日期上的服务只能是 projected（提示性限制，不裁剪区间）
    if req_to > ctx.as_of.date() and point["active"]:
        reasons.append(
            f"{ctx.as_of.date()} 之后的服务属于未来情景，只能计为 projected"
        )

    # ---- 区间裁剪：先扣因子越界，再扣权利冲突 --------------------------
    claimable = _subtract_many(service_spans, factor_blocked + rights_blocked)
    limited = sorted(set(factor_blocked))

    if not claimable:
        decision = DECISION_DENIED
    elif len(claimable) != len(service_spans) or limited:
        decision = DECISION_PARTIAL
    elif point["cell_support"] == "weak":
        # 区间完整但证据弱：核准并在声明上标注弱支持，不裁剪区间
        decision = DECISION_APPROVED
        reasons.append("反事实单元仅弱支持，声明须保留该限制说明")
    else:
        decision = DECISION_APPROVED

    review = {
        "id": "RV" + uuid.uuid4().hex[:10],
        "application_id": app_id,
        "device_id": device_id,
        "canonical_device_id": canonical_id,
        "applicant": application["applicant"],
        "mode": mode.value,
        "requested_from": application["service_from"],
        "requested_to": application["service_to"],
        "decision": decision,
        "claimable_spans": [[a.isoformat(), b.isoformat()] for a, b in claimable],
        "limited_spans": [[a.isoformat(), b.isoformat()] for a, b in limited],
        "reasons": reasons,
        "reviewed_at": ctx.as_of.isoformat(),
        "analysis_id": ctx.analysis["analysis_id"],
    }
    if decision != DECISION_DENIED:
        for span in claimable:
            granted.setdefault(canonical_id, []).append(
                (span[0], span[1], mode.value)
            )
    return review


def issue_claim(store, review: dict, application: dict, ctx: ReviewContext) -> dict:
    """依据通过/部分通过的审阅决定核发声明，写入完整计算依据快照。"""
    if review["decision"] == DECISION_DENIED:
        raise ValueError(f"申请 {application['id']} 已被驳回，不能核发")
    canonical_id = review["canonical_device_id"]
    point = _device_point(ctx, canonical_id)
    mc = ctx.analysis["devices_mc"][canonical_id]

    claim_id = "C" + uuid.uuid4().hex[:10]
    claim = {
        "id": claim_id,
        "application_id": application["id"],
        "canonical_device_id": canonical_id,
        "device_id_at_review": application["device"],
        "parties": [
            {
                "applicant": application["applicant"],
                "mode": application["mode"],
                "share_note": (
                    "独占认领：区间内唯一引用方"
                    if application["mode"] == ClaimMode.EXCLUSIVE.value
                    else "共同披露：可多方引用，项目总计只计一次"
                ),
            }
        ],
        "service_from": review["requested_from"],
        "service_to": review["requested_to"],
        "approved_spans": review["claimable_spans"],
        "limited_spans": review["limited_spans"],
        "scenario_id": ctx.analysis["scenario"]["id"],
        "scenario_label": ctx.analysis["scenario"]["label"],
        "analysis_id": ctx.analysis["analysis_id"],
        "as_of": ctx.as_of.isoformat(),
        "basis_snapshot": {
            "cell_support": point["cell_support"],
            "substitution_rate": point["service"]["substitution_rate"],
            "expected_life_years": point["service"]["expected_life_years"],
            "observed_service_years": point["service"]["observed_service_years"],
            "actual_burden_kgco2e": point["actual_burden_kgco2e"],
            "avoided_kgco2e": point["avoided_kgco2e"],
            "net_kgco2e": point["net_kgco2e"],
            "mc": mc,
            "episodes": point["episodes"],
            "limitations": point["limitations"],
            "review_reasons": review["reasons"],
        },
        "status": "active",
        "issued_at": ctx.as_of.isoformat(),
        "superseded_by": None,
    }
    store.append("claims", [claim])
    store.append(
        "applications",
        [
            {
                "id": "D" + uuid.uuid4().hex[:10],
                "application_id": application["id"],
                "claim_id": claim_id,
                "review": review["id"],
                "decision": review["decision"],
                "decided_at": ctx.as_of.isoformat(),
            }
        ],
    )
    return claim


def record_denial(store, review: dict, ctx: ReviewContext) -> dict:
    record = {
        "id": "D" + uuid.uuid4().hex[:10],
        "application_id": review["application_id"],
        "claim_id": None,
        "review": review["id"],
        "decision": review["decision"],
        "reasons": review["reasons"],
        "decided_at": ctx.as_of.isoformat(),
    }
    store.append("applications", [record])
    return record


def active_claims(store) -> list[dict]:
    withdrawn = {c["claim_id"] for c in store.read("corrections") if c["kind"] == WITHDRAWAL}
    return [c for c in store.read("claims") if c["id"] not in withdrawn]


def recheck_after_event(store, event: dict, ctx: ReviewContext, canonical_map: dict[str, str]) -> list[dict]:
    """事件驱动复核：退回、因子修订或身份合并后重算受影响声明。

    原件保留；按受影响区间生成 correction（部分更正）或 withdrawal（全部失效）。
    """
    affected_ids: set[str] = set()
    new_canonical = canonical_map.get(event["device"], event["device"])
    for claim in active_claims(store):
        if claim["canonical_device_id"] in (event["device"], new_canonical):
            affected_ids.add(claim["id"])

    results: list[dict] = []
    for claim_id in sorted(affected_ids):
        claim = store.find("claims", claim_id)
        point = _device_point(ctx, claim["canonical_device_id"])
        still_valid_spans = []
        for span in claim["approved_spans"]:
            span_from, span_to = parse_date(span[0]), parse_date(span[1])
            for ep in point["episodes"]:
                ep_from = parse_date(ep["opened_on"])
                if ep["close_reason"] in ("returned", "dismantled"):
                    # 已关闭片段：只有产生了有记录支持的观测，服务才真实存在；
                    # 无观测的退回/拆解片段没有可声明的已实现服务
                    if ep.get("observed_years", 0.0) <= 0.0:
                        continue
                    ep_to = parse_date(ep["closed_on"])
                else:
                    ep_to = span_to
                inter = _intersection(span_from, span_to, ep_from, ep_to)
                if inter:
                    still_valid_spans.append(inter)
        kind = CORRECTION if still_valid_spans else WITHDRAWAL
        # 因子版本变化检测：比较声明快照与当前分析视图中的因子 id
        # （含已实现期与预计期因子；预计期因子反映修订后的当前依据）
        def _factor_ids(episodes, projected: bool):
            key = "projected_factor_id" if projected else "factor_id"
            return sorted({ep.get(key) for ep in episodes if ep.get(key)})

        original_realized = _factor_ids(
            claim["basis_snapshot"].get("episodes", []), False
        )
        original_projected = _factor_ids(
            claim["basis_snapshot"].get("episodes", []), True
        )
        current_realized = _factor_ids(point["episodes"], False)
        current_projected = _factor_ids(point["episodes"], True)
        factor_revised = (
            original_realized != current_realized
            or original_projected != current_projected
        )
        recomputed = {
            "cell_support": point["cell_support"],
            "observed_service_years": point["service"]["observed_service_years"],
            "avoided_kgco2e": point["avoided_kgco2e"],
            "net_kgco2e": point["net_kgco2e"],
            "limitations": point["limitations"],
        }
        if factor_revised:
            recomputed["factor_revision"] = {
                "realized_factor_ids_at_issuance": original_realized,
                "projected_factor_ids_at_issuance": original_projected,
                "realized_factor_ids_at_recheck": current_realized,
                "projected_factor_ids_at_recheck": current_projected,
                "note": "因子已修订，更正后的数字按当前适用因子重算；"
                        "原件快照保留核发当时依据，不被覆盖",
            }
        correction = {
            "id": "X" + uuid.uuid4().hex[:10],
            "claim_id": claim_id,
            "event_id": event["id"],
            "kind": kind,
            "original_status": claim["status"],
            "remaining_spans": [
                [a.isoformat(), b.isoformat()] for a, b in still_valid_spans
            ],
            "recomputed_basis": recomputed,
            "note": (
                "部分区间失效，原件保留并关联本更正；未受影响范围继续有效"
                if kind == CORRECTION
                else "声明覆盖服务已全部失效（如退回后无在用服务），原件撤回保留"
            ),
            "created_at": ctx.as_of.isoformat(),
        }
        store.append("corrections", [correction])
        results.append(correction)
    return results
