"""报告与下钻：项目合计、设备明细、声明权利与更正链。

所有数字区分 realized（已实现）与 projected（预计）；
共同披露在项目总计中按设备服务只计一次，不按申请方重复累加。
报告同时产出 JSON（机器可读）与 Markdown（合作方审阅）。
"""
from __future__ import annotations

import pathlib

from .claims import WITHDRAWAL, active_claims


def project_summary(analysis: dict, store) -> dict:
    """汇总项目合计，并按声明权利口径核对不重复累加。"""
    devices = analysis["devices"]
    totals = {
        "n_canonical_devices": len(devices),
        "n_active": sum(1 for d in devices if d["active"]),
        "n_returned": sum(1 for d in devices if d["returned"]),
        "realized_avoided_kgco2e": round(
            sum(d["avoided_kgco2e"]["realized"] for d in devices), 3
        ),
        "projected_avoided_kgco2e": round(
            sum(d["avoided_kgco2e"]["projected"] for d in devices), 3
        ),
        "actual_burden_kgco2e_mid": round(
            sum(d["actual_burden_kgco2e"]["mid"] for d in devices), 3
        ),
        "net_realized_kgco2e": round(
            sum(d["net_kgco2e"]["realized"] for d in devices), 3
        ),
        "net_total_kgco2e": round(
            sum(d["net_kgco2e"]["total"] for d in devices), 3
        ),
    }
    # 声明口径：按规范设备去重，核对共同披露没有重复计入
    claims = active_claims(store)
    claimed_devices = {c["canonical_device_id"] for c in claims}
    totals["claims"] = {
        "n_active_claims": len(claims),
        "n_covered_devices": len(claimed_devices),
        "n_joint_parties": sum(
            len([p for p in c["parties"] if p["mode"] == "joint"]) for c in claims
        ),
        "double_count_check": (
            "按设备服务计一次；共同披露多方不重复累加"
        ),
    }
    return totals


def claim_registry(store) -> list[dict]:
    """声明登记册：状态、权利方、情景、更正链。"""
    corrections_by_claim: dict[str, list[dict]] = {}
    for corr in store.read("corrections"):
        corrections_by_claim.setdefault(corr["claim_id"], []).append(corr)
    registry = []
    for claim in store.read("claims"):
        chain = corrections_by_claim.get(claim["id"], [])
        withdrawn = any(c["kind"] == WITHDRAWAL for c in chain)
        registry.append(
            {
                "claim_id": claim["id"],
                "application_id": claim["application_id"],
                "canonical_device_id": claim["canonical_device_id"],
                "parties": claim["parties"],
                "service_from": claim["service_from"],
                "service_to": claim["service_to"],
                "approved_spans": claim["approved_spans"],
                "scenario_id": claim["scenario_id"],
                "status": "withdrawn" if withdrawn else claim["status"],
                "issued_at": claim["issued_at"],
                "correction_chain": [
                    {
                        "correction_id": c["id"],
                        "kind": c["kind"],
                        "event_id": c["event_id"],
                        "remaining_spans": c["remaining_spans"],
                        "note": c["note"],
                        "created_at": c["created_at"],
                    }
                    for c in chain
                ],
            }
        )
    return registry


