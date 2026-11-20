"""命令行入口：本地循环利用效益声明全流程。

子命令：
  import-data        导入资料批次（校验摘要与契约，登记进追加式账本）
  identities         查看规范身份、空序列号与重复序列号提示
  merge-identity     登记人工确认的同一实物对应
  analyze            运行净效益分析（含相关蒙特卡洛与留出检查）
  list-applications  列出申请
  review             审阅单份或全部申请
  issue              对通过审阅的申请核发声明
  recheck-event      事件（退回/因子修订/身份合并）后复核受影响声明
  report             产出 JSON + Markdown 下钻报告
  show-claim         查看声明原件与更正链
  reset              清空 .local 本地状态（不动 data/）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

from . import claims as claims_mod
from . import contract
from .claims import ReviewContext, issue_claim, record_denial, recheck_after_event, review_all
from .identity import IdentityResolver, propose_link
from .pipeline import build_engine, run_analysis
from .reporting import claim_registry, write_reports
from .storage import Store
from .timeutil import parse_ts


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_import(store: Store, args) -> None:
    record = store.import_batch(args.source)
    _print(
        {
            "imported": True,
            "batch_id": record["id"],
            "source_dir": record["source_dir"],
            "tables": {t["name"]: t["rows"] for t in record["manifest"]["tables"]},
        }
    )


def cmd_identities(store: Store, args) -> None:
    tables = store.all_tables_ignoring_asof()
    resolver = IdentityResolver(tables.get("devices", []), store.read("identity_links"))
    _print(
        {
            "canonical_devices": {
                root: {
                    "kind": view["kind"],
                    "region": view["region"],
                    "serial": view["serial"],
                    "merged_members": view["merged_members"],
                }
                for root, view in sorted(resolver.canonical_devices().items())
            },
            "null_serial_kept_independent": resolver.null_serial_unlinked(),
            "duplicated_serials_unlinked": resolver.duplicated_serials(),
            "note": "空序列号与未确认重复序列号一律保持独立，禁止按型号猜测合并",
        }
    )


def cmd_merge_identity(store: Store, args) -> None:
    link = propose_link(store, args.kept, args.merged, args.reason, args.evidence)
    _print({"identity_link_recorded": link})


def cmd_analyze(store: Store, args) -> None:
    as_of = parse_ts(args.as_of) if args.as_of else None
    analysis = run_analysis(
        store,
        scenario_id=args.scenario,
        as_of=as_of,
        n_trials=args.trials,
        seed=args.seed,
    )
    path = store.write_output(f"analysis_{analysis['analysis_id']}.json", analysis)
    _print(
        {
            "analysis_id": analysis["analysis_id"],
            "scenario": analysis["scenario"]["id"],
            "as_of": analysis["as_of"],
            "point_totals_kgco2e": analysis["point_totals_kgco2e"],
            "mc_net_total": analysis["monte_carlo"]["correlated"]["net_total"],
            "correlation_note": analysis["monte_carlo"]["correlation_note"],
            "saved_to": str(path),
        }
    )


def _load_review_context(store, analysis_id: str | None) -> tuple[dict, ReviewContext, IdentityResolver]:
    analyses = store.read("analyses")
    if not analyses:
        raise SystemExit("尚无分析运行，请先执行 analyze")
    if analysis_id:
        record = store.find("analyses", analysis_id)
        if record is None:
            raise SystemExit(f"分析运行不存在：{analysis_id}")
    else:
        # 审阅默认锁定最近一次中枢情景；其他情景仅用于敏感性对照
        central = [a for a in analyses if a["scenario_id"] == "central"]
        record = (central or analyses)[-1]
    as_of = parse_ts(record["as_of"])
    engine, _, _, resolver = build_engine(
        store, as_of, n_trials=record["n_trials"], seed=record["seed"]
    )
    analysis = engine.run(record["scenario_id"])
    analysis["analysis_id"] = record["id"]
    analysis["scenario"] = {"id": record["scenario_id"], **{
        k: v for k, v in analysis["scenario"].items() if k != "id"
    }}
    ctx = ReviewContext(engine=engine, analysis=analysis, as_of=as_of)
    return analysis, ctx, resolver


def cmd_list_applications(store: Store, args) -> None:
    tables = store.all_tables_ignoring_asof()
    decided = {d["application_id"] for d in store.read("applications")}
    rows = []
    for app in tables.get("applications", []):
        rows.append(
            {
                "id": app["id"],
                "applicant": app["applicant"],
                "device": app["device"],
                "span": f"{app['service_from']}~{app['service_to']}",
                "mode": app["mode"],
                "already_decided": app["id"] in decided,
            }
        )
    _print({"applications": rows, "n": len(rows)})


def cmd_review(store: Store, args) -> None:
    analysis, ctx, resolver = _load_review_context(store, args.analysis_id)
    tables = store.all_tables_ignoring_asof()
    canonical_map = {d: resolver.canonical(d) for d in resolver.devices}
    apps = tables.get("applications", [])
    if args.application:
        apps = [a for a in apps if a["id"] == args.application]
        if not apps:
            raise SystemExit(f"申请不存在：{args.application}")
    # 按提交时间/身份排序，同批内先决定者先占区间；已决定申请自动跳过
    reviews = review_all(store, sorted(apps, key=lambda a: (a["known_at"], a["id"])),
                         ctx, canonical_map)
    if args.issue:
        issued, denied = [], []
        app_by_id = {a["id"]: a for a in apps}
        for review in reviews:
            app = app_by_id[review["application_id"]]
            if review["decision"] == claims_mod.DECISION_DENIED:
                record_denial(store, review, ctx)
                denied.append({"application_id": app["id"], "reasons": review["reasons"]})
            else:
                claim = issue_claim(store, review, app, ctx)
                issued.append(
                    {
                        "claim_id": claim["id"],
                        "application_id": app["id"],
                        "decision": review["decision"],
                        "device": claim["canonical_device_id"],
                        "approved_spans": claim["approved_spans"],
                    }
                )
        _print({"issued": issued, "denied": denied,
                "n_issued": len(issued), "n_denied": len(denied)})
    else:
        _print({"reviews": reviews, "n": len(reviews)})


def cmd_register_event(store: Store, args) -> None:
    """登记触发复核的业务事件；退回事件可直接从流转事实核验。"""
    import uuid as _uuid

    tables = store.all_tables_ignoring_asof()
    detail = json.loads(args.detail) if args.detail else {}
    if args.kind == "returned":
        returns = [
            m
            for m in tables.get("movements", [])
            if m["device"] == args.device and m["kind"] == "returned"
        ]
        if not returns:
            raise SystemExit(f"设备 {args.device} 没有退回流转事实，不能登记退回事件")
        latest = sorted(returns, key=lambda m: (m["day"], m["id"]))[-1]
        detail.setdefault("movement_id", latest["id"])
        occurred_at = latest["known_at"]
    elif args.kind == "factor_revision":
        occurred_at = args.occurred_at
        if not occurred_at:
            raise SystemExit("因子修订事件需要 --occurred-at")
    else:
        occurred_at = args.occurred_at
        if not occurred_at:
            raise SystemExit("自定义事件需要 --occurred-at")
    event = {
        "id": "E" + _uuid.uuid4().hex[:10],
        "kind": args.kind,
        "device": args.device,
        "occurred_at": occurred_at,
        "detail": detail,
    }
    store.append("events", [event])
    _print({"event_registered": event})


def cmd_recheck_event(store: Store, args) -> None:
    event = store.find("events", args.event_id)
    if event is None:
        raise SystemExit(f"事件不存在：{args.event_id}")
    analyses = store.read("analyses")
    if not analyses:
        raise SystemExit("尚无分析运行")
    record = analyses[-1]
    as_of = parse_ts(args.as_of) if args.as_of else parse_ts(event["occurred_at"])
    engine, _, _, resolver = build_engine(
        store, as_of, n_trials=record["n_trials"], seed=record["seed"]
    )
    analysis = engine.run(record["scenario_id"])
    analysis["analysis_id"] = record["id"]
    ctx = ReviewContext(engine=engine, analysis=analysis, as_of=as_of)
    canonical_map = {d: resolver.canonical(d) for d in resolver.devices}
    corrections = recheck_after_event(store, event, ctx, canonical_map)
    _print({"event_id": args.event_id, "corrections": corrections})


def cmd_report(store: Store, args) -> None:
    analyses = store.read("analyses")
    if not analyses:
        raise SystemExit("尚无分析运行")
    if args.analysis_id:
        record = store.find("analyses", args.analysis_id)
        if record is None:
            raise SystemExit(f"分析运行不存在：{args.analysis_id}")
    elif args.scenario:
        matches = [a for a in analyses if a["scenario_id"] == args.scenario]
        if not matches:
            raise SystemExit(f"没有情景 {args.scenario} 的分析运行")
        record = matches[-1]
    else:
        # 默认取最近一次中枢情景，避免把上限/保守情景误当作项目口径
        central = [a for a in analyses if a["scenario_id"] == "central"]
        record = (central or analyses)[-1]
    engine, _, _, _ = build_engine(
        store, parse_ts(record["as_of"]),
        n_trials=record["n_trials"], seed=record["seed"],
    )
    analysis = engine.run(record["scenario_id"])
    analysis["analysis_id"] = record["id"]
    out_dir = pathlib.Path(args.output_dir) if args.output_dir else store.output_dir
    paths = write_reports(analysis, store, out_dir)
    _print({"reports": {k: str(v) for k, v in paths.items()},
            "scenario_used": record["scenario_id"],
            "claim_registry": claim_registry(store)})


def cmd_show_claim(store: Store, args) -> None:
    claim = store.find("claims", args.claim_id)
    if claim is None:
        raise SystemExit(f"声明不存在：{args.claim_id}")
    chain = [c for c in store.read("corrections") if c["claim_id"] == args.claim_id]
    _print({"claim_original_immutable": claim, "correction_chain": chain})


def cmd_reset(store: Store, args) -> None:
    if not args.yes:
        raise SystemExit("重置会清空 .local 账本；如确认请追加 --yes")
    shutil.rmtree(store.root, ignore_errors=True)
    print(f"已清空 {store.root}（data/ 原始资料未改动）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="circular_claims",
        description="家电循环利用环境效益声明本地流程（合成演练，非真实碳交易/认证）",
    )
    parser.add_argument("--state-dir", default=".local", help="本地状态目录（默认 .local）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-data", help="导入资料批次")
    p.add_argument("source")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("identities", help="查看规范身份与合并保护提示")
    p.set_defaults(func=cmd_identities)

    p = sub.add_parser("merge-identity", help="登记人工确认同一实物对应")
    p.add_argument("--kept", required=True, help="保留的规范设备 id")
    p.add_argument("--merged", required=True, help="被合并设备 id")
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence", required=True)
    p.set_defaults(func=cmd_merge_identity)

    p = sub.add_parser("analyze", help="运行净效益分析")
    p.add_argument("--scenario", default="central", choices=sorted(SCENARIO_IDS))
    p.add_argument("--as-of", default=None, help="ISO 时间戳；默认取全部 known_at 最大值")
    p.add_argument("--trials", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20260901)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("list-applications")
    p.set_defaults(func=cmd_list_applications)

    p = sub.add_parser("review", help="审阅申请（--issue 同时核发）")
    p.add_argument("--application", default=None)
    p.add_argument("--analysis-id", default=None)
    p.add_argument("--issue", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("register-event", help="登记退回/因子修订等复核事件")
    p.add_argument("--kind", required=True,
                   choices=["returned", "factor_revision", "identity_merge", "other"])
    p.add_argument("--device", required=True)
    p.add_argument("--occurred-at", default=None)
    p.add_argument("--detail", default=None, help="JSON 字符串")
    p.set_defaults(func=cmd_register_event)

    p = sub.add_parser("recheck-event", help="事件后复核并生成更正/撤回")
    p.add_argument("--event-id", required=True)
    p.add_argument("--as-of", default=None)
    p.set_defaults(func=cmd_recheck_event)

    p = sub.add_parser("report", help="产出下钻报告（默认取最近中枢情景）")
    p.add_argument("--analysis-id", default=None)
    p.add_argument("--scenario", default=None,
                   choices=sorted(SCENARIO_IDS), help="指定情景的最近一次运行")
    p.add_argument("--output-dir", default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("show-claim")
    p.add_argument("claim_id")
    p.set_defaults(func=cmd_show_claim)

    p = sub.add_parser("reset")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)
    return parser


# 延迟引用情景 id 列表
from .engine import SCENARIOS as _SCENARIOS  # noqa: E402

SCENARIO_IDS = tuple(_SCENARIOS)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.state_dir)
    try:
        args.func(store, args)
    except (contract.ContractError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
