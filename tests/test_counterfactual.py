"""反事实估计与留出检查测试：发展折拟合、holdout 不参与、矛盾可检出。"""
import unittest

from circular_claims.counterfactual import (
    CellEstimate,
    _binomial_tail_two_sided,
    estimate_cells,
)


def sub(sid, kind, region, intent, months, complete, fold):
    return {"id": sid, "kind": kind, "region": region, "would_buy_new": intent,
            "observed_service_months": months, "observation_complete": complete,
            "known_at": "2026-09-03T09:00:00+08:00", "fold": fold}


class CounterfactualTests(unittest.TestCase):
    def _rows(self, dev_intent, dev_months, hold_intent=None, hold_months=None):
        rows = []
        for i in range(20):
            rows.append(sub(f"Sd{i}", "aircon", "R1",
                            dev_intent(i), dev_months(i), True, "development"))
        if hold_intent is not None:
            for i in range(12):
                rows.append(sub(f"Sh{i}", "aircon", "R1",
                                hold_intent(i), hold_months(i), True, "holdout"))
        return rows

    def test_holdout_never_enters_estimates(self):
        rows = self._rows(
            dev_intent=lambda i: i < 15,          # 发展折 75% 替代
            dev_months=lambda i: 12,
            hold_intent=lambda i: i == 0,          # holdout 几乎全不替代
            hold_months=lambda i: 12,
        )
        cells = estimate_cells(rows)
        cell = cells[("aircon", "R1")]
        self.assertEqual(cell.n_known, 20)
        self.assertAlmostEqual(cell.substitution_rate, 15.5 / 21.0, places=4)
        # 点估计没有被 holdout 拉低
        self.assertGreater(cell.substitution_rate, 0.7)

    def test_holdout_contradiction_flagged(self):
        rows = self._rows(
            dev_intent=lambda i: True,             # 发展折 100% 替代
            dev_months=lambda i: 12,
            hold_intent=lambda i: False,           # holdout 0% 替代
            hold_months=lambda i: 12,
        )
        cells = estimate_cells(rows)
        cell = cells[("aircon", "R1")]
        rate_check = next(c for c in cell.holdout_checks if c["check"] == "substitution_rate")
        self.assertTrue(rate_check["contradicted"])
        self.assertEqual(cell.support, "contradicted")

    def test_life_contradiction_flagged_via_normal_approx(self):
        rows = self._rows(
            dev_intent=lambda i: True,
            dev_months=lambda i: 12,               # 发展折寿命稳定 12 月
            hold_intent=lambda i: True,
            hold_months=lambda i: 3,               # holdout 寿命显著更短
        )
        cells = estimate_cells(rows)
        cell = cells[("aircon", "R1")]
        life_check = next(c for c in cell.holdout_checks if c["check"] == "life_extension")
        self.assertTrue(life_check["contradicted"])
        self.assertEqual(cell.support, "contradicted")

    def test_unknown_intent_excluded_from_rate_denominator(self):
        rows = [
            sub("a", "fridge", "R0", True, 10, True, "development"),
            sub("b", "fridge", "R0", False, 10, True, "development"),
            sub("c", "fridge", "R0", None, 10, True, "development"),
        ]
        cells = estimate_cells(rows)
        cell = cells[("fridge", "R0")]
        self.assertEqual(cell.n_known, 2)
        self.assertEqual(cell.n_unknown_intent, 1)
        self.assertTrue(any("意向未知" in note for note in cell.limitations))

    def test_binomial_two_sided_bounds(self):
        self.assertAlmostEqual(_binomial_tail_two_sided(5, 10, 0.5), 1.0, places=6)
        self.assertLess(_binomial_tail_two_sided(0, 10, 0.9), 0.05)

    def test_insufficient_support_when_no_evidence(self):
        rows = [sub("a", "computer", "R0", None, 10, False, "development")]
        cells = estimate_cells(rows)
        self.assertEqual(cells[("computer", "R0")].support, "insufficient")
        self.assertIsInstance(CellEstimate(kind="x", region="y"), CellEstimate)


if __name__ == "__main__":
    unittest.main()
