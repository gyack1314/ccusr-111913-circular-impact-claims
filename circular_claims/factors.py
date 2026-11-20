"""演练因子注册：按资料契约执行适用范围匹配。

因子是"被替代新设备制造负担"的合成演练因子（kg CO2e/服务年），并非现实系数。
跨用途、跨地区、超出有效期的因子没有可比依据：不做最近邻猜测，直接标记
out_of_scope 并限制结论。因子修订通过新 id + 新有效期进入，旧因子保留可追溯。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .timeutil import parse_date, parse_ts


@dataclass(frozen=True)
class FactorMatch:
    factor: dict
    status: str  # in_scope | out_of_scope
    reason: str | None = None


class FactorRegistry:
    def __init__(self, factor_rows: list[dict], as_of: dt.datetime):
        # as_of 之后才获知的因子不可用于当前分析
        self.rows = [f for f in factor_rows if parse_ts(f["known_at"]) <= as_of]
        self.as_of = as_of

    def lookup(
        self,
        kind: str,
        region: str,
        on_date: dt.date,
        unit: str = "kgco2e_per_service_year",
    ) -> FactorMatch:
        """返回当日适用的唯一因子；同一窗口出现多个候选时要求显式消歧。"""
        candidates = [
            f
            for f in self.rows
            if f["kind"] == kind
            and f["region"] == region
            and f["unit"] == unit
            and parse_date(f["valid_from"]) <= on_date < parse_date(f["valid_to"])
        ]
        if not candidates:
            # 区分"完全无因子"与"有因子但时间不适用"，便于报告解释
            scoped = [
                f
                for f in self.rows
                if f["kind"] == kind and f["region"] == region and f["unit"] == unit
            ]
            if scoped:
                windows = [
                    f"{f['valid_from']}~{f['valid_to']}({f['id']})" for f in scoped
                ]
                return FactorMatch(
                    factor={},
                    status="out_of_scope",
                    reason=f"该地域/类别因子在 {on_date} 不在有效期内：{windows}",
                )
            return FactorMatch(
                factor={},
                status="out_of_scope",
                reason=f"缺少 kind={kind} region={region} unit={unit} 的因子，"
                "跨地区/跨用途因子不可比",
            )
        if len(candidates) > 1:
            return FactorMatch(
                factor={},
                status="out_of_scope",
                reason="同一适用窗口存在多个因子候选，需人工消歧："
                + ",".join(f["id"] for f in candidates),
            )
        return FactorMatch(factor=candidates[0], status="in_scope")

    def coverage_window(self, kind: str, region: str) -> tuple[dt.date, dt.date] | None:
        """返回某类别/地区已登记因子覆盖的最大区间（用于声明期限制说明）。"""
        rows = [f for f in self.rows if f["kind"] == kind and f["region"] == region]
        if not rows:
            return None
        return (
            min(parse_date(f["valid_from"]) for f in rows),
            max(parse_date(f["valid_to"]) for f in rows),
        )

    def correlation_groups(self) -> set[str]:
        return {f["correlation_group"] for f in self.rows}
