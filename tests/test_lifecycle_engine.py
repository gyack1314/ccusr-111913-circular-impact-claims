"""生命周期与净效益引擎测试：服务年口径、退回截断、已实现/预计分账。"""
import datetime as dt
import unittest

from circular_claims.counterfactual import CellEstimate
from circular_claims.engine import SCENARIOS, Engine
from circular_claims.factors import FactorRegistry
from circular_claims.identity import IdentityResolver
from circular_claims.lifecycle import SERVICE_HOURS_PER_YEAR, build_timeline
from circular_claims.uncertainty import build_draws

AS_OF = dt.datetime(2026, 10, 1, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def movement(mid, kind, day, hours=0):
    return {"id": mid, "device": "D1", "kind": kind, "day": day,
            "known_at": day + "T09:00:00+08:00", "service_hours": hours,
            "holder": "O1"}


class LifecycleTests(unittest.TestCase):
    DEVICE = {"id": "D1", "serial": "SN1", "kind": "aircon", "region": "R1",
              "model_age_years": 3, "confirmed_same_as": None}

    def test_service_years_come_from_hours_not_sale(self):
        movs = [
            movement("m1", "received", "2026-06-01"),
            movement("m2", "refurbished", "2026-07-01"),
            movement("m3", "sold", "2026-08-01"),
            movement("m4", "in_use", "2026-09-01", hours=500),
        ]
        tl = build_timeline(self.DEVICE, movs, AS_OF)
        self.assertEqual(len(tl.episodes), 1)
        self.assertTrue(tl.episodes[0].is_active)
        self.assertEqual(tl.realized_service_years(), 500 / SERVICE_HOURS_PER_YEAR)

    def test_return_closes_episode_and_no_future_service(self):
        movs = [
            movement("m1", "sold", "2026-08-01"),
            movement("m2", "returned", "2026-09-01"),
        ]
        tl = build_timeline(self.DEVICE, movs, AS_OF)
        ep = tl.episodes[0]
        self.assertFalse(ep.is_active)
        self.assertEqual(ep.close_reason, "returned")
        self.assertEqual(tl.realized_service_years(), 0.0)
        self.assertTrue(tl.has_return())

    def test_in_use_without_sold_is_rejected(self):
        with self.assertRaises(ValueError):
            build_timeline(self.DEVICE, [movement("m1", "in_use", "2026-09-01", 10)], AS_OF)


def _factor():
    return {"id": "F1", "kind": "aircon", "region": "R1",
            "unit": "kgco2e_per_service_year", "low": 100.0, "high": 200.0,
            "correlation_group": "g1", "valid_from": "2026-01-01",
            "valid_to": "2027-01-01", "known_at": "2026-09-01T09:00:00+08:00"}


class EngineTests(unittest.TestCase):
    def _engine(self, device_rows, movement_rows, burden_rows, support="supported"):
        tables = {
            "devices": device_rows,
            "movements": movement_rows,
            "factors": [_factor()],
            "substitution": [],
            "burdens": burden_rows,
        }
        resolver = IdentityResolver(device_rows, [])
        cell = CellEstimate(kind="aircon", region="R1", n_intent=8, n_known=10,
                            substitution_rate=0.8, rate_alpha=8.5, rate_beta=2.5,
                            life_months=[12.0] * 16, life_mean_months=12.0,
                            life_sem_months=0.5, support=support)
        registry = FactorRegistry([_factor()], AS_OF)
        draws = build_draws([_factor()], {("aircon", "R1"): cell}, burden_rows,
                            n_trials=200, seed=1)
        return Engine(tables, AS_OF, resolver, {("aircon", "R1"): cell},
                      registry, draws)

    def test_realized_vs_projected_split_and_burden_retained(self):
        device = {"id": "D1", "serial": "SN1", "kind": "aircon", "region": "R1",
                  "model_age_years": 3, "confirmed_same_as": None}
        movs = [
            movement("m1", "sold", "2026-08-01"),
            {"id": "m2", "device": "D1", "kind": "in_use", "day": "2026-09-01",
             "known_at": "2026-09-02T09:00:00+08:00", "service_hours": 500,
             "holder": "O1"},
        ]
        burden = [{"id": "B1", "device": "D1", "kind": "repair_transport",
                   "kgco2e_low": 10.0, "kgco2e_high": 20.0,
                   "occurred_at": "2026-08-02T09:00:00+08:00",
                   "known_at": "2026-08-03T09:00:00+08:00"}]
        engine = self._engine([device], movs, burden)
        result = engine.compute_device_point("D1", SCENARIOS["central"])
        # 已观测 0.25 服务年 × 中点因子 150 × 替代率 0.8 = 30
        self.assertAlmostEqual(result["avoided_kgco2e"]["realized"], 30.0, places=6)
        # 已发生负担中点 15 必须保留
        self.assertEqual(result["actual_burden_kgco2e"]["mid"], 15.0)
        self.assertAlmostEqual(result["net_kgco2e"]["realized"], 15.0, places=6)
        # 预计项为正且与已实现分列
        self.assertGreater(result["avoided_kgco2e"]["projected"], 0.0)
        self.assertIn("projected", result["net_kgco2e"])
        self.assertAlmostEqual(
            result["net_kgco2e"]["total"],
            result["net_kgco2e"]["realized"] + result["net_kgco2e"]["projected"],
            places=6,
        )

    def test_returned_device_has_zero_avoided_and_negative_net(self):
        device = {"id": "D1", "serial": "SN1", "kind": "aircon", "region": "R1",
                  "model_age_years": 3, "confirmed_same_as": None}
        movs = [movement("m1", "sold", "2026-08-01"),
                movement("m2", "returned", "2026-09-01")]
        burden = [{"id": "B1", "device": "D1", "kind": "repair_transport",
                   "kgco2e_low": 10.0, "kgco2e_high": 10.0,
                   "occurred_at": "2026-08-02T09:00:00+08:00",
                   "known_at": "2026-08-03T09:00:00+08:00"}]
        engine = self._engine([device], movs, burden)
        result = engine.compute_device_point("D1", SCENARIOS["central"])
        self.assertEqual(result["avoided_kgco2e"]["realized"], 0.0)
        self.assertEqual(result["avoided_kgco2e"]["projected"], 0.0)
        self.assertEqual(result["net_kgco2e"]["total"], -10.0)

    def test_contradicted_cell_zeroes_projected_only(self):
        device = {"id": "D1", "serial": "SN1", "kind": "aircon", "region": "R1",
                  "model_age_years": 3, "confirmed_same_as": None}
        movs = [movement("m1", "sold", "2026-08-01"),
                {"id": "m2", "device": "D1", "kind": "in_use", "day": "2026-09-01",
                 "known_at": "2026-09-02T09:00:00+08:00", "service_hours": 400,
                 "holder": "O1"}]
        engine = self._engine([device], movs, [], support="contradicted")
        result = engine.compute_device_point("D1", SCENARIOS["central"])
        self.assertEqual(result["avoided_kgco2e"]["projected"], 0.0)
        self.assertGreater(result["avoided_kgco2e"]["realized"], 0.0)

    def test_projected_service_capped_by_factor_coverage(self):
        device = {"id": "D1", "serial": "SN1", "kind": "aircon", "region": "R1",
                  "model_age_years": 3, "confirmed_same_as": None}
        # 观测日 2026-12-15，因子 2027-01-01 到期：预计覆盖上限仅约 17 天
        movs = [movement("m1", "sold", "2026-11-15"),
                {"id": "m2", "device": "D1", "kind": "in_use", "day": "2026-12-15",
                 "known_at": "2026-12-16T09:00:00+08:00", "service_hours": 100,
                 "holder": "O1"}]
        engine = self._engine([device], movs, [])
        result = engine.compute_device_point("D1", SCENARIOS["central"])
        # 延寿 1 年但因子只再覆盖 ~17 天，预计量必须很小
        self.assertLess(result["avoided_kgco2e"]["projected"], 8.0)


if __name__ == "__main__":
    unittest.main()
