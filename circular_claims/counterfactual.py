"""反事实情景估计：替代率、延寿年限与留出支持检查。

严格区分"参与估计"与"留出核对"：
- 替代率 = 调查中有明确新购替代意向（would_buy_new=true）的比例；
  null 表示意向未知，按契约既不算替代也不算不替代，只进入分母之外的说明。
- 延寿年限来自观察完整（observation_complete=true）记录的已观察月数，
  不是每件旧货都按一比一替代新购，也不是把销售日当成一整年服务。
- 估计仅使用 development 折；holdout 折绝不参与拟合，只用来检查假设的
  支持程度。检查不通过的单元，其设备结论必须显式降级。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .contract import Fold


@dataclass
class CellEstimate:
    kind: str
    region: str
    # 替代率（Jeffreys Beta 后验：Beta(0.5, 0.5) 先验）
    n_intent: int = 0
    n_known: int = 0
    n_unknown_intent: int = 0
    substitution_rate: float = 0.0
    rate_alpha: float = 0.5
    rate_beta: float = 0.5
    # 延寿（月）：仅完整观察记录
    life_months: list[float] = field(default_factory=list)
    life_mean_months: float = 0.0
    life_sem_months: float = 0.0
    # 留出检查
    holdout_n: int = 0
    holdout_checks: list[dict] = field(default_factory=list)
    support: str = "insufficient"  # supported | weak | contradicted | insufficient
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "region": self.region,
            "substitution": {
                "n_known_intent": self.n_known,
                "n_would_buy_new": self.n_intent,
                "n_unknown_intent": self.n_unknown_intent,
                "rate_point": round(self.substitution_rate, 6),
                "beta_posterior": [self.rate_alpha, self.rate_beta],
            },
            "life_extension": {
                "n_complete_obs": len(self.life_months),
                "mean_months": round(self.life_mean_months, 4),
                "sem_months": round(self.life_sem_months, 4),
                "mean_years": round(self.life_mean_months / 12.0, 4),
            },
            "holdout": {
                "n_records": self.holdout_n,
                "checks": self.holdout_checks,
            },
            "support": self.support,
            "limitations": self.limitations,
        }


def _binomial_tail_two_sided(k: int, n: int, p: float) -> float:
    """精确二项双侧 p 值：两侧概率不大于观测点概率的概率之和。"""
    if n == 0:
        return 1.0
    p = min(max(p, 1e-12), 1 - 1e-12)
    log_probs = [
        math.lgamma(n + 1)
        - math.lgamma(i + 1)
        - math.lgamma(n - i + 1)
        + i * math.log(p)
        + (n - i) * math.log(1 - p)
        for i in range(n + 1)
    ]
    observed = log_probs[k]
    return float(sum(math.exp(lp) for lp in log_probs if lp <= observed + 1e-12))


def _normal_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def estimate_cells(substitution: list[dict], min_dev_n: int = 15) -> dict[tuple[str, str], CellEstimate]:
    """从调查记录估计各 (kind, region) 单元，并用 holdout 折做支持检查。"""
    cells: dict[tuple[str, str], CellEstimate] = {}

    def cell(kind: str, region: str) -> CellEstimate:
        key = (kind, region)
        if key not in cells:
            cells[key] = CellEstimate(kind=kind, region=region)
        return cells[key]

    # ---- 估计：仅 development 折 -------------------------------------
    for row in substitution:
        if row["fold"] != Fold.DEVELOPMENT.value:
            continue
        c = cell(row["kind"], row["region"])
        if row["would_buy_new"] is None:
            c.n_unknown_intent += 1
        else:
            c.n_known += 1
            if row["would_buy_new"]:
                c.n_intent += 1
        if row["observation_complete"]:
            c.life_months.append(float(row["observed_service_months"]))

    for c in cells.values():
        if c.n_known > 0:
            c.rate_alpha = 0.5 + c.n_intent
            c.rate_beta = 0.5 + c.n_known - c.n_intent
            c.substitution_rate = c.rate_alpha / (c.rate_alpha + c.rate_beta)
        if c.life_months:
            n = len(c.life_months)
            mean = sum(c.life_months) / n
            c.life_mean_months = mean
            if n > 1:
                variance = sum((x - mean) ** 2 for x in c.life_months) / (n - 1)
                c.life_sem_months = math.sqrt(variance / n)

    # ---- 留出检查：holdout 折绝不参与拟合 -----------------------------
    holdout_by_cell: dict[tuple[str, str], list[dict]] = {}
    for row in substitution:
        if row["fold"] == Fold.HOLDOUT.value:
            holdout_by_cell.setdefault((row["kind"], row["region"]), []).append(row)

    for key, rows in holdout_by_cell.items():
        c = cells.get(key)
        if c is None:
            continue
        c.holdout_n = len(rows)
        known = [r for r in rows if r["would_buy_new"] is not None]
        if known and c.n_known > 0:
            k = sum(1 for r in known if r["would_buy_new"])
            n = len(known)
            p_value = _binomial_tail_two_sided(k, n, c.substitution_rate)
            c.holdout_checks.append(
                {
                    "check": "substitution_rate",
                    "holdout_yes": k,
                    "holdout_n": n,
                    "predicted_rate": round(c.substitution_rate, 4),
                    "two_sided_p": round(p_value, 4),
                    "contradicted": p_value < 0.05,
                }
            )
        complete = [
            float(r["observed_service_months"])
            for r in rows
            if r["observation_complete"]
        ]
        if complete and c.life_months:
            holdout_mean = sum(complete) / len(complete)
            if c.life_sem_months > 0:
                # 用发展折 SEM 作为均值标准误，做正态近似双侧检验
                z = (holdout_mean - c.life_mean_months) / c.life_sem_months
                p_value = _normal_two_sided(z)
            else:
                # 发展折观察值完全一致（SEM=0）：均值不同即判矛盾，避免除零
                delta = abs(holdout_mean - c.life_mean_months)
                z = float("inf") if delta > 1e-12 else 0.0
                p_value = 0.0 if delta > 1e-12 else 1.0
            c.holdout_checks.append(
                {
                    "check": "life_extension",
                    "holdout_n": len(complete),
                    "holdout_mean_months": round(holdout_mean, 3),
                    "predicted_mean_months": round(c.life_mean_months, 3),
                    "z": round(z, 3) if math.isfinite(z) else "inf",
                    "two_sided_p": round(p_value, 4),
                    "contradicted": p_value < 0.05,
                }
            )

    # ---- 支持程度定级 -------------------------------------------------
    for c in cells.values():
        contradicted = any(chk.get("contradicted") for chk in c.holdout_checks)
        weak = (
            c.n_known < min_dev_n
            or len(c.life_months) < 10
            or c.holdout_n < 8
        )
        if contradicted:
            c.support = "contradicted"
            c.limitations.append("holdout 留出记录否定了发展折估计，结论不得按原假设声明")
        elif c.n_known == 0 or not c.life_months:
            c.support = "insufficient"
            c.limitations.append("发展折缺少替代意向或完整寿命观察，无法估计反事实")
        elif weak:
            c.support = "weak"
            c.limitations.append("样本量偏小，反事实参数仅具弱支持")
        else:
            c.support = "supported"
        if c.n_unknown_intent:
            c.limitations.append(
                f"{c.n_unknown_intent} 条替代意向未知记录已排除出替代率分母"
            )
    return cells


def bootstrap_life_months(
    c: CellEstimate, rng: random.Random
) -> float:
    """对发展折完整寿命观察做一次非参数 Bootstrap 重采样均值。"""
    if not c.life_months:
        return 0.0
    return sum(rng.choice(c.life_months) for _ in c.life_months) / len(c.life_months)
