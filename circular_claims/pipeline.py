"""分析流水线：把存储中的资料组装为一次可复现的分析运行。"""
from __future__ import annotations

import datetime as dt
import uuid

from .counterfactual import estimate_cells
from .engine import SCENARIOS, Engine
from .factors import FactorRegistry
from .identity import IdentityResolver
from .timeutil import latest_known_at
from .uncertainty import build_draws


def build_engine(
    store,
    as_of: dt.datetime | None = None,
    n_trials: int = 2000,
    seed: int = 20260901,
    with_naive: bool = True,
) -> tuple[Engine, dict[str, list[dict]], dt.datetime, IdentityResolver]:
    tables = store.all_tables_ignoring_asof()
    if as_of is None:
        as_of = latest_known_at(tables)
    visible_tables = store.tables(as_of)
    resolver = IdentityResolver(
        visible_tables.get("devices", []), store.read("identity_links")
    )
    cells = estimate_cells(visible_tables.get("substitution", []))
    registry = FactorRegistry(visible_tables.get("factors", []), as_of)
    draws = build_draws(
        registry.rows,
        cells,
        visible_tables.get("burdens", []),
        n_trials=n_trials,
        seed=seed,
        correlated=True,
    )
    naive = (
        build_draws(
            registry.rows,
            cells,
            visible_tables.get("burdens", []),
            n_trials=n_trials,
            seed=seed,
            correlated=False,
        )
        if with_naive
        else None
    )
    engine = Engine(
        visible_tables, as_of, resolver, cells, registry, draws, naive
    )
    return engine, visible_tables, as_of, resolver


def run_analysis(
    store,
    scenario_id: str = "central",
    as_of: dt.datetime | None = None,
    n_trials: int = 2000,
    seed: int = 20260901,
) -> dict:
    if scenario_id not in SCENARIOS:
        raise KeyError(f"未知情景 {scenario_id}，可选 {sorted(SCENARIOS)}")
    engine, _, as_of, resolver = build_engine(
        store, as_of, n_trials=n_trials, seed=seed
    )
    analysis = engine.run(scenario_id)
    analysis["analysis_id"] = "AN" + uuid.uuid4().hex[:10]
    analysis["canonical_map"] = {
        d: resolver.canonical(d) for d in resolver.devices
    }
    store.append(
        "analyses",
        [
            {
                "id": analysis["analysis_id"],
                "scenario_id": scenario_id,
                "as_of": as_of.isoformat(),
                "n_trials": n_trials,
                "seed": seed,
                "n_canonical_devices": analysis["n_canonical_devices"],
                "point_totals_kgco2e": analysis["point_totals_kgco2e"],
                "created_at": dt.datetime.now(dt.timezone.utc)
                .astimezone()
                .isoformat(),
            }
        ],
    )
    return analysis
