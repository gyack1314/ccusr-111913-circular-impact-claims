"""不确定性测试：同源相关性必须保留，朴素独立束区间虚窄。"""
import unittest

from circular_claims.counterfactual import CellEstimate
from circular_claims.uncertainty import build_draws, quantile


def factor(fid, group):
    return {"id": fid, "kind": "aircon", "region": "R1",
            "unit": "kgco2e_per_service_year", "low": 100.0, "high": 200.0,
            "correlation_group": group, "valid_from": "2026-01-01",
            "valid_to": "2027-01-01", "known_at": "2026-09-01T09:00:00+08:00"}


class UncertaintyTests(unittest.TestCase):
    def test_correlated_group_shares_draw(self):
        factors = [factor("F1", "shared"), factor("F2", "shared")]
        cell = CellEstimate(kind="aircon", region="R1")
        bundle = build_draws(factors, {("aircon", "R1"): cell}, [],
                             n_trials=500, seed=42, correlated=True)
        for t in range(500):
            self.assertEqual(
                bundle.factor_draws["F1"][t], bundle.factor_draws["F2"][t]
            )

    def test_naive_independent_draws_diverge(self):
        factors = [factor("F1", "shared"), factor("F2", "shared")]
        cell = CellEstimate(kind="aircon", region="R1")
        bundle = build_draws(factors, {("aircon", "R1"): cell}, [],
                             n_trials=500, seed=42, correlated=False)
        self.assertNotEqual(
            bundle.factor_draws["F1"][:50], bundle.factor_draws["F2"][:50]
        )

    def test_correlation_widens_aggregate_interval(self):
        # 20 个设备共享同一来源组：相关求和的离散度应大于独立求和
        factors = [factor(f"F{i}", "manufacturing-aircon") for i in range(20)]
        cell = CellEstimate(kind="aircon", region="R1", n_intent=8, n_known=10,
                            rate_alpha=8.5, rate_beta=2.5,
                            life_months=[12.0] * 16, life_mean_months=12.0)
        cells = {("aircon", "R1"): cell}
        corr = build_draws(factors, cells, [], n_trials=2000, seed=7, correlated=True)
        naive = build_draws(factors, cells, [], n_trials=2000, seed=7, correlated=False)

        def total_width(bundle):
            totals = [
                sum(bundle.factor_draws[f"F{i}"][t] for i in range(20))
                for t in range(2000)
            ]
            return quantile(totals, 0.95) - quantile(totals, 0.05)

        self.assertGreater(total_width(corr), total_width(naive))

    def test_deterministic_seed(self):
        factors = [factor("F1", "g")]
        cell = CellEstimate(kind="aircon", region="R1")
        a = build_draws(factors, {("aircon", "R1"): cell}, [], 300, seed=99)
        b = build_draws(factors, {("aircon", "R1"): cell}, [], 300, seed=99)
        self.assertEqual(a.factor_draws["F1"], b.factor_draws["F1"])


if __name__ == "__main__":
    unittest.main()
