"""净效益引擎：把实际流转、已发生负担与有依据的反事实情景联系起来。

三类量严格分账，任何报告与声明都不得混同：
1. actual_burden   已经发生的负担（维修、额外运输、使用、拆解），全额保留；
2. realized        已观测服务对应的反事实避免量——服务已发生，证据可查；
3. projected       未来使用情景的预计避免量——没有证据的未来收益绝不可计为已实现。

避免量只来自"被替代新设备制造负担"因子（kg CO2e/服务年）乘以替代率与
有效服务年；一次销售不等于一整年服务，退回/提前报废按实际观测截断。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .counterfactual import CellEstimate
from .factors import FactorRegistry
from .identity import IdentityResolver
from .lifecycle import (
    DeviceTimeline,
    burden_totals,
    build_all,
    visible_burdens,
)
from .timeutil import parse_date, parse_ts
from .uncertainty import DrawBundle, quantile, summarize

SERVICE_HOURS_PER_YEAR = 2000.0


@dataclass(frozen=True)
class ScenarioConfig:
    """情景只改变点值口径；蒙特卡洛分布由证据本身决定，不随情景挑选。"""

    scenario_id: str
    label: str
    factor_pick: str     # midpoint | low | high
    rate_pick: str       # posterior_mean | conservative(low) | ambitious(high)
    life_pick: str       # mean | low | high
    description: str


SCENARIOS: dict[str, ScenarioConfig] = {
    "central": ScenarioConfig(
        "central",
        "中枢情景：因子区间中点、替代率后验均值、延寿均值",
        "midpoint", "posterior_mean", "mean",
        "所有点值取证据区间的中枢；不确定范围另由蒙特卡洛 p05/p95 给出。",
    ),
    "conservative": ScenarioConfig(
        "conservative",
        "保守情景：因子下界、替代率 p05、延寿 p05",
        "low", "low", "low",
        "用于合作方审慎审阅；低估避免量、不低估已发生负担。",
    ),
    "ambitious": ScenarioConfig(
        "ambitious",
        "上限情景：因子上界、替代率 p95、延寿 p95",
        "high", "high", "high",
        "仅展示证据支持的上沿，不得与其他情景的最大项拼接成一张声明。",
    ),
}


def _factor_point(factor: dict, pick: str) -> float:
    if pick == "low":
        return factor["low"]
    if pick == "high":
        return factor["high"]
    return (factor["low"] + factor["high"]) / 2.0


class Engine:
    def __init__(
        self,
        tables: dict[str, list[dict]],
        as_of: dt.datetime,
        resolver: IdentityResolver,
        cells: dict[tuple[str, str], CellEstimate],
        registry: FactorRegistry,
        draws: DrawBundle,
        naive_draws: DrawBundle | None = None,
    ):
        self.tables = tables
        self.as_of = as_of
        self.resolver = resolver
        self.cells = cells
        self.registry = registry
        self.draws = draws
        self.naive_draws = naive_draws
        # 保守/上限情景的点值取单元级共享抽样的分位数（情景只改点值口径）
        self._rate_q: dict[tuple[str, str], dict[str, float]] = {}
        self._life_q: dict[tuple[str, str], dict[str, float]] = {}
        for key in cells:
            self._rate_q[key] = {
                "low": quantile(draws.rate_draws[key], 0.05),
                "high": quantile(draws.rate_draws[key], 0.95),
            }
            self._life_q[key] = {
                "low": quantile(draws.life_draws[key], 0.05) / 12.0,
                "high": quantile(draws.life_draws[key], 0.95) / 12.0,
            }
        self.timelines = build_all(
            tables.get("devices", []),
            tables.get("movements", []),
            as_of,
            {d: resolver.canonical(d) for d in resolver.devices},
        )
        self.burdens_by_device = visible_burdens(tables.get("burdens", []), as_of)

    # ------------------------------------------------------------------
    def _cell(self, timeline: DeviceTimeline) -> CellEstimate | None:
        return self.cells.get((timeline.kind, timeline.region))

    def _factor_for(self, timeline: DeviceTimeline, on_date: dt.date):
        return self.registry.lookup(timeline.kind, timeline.region, on_date)

    def _episode_geometry(self, timeline: DeviceTimeline, cell: CellEstimate | None):
        """返回每个服务片段的几何信息与因子适用状态。

        已实现服务与预计未来服务可能跨越因子修订边界，因此分别取因子：
        - realized 因子：观测时点适用的因子（历史服务按历史依据计价）；
        - projected 因子：as-of 时点适用的因子（反映已获知的因子修订）。
        """
        out = []
        coverage = self.registry.coverage_window(timeline.kind, timeline.region)
        for ep in timeline.episodes:
            observed_date = ep.closed_on or ep.observed_through or ep.opened_on
            realized_match = self._factor_for(timeline, observed_date)
            # 仍在进行的片段，预计部分按 as-of 时点选当前因子（修订在此生效）
            projected_date = self.as_of.date() if ep.is_active else observed_date
            projected_match = self._factor_for(timeline, projected_date)
            factor_cover_to = coverage[1].isoformat() if coverage else None
            out.append(
                {
                    "opened_on": ep.opened_on.isoformat(),
                    "closed_on": ep.closed_on.isoformat() if ep.closed_on else None,
                    "close_reason": ep.close_reason,
                    "observed_hours": ep.observed_hours,
                    "observed_years": ep.observed_service_years,
                    "observed_through": (
                        ep.observed_through.isoformat()
                        if ep.observed_through else None
                    ),
                    "factor_status": realized_match.status,
                    "factor_reason": realized_match.reason,
                    "factor_id": realized_match.factor.get("id"),
                    "factor": realized_match.factor,
                    "projected_factor_id": projected_match.factor.get("id"),
                    "projected_factor_status": projected_match.status,
                    "projected_factor_reason": projected_match.reason,
                    "projected_factor": projected_match.factor,
                    "factor_cover_to": factor_cover_to,
                }
            )
        return out

    def _projected_years_capped(
        self, geo: dict, observed_years: float, life_years: float
    ) -> float:
        """预计剩余服务年：延寿截尾，再按因子覆盖终点截断（无依据不估算）。"""
        projected_years = max(0.0, life_years - observed_years)
        cover_to = geo.get("factor_cover_to")
        if cover_to and geo["observed_through"]:
            from .timeutil import years_between

            cover_cap = years_between(
                parse_date(geo["observed_through"]), parse_date(cover_to)
            )
            projected_years = min(projected_years, max(0.0, cover_cap))
        return projected_years

    # ------------------------------------------------------------------
    def compute_device_point(
        self, canonical_id: str, scenario: ScenarioConfig
    ) -> dict:
        tl = self.timelines[canonical_id]
        cell = self._cell(tl)
        geometry = self._episode_geometry(tl, cell)
        limitations: list[str] = []

        key = (tl.kind, tl.region)
        if cell and cell.n_known > 0:
            if scenario.rate_pick == "posterior_mean":
                rate = cell.substitution_rate
            else:
                rate = self._rate_q[key][scenario.rate_pick]
        else:
            rate = 0.0
        if cell and cell.life_months:
            if scenario.life_pick == "mean":
                life_years = cell.life_mean_months / 12.0
            else:
                life_years = self._life_q[key][scenario.life_pick]
        else:
            life_years = 0.0
        if cell:
            if cell.support in ("contradicted", "insufficient"):
                limitations.append(
                    f"反事实单元支持程度={cell.support}，预计项按 0 处理"
                )
            limitations.extend(cell.limitations)

        realized_avoided = 0.0
        projected_avoided = 0.0
        for geo in geometry:
            if geo["factor_status"] != "in_scope":
                limitations.append(
                    f"服务片段 {geo['opened_on']} 已实现期因子越界：{geo['factor_reason']}"
                )
            else:
                factor_value = _factor_point(geo["factor"], scenario.factor_pick)
                realized_avoided += (
                    geo["observed_years"] * factor_value * rate
                )
            if geo["close_reason"] is None and cell and cell.support not in (
                "contradicted",
                "insufficient",
            ):
                if geo["projected_factor_status"] != "in_scope":
                    limitations.append(
                        f"服务片段 {geo['opened_on']} 预计期因子越界："
                        f"{geo['projected_factor_reason']}"
                    )
                    continue
                # 仍在进行：预计剩余服务 = 预期延寿 - 已观测服务，并按因子覆盖截断
                projected_years = self._projected_years_capped(
                    geo, geo["observed_years"], life_years
                )
                projected_value = _factor_point(
                    geo["projected_factor"], scenario.factor_pick
                )
                projected_avoided += projected_years * projected_value * rate

        rows = self.burdens_by_device.get(canonical_id, [])
        # 负担按规范设备折叠（被合并设备的负担同样计入）
        member_ids = set(self.resolver.members(canonical_id))
        rows = [
            b
            for b in self.tables.get("burdens", [])
            if b["device"] in member_ids and parse_ts(b["known_at"]) <= self.as_of
        ]
        totals = burden_totals(rows)
        actual_low = sum(v[0] for v in totals.values())
        actual_high = sum(v[1] for v in totals.values())
        actual_mid = (actual_low + actual_high) / 2.0

        realized_net = realized_avoided - actual_mid
        projected_net = projected_avoided  # 未来负担没有证据，不臆造
        return {
            "device_id": canonical_id,
            "merged_members": self.resolver.merged_only(canonical_id),
            "kind": tl.kind,
            "region": tl.region,
            "serial": tl.serial,
            "active": any(e.is_active for e in tl.episodes),
            "returned": tl.has_return(),
            "dismantled_on": tl.dismantled_on.isoformat() if tl.dismantled_on else None,
            "cell_support": cell.support if cell else "insufficient",
            "episodes": geometry,
            "service": {
                "observed_service_years": round(tl.realized_service_years(), 6),
                "expected_life_years": round(life_years, 6),
                "substitution_rate": round(rate, 6),
            },
            "actual_burden_kgco2e": {
                "low": round(actual_low, 3),
                "mid": round(actual_mid, 3),
                "high": round(actual_high, 3),
                "by_kind": {k: [round(v[0], 3), round(v[1], 3)] for k, v in totals.items()},
                "n_records": len(rows),
            },
            "avoided_kgco2e": {
                "realized": round(realized_avoided, 3),
                "projected": round(projected_avoided, 3),
            },
            "net_kgco2e": {
                "realized": round(realized_net, 3),
                "projected": round(projected_net, 3),
                "total": round(realized_net + projected_net, 3),
            },
            "limitations": sorted(set(limitations)),
        }

    # ------------------------------------------------------------------
    def compute_device_mc(self, canonical_id: str) -> dict:
        """逐试验计算单设备净效益，保留因子组/调查单元的共享抽样。"""
        tl = self.timelines[canonical_id]
        cell = self._cell(tl)
        key = (tl.kind, tl.region)
        geometry = self._episode_geometry(tl, cell)
        member_ids = set(self.resolver.members(canonical_id))
        burden_ids = [
            b["id"]
            for b in self.tables.get("burdens", [])
            if b["device"] in member_ids and parse_ts(b["known_at"]) <= self.as_of
        ]
        n = self.draws.n_trials
        realized = [0.0] * n
        projected = [0.0] * n
        burden = [0.0] * n
        for t in range(n):
            rate = self.draws.rate_draws[key][t] if cell else 0.0
            life_years = (
                self.draws.life_draws[key][t] / 12.0 if cell else 0.0
            )
            for geo in geometry:
                if geo["factor_status"] == "in_scope":
                    fv = self.draws.factor_draws[geo["factor_id"]][t]
                    realized[t] += geo["observed_years"] * fv * rate
                if (
                    geo["close_reason"] is None
                    and cell
                    and cell.support not in ("contradicted", "insufficient")
                    and geo["projected_factor_status"] == "in_scope"
                ):
                    projected_years = self._projected_years_capped(
                        geo, geo["observed_years"], life_years
                    )
                    pfv = self.draws.factor_draws[geo["projected_factor_id"]][t]
                    projected[t] += projected_years * pfv * rate
            burden[t] = sum(self.draws.burden_draws[bid][t] for bid in burden_ids)
        realized_net = [realized[t] - burden[t] for t in range(n)]
        return {
            "device_id": canonical_id,
            "realized_avoided": summarize(realized),
            "projected_avoided": summarize(projected),
            "actual_burden": summarize(burden),
            "net_realized": summarize(realized_net),
            "net_total": summarize([realized_net[t] + projected[t] for t in range(n)]),
        }

    def project_total_mc(self, bundle: DrawBundle) -> dict:
        """用指定抽样束汇总项目合计；相关束与朴素束的区间宽度可直接对比。"""
        n = bundle.n_trials
        totals = {
            "realized_avoided": [0.0] * n,
            "projected_avoided": [0.0] * n,
            "actual_burden": [0.0] * n,
            "net_realized": [0.0] * n,
            "net_total": [0.0] * n,
        }
        for canonical_id in self.timelines:
            tl = self.timelines[canonical_id]
            cell = self._cell(tl)
            key = (tl.kind, tl.region)
            geometry = self._episode_geometry(tl, cell)
            member_ids = set(self.resolver.members(canonical_id))
            burden_ids = [
                b["id"]
                for b in self.tables.get("burdens", [])
                if b["device"] in member_ids
                and parse_ts(b["known_at"]) <= self.as_of
            ]
            for t in range(n):
                rate = bundle.rate_draws[key][t] if cell else 0.0
                life_years = bundle.life_draws[key][t] / 12.0 if cell else 0.0
                for geo in geometry:
                    if geo["factor_status"] == "in_scope":
                        fv = bundle.factor_draws[geo["factor_id"]][t]
                        totals["realized_avoided"][t] += (
                            geo["observed_years"] * fv * rate
                        )
                    if (
                        geo["close_reason"] is None
                        and cell
                        and cell.support not in ("contradicted", "insufficient")
                        and geo["projected_factor_status"] == "in_scope"
                    ):
                        py = self._projected_years_capped(
                            geo, geo["observed_years"], life_years
                        )
                        pfv = bundle.factor_draws[geo["projected_factor_id"]][t]
                        totals["projected_avoided"][t] += py * pfv * rate
                b = sum(bundle.burden_draws[bid][t] for bid in burden_ids)
                totals["actual_burden"][t] += b
        # 净额在全部设备汇总后统一计算，避免逐设备累加污染
        for t in range(n):
            totals["net_realized"][t] = (
                totals["realized_avoided"][t] - totals["actual_burden"][t]
            )
            totals["net_total"][t] = (
                totals["net_realized"][t] + totals["projected_avoided"][t]
            )
        return {k: summarize(v) for k, v in totals.items()}

    # ------------------------------------------------------------------
    def run(self, scenario_id: str = "central") -> dict:
        scenario = SCENARIOS[scenario_id]
        devices_point = [
            self.compute_device_point(cid, scenario)
            for cid in sorted(self.timelines)
        ]
        devices_mc = {
            cid: self.compute_device_mc(cid) for cid in sorted(self.timelines)
        }
        point_totals = {
            "actual_burden_mid": round(
                sum(d["actual_burden_kgco2e"]["mid"] for d in devices_point), 3
            ),
            "realized_avoided": round(
                sum(d["avoided_kgco2e"]["realized"] for d in devices_point), 3
            ),
            "projected_avoided": round(
                sum(d["avoided_kgco2e"]["projected"] for d in devices_point), 3
            ),
            "net_realized": round(
                sum(d["net_kgco2e"]["realized"] for d in devices_point), 3
            ),
            "net_total": round(
                sum(d["net_kgco2e"]["total"] for d in devices_point), 3
            ),
        }
        mc_total = self.project_total_mc(self.draws)
        naive_total = (
            self.project_total_mc(self.naive_draws) if self.naive_draws else None
        )
        correlation_note = None
        if naive_total:
            def width(summary: dict) -> float:
                return summary["p95"] - summary["p05"]
            correlation_note = {
                "correlated_p05_p95_width": round(width(mc_total["net_total"]), 3),
                "naive_independent_width": round(width(naive_total["net_total"]), 3),
                "interpretation": (
                    "朴素独立抽样把同源因子当独立误差平均，区间虚窄；"
                    "正式结论只采用相关束。"
                ),
            }
        return {
            "as_of": self.as_of.isoformat(),
            "scenario": {
                "id": scenario.scenario_id,
                "label": scenario.label,
                "description": scenario.description,
            },
            "n_canonical_devices": len(devices_point),
            "point_totals_kgco2e": point_totals,
            "monte_carlo": {
                "n_trials": self.draws.n_trials,
                "seed": self.draws.seed,
                "correlated": mc_total,
                "naive_independent_reference": naive_total,
                "correlation_note": correlation_note,
            },
            "cells": [
                c.as_dict() for c in sorted(self.cells.values(), key=lambda x: (x.kind, x.region))
            ],
            "devices": devices_point,
            "devices_mc": devices_mc,
        }
