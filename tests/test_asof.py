"""as-of 获知时间纪律测试：业务发生时间不等于资料可得时间。"""
import datetime as dt
import unittest

from circular_claims.factors import FactorRegistry
from circular_claims.timeutil import latest_known_at, parse_ts, visible

TZ = dt.timezone(dt.timedelta(hours=8))


class AsOfTests(unittest.TestCase):
    def test_visible_respects_known_at_not_occurrence_time(self):
        row = {"day": "2026-08-01", "known_at": "2026-09-10T09:00:00+08:00"}
        # 业务 8 月发生，但 9 月才获知：8 月底视角不可见
        self.assertFalse(visible(row, dt.datetime(2026, 8, 31, tzinfo=TZ)))
        self.assertTrue(visible(row, dt.datetime(2026, 9, 11, tzinfo=TZ)))

    def test_latest_known_at_takes_max_across_tables(self):
        tables = {
            "a": [{"known_at": "2026-09-01T09:00:00+08:00"}],
            "b": [{"known_at": "2026-10-01T09:00:00+08:00"}],
        }
        self.assertEqual(
            latest_known_at(tables),
            parse_ts("2026-10-01T09:00:00+08:00"),
        )

    def test_factor_unknown_before_known_at(self):
        factor = {"id": "F1", "kind": "k", "region": "R",
                  "unit": "kgco2e_per_service_year", "low": 1, "high": 2,
                  "correlation_group": "g", "valid_from": "2026-01-01",
                  "valid_to": "2027-01-01",
                  "known_at": "2026-09-01T09:00:00+08:00"}
        early = FactorRegistry([factor], dt.datetime(2026, 8, 1, tzinfo=TZ))
        self.assertEqual(early.rows, [])
        late = FactorRegistry([factor], dt.datetime(2026, 9, 2, tzinfo=TZ))
        self.assertEqual(len(late.rows), 1)


if __name__ == "__main__":
    unittest.main()
