"""声明权利与更正链测试：独占互斥、共同披露共存、退回撤回、原件保留。"""
import datetime as dt
import tempfile
import unittest
import pathlib

from circular_claims.claims import (
    DECISION_APPROVED,
    DECISION_DENIED,
    DECISION_PARTIAL,
    WITHDRAWAL,
    ReviewContext,
    active_claims,
    issue_claim,
    recheck_after_event,
    record_denial,
    review_all,
)
from circular_claims.storage import Store

AS_OF = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class FakeRegistry:
    def coverage_window(self, kind, region):
        return dt.date(2026, 1, 1), dt.date(2027, 1, 1)


class FakeEngine:
    registry = FakeRegistry()


def device_point(device_id, support="supported", active=True,
                 opened="2026-08-01", closed=None):
    return {
        "device_id": device_id,
        "kind": "aircon",
        "region": "R1",
        "active": active,
        "returned": closed == "returned",
        "cell_support": support,
        "episodes": [{
            "opened_on": opened,
            "closed_on": closed[0] if isinstance(closed, tuple) else None,
            "close_reason": closed[1] if isinstance(closed, tuple) else None,
            "observed_hours": 300,
            "observed_years": 0.15,
            "observed_through": "2026-09-01",
            "factor_status": "in_scope",
            "factor_reason": None,
            "factor_id": "F1",
            "factor_cover_to": "2027-01-01",
        }],
        "service": {"observed_service_years": 0.15,
                    "expected_life_years": 1.0, "substitution_rate": 0.8},
        "actual_burden_kgco2e": {"low": 5.0, "mid": 7.0, "high": 9.0,
                                 "by_kind": {}, "n_records": 1},
        "avoided_kgco2e": {"realized": 18.0, "projected": 100.0},
        "net_kgco2e": {"realized": 11.0, "projected": 100.0, "total": 111.0},
        "limitations": [],
    }


def make_ctx(devices):
    analysis = {
        "analysis_id": "ANTEST",
        "scenario": {"id": "central", "label": "中枢"},
        "devices": devices,
        "devices_mc": {d["device_id"]: {"net_total": {"p50": 100.0}} for d in devices},
    }
    return ReviewContext(FakeEngine(), analysis, AS_OF)