def build_markdown(analysis: dict, store) -> str:
    totals = project_summary(analysis, store)
    mc = analysis["monte_carlo"]
    lines: list[str] = []
    lines.append("# 循环利用环境效益审阅报告（本地合成演练）")
    lines.append("")
    lines.append(
        "> 本报告基于合成活动数据与公开格式演练因子，不构成真实碳交易或法定认证。"
        "没有证据的未来收益只列为 projected，不计为已实现减排。"
    )
    lines.append("")
    lines.append(f"- 分析 as-of 时点：{analysis['as_of']}")
    lines.append(f"- 锁定情景：{analysis['scenario']['id']} — {analysis['scenario']['label']}")
    lines.append(f"- 规范设备数：{totals['n_canonical_devices']}（在用 {totals['n_active']} / 退回 {totals['n_returned']}）")
    lines.append("")
    lines.append("## 1. 项目合计（kg CO2e，已实现与预计分列）")
    lines.append("")
    lines.append("| 指标 | 点值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 已实现反事实避免量（realized） | {totals['realized_avoided_kgco2e']} |")
    lines.append(f"| 预计未来避免量（projected，未实现） | {totals['projected_avoided_kgco2e']} |")
    lines.append(f"| 已发生负担（维修/运输/使用，中点） | {totals['actual_burden_kgco2e_mid']} |")
    lines.append(f"| 已实现净效益 | {totals['net_realized_kgco2e']} |")
    lines.append(f"| 含预计的净效益（审阅参考，非已实现） | {totals['net_total_kgco2e']} |")
    lines.append("")
    corr = mc["correlated"]["net_total"]
    lines.append(
        f"蒙特卡洛（{mc['n_trials']} 次，保留同源相关性）净效益合计 "
        f"p05={corr['p05']} / p50={corr['p50']} / p95={corr['p95']}"
    )
    if mc.get("correlation_note"):
        note = mc["correlation_note"]
        lines.append(
            f"- 相关束区间宽度 {note['correlated_p05_p95_width']} vs "
            f"朴素独立束 {note['naive_independent_width']}：{note['interpretation']}"
        )
    lines.append("")
    lines.append("## 2. 反事实单元与留出支持检查")
    lines.append("")
    lines.append("| 类别 | 地区 | 替代率 | 延寿(年) | 支持程度 | holdout 检查 |")
    lines.append("| --- | --- | ---: | ---: | --- | --- |")
    for c in analysis["cells"]:
        checks = "; ".join(
            f"{h['check']}(p={h['two_sided_p']},矛盾={h['contradicted']})"
            for h in c["holdout"]["checks"]
        ) or "无留出记录"
        lines.append(
            f"| {c['kind']} | {c['region']} | {c['substitution']['rate_point']} "
            f"| {c['life_extension']['mean_years']} | {c['support']} | {checks} |"
        )
    lines.append("")
    lines.append("## 3. 设备下钻（服务期间 → 实际负担 → 反事实 → 引用权利）")
    lines.append("")
    registry = {r["canonical_device_id"]: r for r in claim_registry(store)}
    for d in analysis["devices"]:
        lines.append(f"### {d['device_id']}（{d['kind']}/{d['region']}，"
                     f"{'在用' if d['active'] else '已关闭'}{'，退回' if d['returned'] else ''}）")
        if d["merged_members"]:
            lines.append(f"- 合并来源：{', '.join(d['merged_members'])}（人工确认同一实物）")
        lines.append(f"- 已观测服务：{d['service']['observed_service_years']} 服务年；"
                     f"预期延寿：{d['service']['expected_life_years']} 年；替代率：{d['service']['substitution_rate']}")
        for ep in d["episodes"]:
            lines.append(
                f"- 服务片段 {ep['opened_on']} ~ {ep['closed_on'] or '进行中'} "
                f"（{ep['close_reason'] or 'active'}）：观测 {ep['observed_hours']} 小时，"
                f"因子 {ep['factor_id'] or '越界'}（{ep['factor_status']}）"
            )
        ab = d["actual_burden_kgco2e"]
        lines.append(
            f"- 已发生负担：{ab['low']} ~ {ab['high']}（中点 {ab['mid']}，{ab['n_records']} 条记录）"
        )
        lines.append(
            f"- 反事实避免：已实现 {d['avoided_kgco2e']['realized']} / "
            f"预计 {d['avoided_kgco2e']['projected']}；"
            f"净效益 已实现 {d['net_kgco2e']['realized']} / 含预计 {d['net_kgco2e']['total']}"
        )
        if d["limitations"]:
            lines.append("- 限制：" + "；".join(d["limitations"]))
        claim = registry.get(d["device_id"])
        if claim:
            parties = ", ".join(f"{p['applicant']}({p['mode']})" for p in claim["parties"])
            lines.append(
                f"- 引用权利：声明 {claim['claim_id']} 状态={claim['status']} "
                f"权利方=[{parties}] 情景={claim['scenario_id']} "
                f"更正链={len(claim['correction_chain'])} 条"
            )
        else:
            lines.append("- 引用权利：无有效声明")
        lines.append("")
    lines.append("## 4. 声明与更正链")
    lines.append("")
    lines.append("| 声明 | 设备 | 权利方(模式) | 区间 | 情景 | 状态 | 更正 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in claim_registry(store):
        parties = ", ".join(f"{p['applicant']}({p['mode']})" for p in r["parties"])
        lines.append(
            f"| {r['claim_id']} | {r['canonical_device_id']} | {parties} "
            f"| {r['service_from']}~{r['service_to']} | {r['scenario_id']} "
            f"| {r['status']} | {len(r['correction_chain'])} |"
        )
    lines.append("")
    lines.append("## 5. 关键假设与适用范围限制")
    lines.append("")
    lines.append("- 因子仅为被替代新设备制造负担的演练因子，单位 kg CO2e/服务年；"
                 "使用阶段反事实无适用因子，未估算使用侧净效应。")
    lines.append("- 替代率与延寿来自 development 折调查，holdout 折仅做留出检查；"
                 "contradicted/insufficient 单元的预计项按零处理。")
    lines.append("- 一次销售不等于完整寿命；退回设备无在用观测时已实现避免量为零。")
    lines.append("- 因子跨地区/跨用途/超有效期不可比，相关区间已显式标记 limited_spans。")
    return "\n".join(lines) + "\n"


def write_reports(analysis: dict, store, output_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "analysis_report.json"
    md_path = output_dir / "analysis_report.md"
    payload = {
        "summary": project_summary(analysis, store),
        "analysis": analysis,
        "claims": claim_registry(store),
    }
    json_path.write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(build_markdown(analysis, store), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
