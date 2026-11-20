"""身份解析测试：空序列号不猜合并、序列号冲突拒绝、人工确认才折叠。"""
import tempfile
import unittest
import pathlib

from circular_claims.identity import IdentityError, IdentityResolver, propose_link
from circular_claims.storage import Store


def device(did, serial=None, same_as=None, kind="aircon", region="R0"):
    return {"id": did, "serial": serial, "kind": kind, "region": region,
            "model_age_years": 3, "confirmed_same_as": same_as}


class IdentityTests(unittest.TestCase):
    def test_null_serial_never_auto_merges(self):
        devices = [device("D1"), device("D2")]  # 同型号同地区，均无序列号
        resolver = IdentityResolver(devices, [])
        self.assertEqual(resolver.canonical("D1"), "D1")
        self.assertEqual(resolver.canonical("D2"), "D2")
        self.assertEqual(set(resolver.null_serial_unlinked()), {"D1", "D2"})

    def test_confirmed_link_folds_identity(self):
        devices = [device("D1", "SN1"), device("D2", None, same_as="D1")]
        resolver = IdentityResolver(devices, [])
        self.assertEqual(resolver.canonical("D2"), "D1")
        self.assertEqual(resolver.merged_only("D1"), ["D2"])

    def test_distinct_serials_cannot_merge(self):
        devices = [device("D1", "SN1"), device("D2", "SN2")]
        with self.assertRaises(IdentityError):
            IdentityResolver(devices, [
                {"id": "L1", "canonical_kept": "D1", "merged_id": "D2",
                 "reason": "guess", "evidence": "none"}])

    def test_duplicate_serial_without_confirmation_stays_split(self):
        devices = [device("D1", "SN9"), device("D2", "SN9")]
        resolver = IdentityResolver(devices, [])
        self.assertEqual(resolver.canonical("D1"), "D1")
        self.assertEqual(resolver.duplicated_serials(), {"SN9": ["D1", "D2"]})

    def test_cycle_rejected(self):
        devices = [device("D1", None), device("D2", None), device("D3", None)]
        with self.assertRaises(IdentityError):
            IdentityResolver(devices, [
                {"id": "L1", "canonical_kept": "D2", "merged_id": "D1"},
                {"id": "L2", "canonical_kept": "D3", "merged_id": "D2"},
                {"id": "L3", "canonical_kept": "D1", "merged_id": "D3"},
            ])

    def test_propose_link_requires_evidence_and_writes_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "state")
            store.append("records", [
                {"id": "r1", "batch_id": "B0", "table": "devices",
                 "row_id": "D1", "data": device("D1", "SN1")},
                {"id": "r2", "batch_id": "B0", "table": "devices",
                 "row_id": "D2", "data": device("D2", None)},
            ])
            with self.assertRaises(IdentityError):
                propose_link(store, "D1", "D2", reason="", evidence="")
            link = propose_link(store, "D1", "D2", "manual_check", "work_order #7")
            self.assertTrue(store.find("identity_links", link["id"]))
            self.assertEqual(store.read("events")[0]["kind"], "identity_merge")


if __name__ == "__main__":
    unittest.main()