def app(aid, applicant, device, mode, known="2026-09-11T09:00:00+08:00",
        start="2026-09-01", end="2027-01-01"):
    return {"id": aid, "applicant": applicant, "device": device,
            "service_from": start, "service_to": end, "mode": mode,
            "known_at": known}


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "state")
        self.ctx = make_ctx([device_point("D1")])
        self.cmap = {"D1": "D1"}

    def tearDown(self):
        self.tmp.cleanup()

    def _issue_all(self, applications):
        reviews = review_all(self.store, applications, self.ctx, self.cmap)
        by_id = {a["id"]: a for a in applications}
        for review in reviews:
            if review["decision"] == DECISION_DENIED:
                record_denial(self.store, review, self.ctx)
            else:
                issue_claim(self.store, review, by_id[review["application_id"]], self.ctx)
        return {r["application_id"]: r for r in reviews}

    def test_two_exclusive_overlapping_only_first_survives(self):
        apps = [app("A1", "O1", "D1", "exclusive"),
                app("A2", "O2", "D1", "exclusive", known="2026-09-12T09:00:00+08:00")]
        decisions = self._issue_all(apps)
        self.assertNotEqual(decisions["A1"]["decision"], DECISION_DENIED)
        self.assertEqual(decisions["A2"]["decision"], DECISION_DENIED)
        self.assertIn("互斥", decisions["A2"]["reasons"][0])
        self.assertEqual(len(active_claims(self.store)), 1)

    def test_joint_claims_coexist_but_exclusive_blocks(self):
        apps = [app("A1", "O1", "D1", "joint"),
                app("A2", "O2", "D1", "joint", known="2026-09-12T09:00:00+08:00")]
        decisions = self._issue_all(apps)
        self.assertNotEqual(decisions["A2"]["decision"], DECISION_DENIED)
        self.assertEqual(len(active_claims(self.store)), 2)
        # 再来一份独占，必须被驳回
        decisions2 = self._issue_all(
            [app("A3", "O3", "D1", "exclusive", known="2026-09-13T09:00:00+08:00")])
        self.assertEqual(decisions2["A3"]["decision"], DECISION_DENIED)

    def test_factor_window_trim_to_approved_span(self):
        # 申请到 2027-09-01，但因子只覆盖到 2027-01-01
        decisions = self._issue_all([app("A1", "O1", "D1", "exclusive",
                                         end="2027-09-01")])
        self.assertEqual(decisions["A1"]["decision"], DECISION_PARTIAL)
        claim = active_claims(self.store)[0]
        self.assertEqual(claim["approved_spans"], [["2026-09-01", "2027-01-01"]])

    def test_returned_device_application_denied(self):
        # 退回发生在申请区间开始之前：区间内不存在任何服务片段
        ctx = make_ctx([device_point("D2", active=False,
                                     closed=("2026-08-20", "returned"))])
        reviews = review_all(self.store, [app("A9", "O1", "D2", "exclusive")],
                             ctx, {"D2": "D2"})
        self.assertEqual(reviews[0]["decision"], DECISION_DENIED)
        self.assertIn("没有有证据支持的服务片段", reviews[0]["reasons"][0])

    def test_recheck_withdraws_after_full_return_and_keeps_original(self):
        self._issue_all([app("A1", "O1", "D1", "exclusive")])
        claim = active_claims(self.store)[0]
        # 事件后设备退回，且无在用服务：服务片段在退回日关闭且观测为零
        point = device_point("D1", active=False,
                             closed=("2026-10-08", "returned"))
        point["episodes"][0]["observed_hours"] = 0
        point["episodes"][0]["observed_years"] = 0.0
        point["episodes"][0]["observed_through"] = None
        returned_ctx = make_ctx([point])
        event = {"id": "E1", "kind": "returned", "device": "D1",
                 "occurred_at": "2026-10-09T09:00:00+08:00", "detail": {}}
        # 声明区间从 2026-09-01 起，而退回片段 8 月售出、10-08 关闭但无观测，
        # 仍与声明区间相交 -> 复核按"无剩余可声明服务"撤回
        corrections = recheck_after_event(
            self.store, event, returned_ctx, {"D1": "D1"})
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["kind"], "withdrawal")
        # 原件仍在且字段未被修改
        original = self.store.find("claims", claim["id"])
        self.assertEqual(original["status"], "active")
        self.assertEqual(len(active_claims(self.store)), 0)

    def test_recheck_partial_correction_keeps_unaffected_span(self):
        self._issue_all([app("A1", "O1", "D1", "exclusive")])
        # 退回但已有 2026-09-01~2026-10-08 的观测服务
        ctx = make_ctx([device_point("D1", active=False,
                                     closed=("2026-10-08", "returned"))])
        event = {"id": "E2", "kind": "returned", "device": "D1",
                 "occurred_at": "2026-10-09T09:00:00+08:00", "detail": {}}
        corrections = recheck_after_event(self.store, event, ctx, {"D1": "D1"})
        self.assertEqual(corrections[0]["kind"], "correction")
        self.assertEqual(corrections[0]["remaining_spans"],
                         [["2026-09-01", "2026-10-08"]])
        self.assertEqual(len(active_claims(self.store)), 1)

    def test_claim_snapshot_locks_scenario_and_basis(self):
        self._issue_all([app("A1", "O1", "D1", "exclusive")])
        claim = active_claims(self.store)[0]
        self.assertEqual(claim["scenario_id"], "central")
        self.assertIn("mc", claim["basis_snapshot"])
        self.assertIn("net_kgco2e", claim["basis_snapshot"])

    def test_recheck_records_factor_revision_without_overwriting_original(self):
        self._issue_all([app("A1", "O1", "D1", "exclusive")])
        claim = active_claims(self.store)[0]
        original_factor = claim["basis_snapshot"]["episodes"][0]["factor_id"]
        # 复核时点：设备仍在用，但预计期因子换成修订版（已实现期因子保持历史版本）
        point = device_point("D1")
        point["episodes"][0]["projected_factor_id"] = "F1-REVISED"
        point["episodes"][0]["projected_factor_status"] = "in_scope"
        ctx = make_ctx([point])
        event = {"id": "E3", "kind": "factor_revision", "device": "D1",
                 "occurred_at": "2026-10-05T09:00:00+08:00", "detail": {}}
        corrections = recheck_after_event(self.store, event, ctx, {"D1": "D1"})
        self.assertEqual(len(corrections), 1)
        self.assertIn("factor_revision", corrections[0]["recomputed_basis"])
        rev = corrections[0]["recomputed_basis"]["factor_revision"]
        self.assertEqual(rev["realized_factor_ids_at_issuance"], [original_factor])
        self.assertEqual(rev["projected_factor_ids_at_recheck"], ["F1-REVISED"])
        # 原件快照仍是核发当时的因子 id
        untouched = self.store.find("claims", claim["id"])
        self.assertEqual(
            untouched["basis_snapshot"]["episodes"][0]["factor_id"], original_factor
        )


if __name__ == "__main__":
    unittest.main()
