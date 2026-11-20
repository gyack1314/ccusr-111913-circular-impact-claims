"""不确定性传播：蒙特卡洛抽样，显式保留同源相关性。

关键纪律（任务要求）：同一来源的因子不确定性会同时影响多个设备，
不能把它们当独立误差平均掉。因此：
- 因子按 correlation_group 共享一次均匀抽样，组内所有因子同升同降；
- 替代率 / 延寿是 (kind, region) 单元级调查参数，单元内所有设备共享一次抽样；
- 设备自身的已发生负担彼此独立抽样。
另提供 naive_independent 模式：故意忽略相关性（每个因子各抽各的），
仅用于对照演示"错误平均"会如何虚窄项目合计区间，不用于正式结论。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .counterfactual import CellEstimate, bootstrap_life_months


@dataclass
class DrawBundle:
    seed: int
    n_trials: int
    correlated: bool
    # factor_id -> 每次试验的因子值（kg CO2e/服务年）
    factor_draws: dict[str, list[float]]
    # (kind, region) -> 每次试验的替代率 / 延寿月数
    rate_draws: dict[tuple[str, str], list[float]]
    life_draws: dict[tuple[str, str], list[float]]
    # burden row_id -> 每次试验的负担值
    burden_draws: dict[str, list[float]]


def _beta_sample(rng: random.Random, alpha: float, beta: float) -> float:
    # 标准库没有 beta，用两个 Gamma(shape, 1) 构造
    x = rng.gammavariate(alpha, 1.0)
    y = rng.gammavariate(beta, 1.0)
    return x / (x + y)


def build_draws(
    factors: list[dict],
    cells: dict[tuple[str, str], CellEstimate],
    burden_rows: list[dict],
    n_trials: int = 2000,
    seed: int = 20260901,
    correlated: bool = True,
) -> DrawBundle:
    rng = random.Random(seed)
    factor_draws: dict[str, list[float]] = {f["id"]: [] for f in factors}
    rate_draws: dict[tuple[str, str], list[float]] = {}
    life_draws: dict[tuple[str, str], list[float]] = {}

    groups = sorted({f["correlation_group"] for f in factors})
    for _ in range(n_trials):
        # 因子：相关模式下每个来源组只抽一次
        if correlated:
            group_u = {g: rng.random() for g in groups}
        for f in factors:
            u = group_u[f["correlation_group"]] if correlated else rng.random()
            factor_draws[f["id"]].append(f["low"] + u * (f["high"] - f["low"]))

    for key, cell in cells.items():
        rate_draws[key] = []
        life_draws[key] = []
        rng_cell = random.Random(seed + hash(key) % 1_000_000)
        for _ in range(n_trials):
            if cell.n_known > 0:
                rate_draws[key].append(
                    _beta_sample(rng_cell, cell.rate_alpha, cell.rate_beta)
                )
            else:
                rate_draws[key].append(0.0)
            life_draws[key].append(bootstrap_life_months(cell, rng_cell))

    burden_draws: dict[str, list[float]] = {b["id"]: [] for b in burden_rows}
    rng_b = random.Random(seed + 7)
    for b in burden_rows:
        for _ in range(n_trials):
            u = rng_b.random()
            burden_draws[b["id"]].append(
                b["kgco2e_low"] + u * (b["kgco2e_high"] - b["kgco2e_low"])
            )

    return DrawBundle(
        seed=seed,
        n_trials=n_trials,
        correlated=correlated,
        factor_draws=factor_draws,
        rate_draws=rate_draws,
        life_draws=life_draws,
        burden_draws=burden_draws,
    )


def quantile(samples: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认线性法一致）。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "p05": round(quantile(samples, 0.05), 3),
        "p50": round(quantile(samples, 0.50), 3),
        "p95": round(quantile(samples, 0.95), 3),
        "mean": round(sum(samples) / len(samples), 3),
    }
