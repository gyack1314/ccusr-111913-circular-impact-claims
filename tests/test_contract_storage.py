"""契约与存储测试：摘要、字段、跨批次引用与追加式账本纪律。"""
import json
import pathlib
import shutil
import tempfile
import unittest

from circular_claims import contract
from circular_claims.storage import Store

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_base_dataset_loads_and_full_validates(self):
        manifest, tables = contract.load_batch(ROOT / "data")
        self.assertEqual(set(tables), set(contract.TABLE_NAMES))
        contract.validate_full_dataset(tables)  # 完整资料含引用校验
        self.assertEqual(len(tables["devices"]), manifest["tables"][0]["rows"])

    def test_digest_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            shutil.copytree(ROOT / "data", d / "data")
            target = d / "data" / "devices.jsonl"
            with target.open("ab") as f:
                f.write(b"\n")
            with self.assertRaises(contract.ContractError):
                contract.load_batch(d / "data")

    def test_field_mismatch_rejected(self):
        row = {"id": "X", "serial": None, "kind": "aircon", "region": "R0",
               "model_age_years": 1}  # 缺 confirmed_same_as
        with self.assertRaises(contract.ContractError):
            contract.validate_tables({"devices": [row]}, check_refs=False)

    def test_unknown_enum_rejected(self):
        good = {"id": "M1", "device": "D1", "kind": "sold_away", "day": "2026-09-01",
                "known_at": "2026-09-02T09:00:00+08:00", "service_hours": 0,
                "holder": "O1"}
        with self.assertRaises(contract.ContractError):
            contract.validate_tables({"movements": [good]}, check_refs=False)

    def test_null_is_not_zero_and_unknown_intent_allowed(self):
        row = {"id": "S1", "kind": "aircon", "region": "R0", "would_buy_new": None,
               "observed_service_months": 3, "observation_complete": True,
               "known_at": "2026-09-02T09:00:00+08:00", "fold": "development"}
        contract.validate_tables({"substitution": [row]}, check_refs=False)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_is_append_only_and_blocks_duplicate_dir(self):
        self.store.import_batch(ROOT / "data")
        with self.assertRaises(ValueError):
            self.store.import_batch(ROOT / "data")
        batches = self.store.read("batches")
        self.assertEqual(len(batches), 1)
        records = self.store.read("records")
        self.assertEqual(sum(1 for r in records if r["table"] == "devices"), 40)

    def test_cross_batch_reference_checked_on_ledger(self):
        bad_dir = pathlib.Path(self.tmp.name) / "bad"
        bad_dir.mkdir()
        (bad_dir / "movements.jsonl").write_text(
            json.dumps({"id": "MX", "device": "D999", "kind": "received",
                        "day": "2026-09-01", "known_at": "2026-09-02T09:00:00+08:00",
                        "service_hours": 0, "holder": "O1"}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(contract.ContractError):
            self.store.import_batch(bad_dir)
        self.assertEqual(self.store.read("records"), [])

    def test_duplicate_row_id_rejected(self):
        self.store.import_batch(ROOT / "data")
        dup_dir = pathlib.Path(self.tmp.name) / "dup"
        dup_dir.mkdir()
        (dup_dir / "burdens.jsonl").write_text(
            json.dumps({"id": "B0", "device": "D001", "kind": "repair_transport",
                        "kgco2e_low": 1, "kgco2e_high": 2,
                        "occurred_at": "2026-09-02T09:00:00+08:00",
                        "known_at": "2026-09-03T09:00:00+08:00"}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.store.import_batch(dup_dir)


if __name__ == "__main__":
    unittest.main()
