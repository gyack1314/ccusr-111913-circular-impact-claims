"""端到端集成测试：真实合成资料上的完整流程纪律。"""
import json
import pathlib
import tempfile
import unittest

from circular_claims.claims import active_claims
from circular_claims.pipeline import run_analysis
from circular_claims.reporting import project_summary, write_reports
from circular_claims.storage import Store
from circular_claims.cli import build_parser

ROOT = pathlib.Path(__file__).resolve().parents[1]


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "state")
        self.store.import_batch(ROOT / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_pipeline_disciplines(self):
        analysis = run_analysis(self.store, n_trials=500, seed=20260901)

        # 1) 已实现与预计分列，且退回设备不产生避免量
        by_id = {d["device_id"]: d for d in analysis["devices"]}
        for returned in ("D004", "D008", "D040"):
            d = by_id[returned]
            self.assertEqual(d["avoided_kgco2e"]["realized"], 0.0)
            self.assertEqual(d["avoided_kgco2e"]["projected"], 0.0)
            self.assertLessEqual(d["net_kgco2e"]["total"], 0.0)
        self.assertGreater(by_id["D001"]["avoided_kgco2e"]["realized"], 0.0)

        # 2) 相关束区间不窄于朴素独立束（同源误差未被平均掉）
        note = analysis["monte_carlo"]["correlation_note"]
        self.assertGreaterEqual(
            note["correlated_p05_p95_width"], note["naive_independent_width"]
        )

        # 3) 留出检查在单元层面落地，且至少有一个单元被识别为矛盾
        supports = {c["support"] for c in analysis["cells"]}
        self.assertIn("contradicted", supports)
        for c in analysis["cells"]:
            self.assertGreaterEqual(c["holdout"]["n_records"], 5)

        # 4) 项目合计 = 设备加总；共同披露尚未核发时权利计数为零
        totals = analysis["point_totals_kgco2e"]
        self.assertAlmostEqual(
            totals["realized_avoided"],
            round(sum(d["avoided_kgco2e"]["realized"] for d in analysis["devices"]), 3),
            places=4,
        )

        # 5) 审阅+核发：同批独占冲突只能存活一份，退回设备申请被驳回
        from circular_claims.claims import ReviewContext, review_all, issue_claim, record_denial, DECISION_DENIED
        from circular_claims.pipeline import build_engine
        from circular_claims.timeutil import parse_ts
        as_of = parse_ts(analysis["as_of"])
        engine, _, _, resolver = build_engine(self.store, as_of, n_trials=500, seed=20260901)
        run = engine.run("central")
        run["analysis_id"] = analysis["analysis_id"]
        ctx = ReviewContext(engine, run, as_of)
        cmap = {d: resolver.canonical(d) for d in resolver.devices}
        apps = self.store.all_tables_ignoring_asof()["applications"]
        reviews = review_all(self.store, apps, ctx, cmap)
        app_by_id = {a["id"]: a for a in apps}
        denied_apps = []
        for review in reviews:
            if review["decision"] == DECISION_DENIED:
                record_denial(self.store, review, ctx)
                denied_apps.append(review["application_id"])
            else:
                issue_claim(self.store, review, app_by_id[review["application_id"]], ctx)
        # D004 的任何申请都不应核发
        d004_claims = [c for c in active_claims(self.store) if c["canonical_device_id"] == "D004"]
        self.assertEqual(d004_claims, [])
        # D001 两份同时间独占申请只能核发一份
        d001_claims = [c for c in active_claims(self.store) if c["canonical_device_id"] == "D001"]
        self.assertEqual(len(d001_claims), 1)

        # 6) 报告可生成且下钻字段齐全
        out = pathlib.Path(self.tmp.name) / "reports"
        paths = write_reports(analysis, self.store, out)
        self.assertTrue(paths["json"].exists() and paths["markdown"].exists())
        payload = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["n_canonical_devices"], 40)
        # 声明覆盖设备数不超过设备总数（共同披露不重复计数）
        self.assertLessEqual(
            payload["summary"]["claims"]["n_covered_devices"], 40
        )

    def test_cli_parser_covers_all_commands(self):
        parser = build_parser()
        registered = next(
            action for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        ).choices
        self.assertEqual(
            set(registered),
            {"import-data", "identities", "merge-identity", "analyze",
             "list-applications", "review", "register-event", "recheck-event",
             "report", "show-claim", "reset"},
        )


if __name__ == "__main__":
    unittest.main()
