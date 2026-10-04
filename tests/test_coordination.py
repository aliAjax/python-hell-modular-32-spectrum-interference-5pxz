import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.audit import verify_chain
from src.domain import ConflictError, DomainError
from src.repository import Repository
from src.service import Service


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, region="north", **overrides):
        payload = {
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-10",
            "region": region,
            "strength_dbm": -40,
            "detected_at": "2026-09-28T02:00:00+00:00",
            "reporter": "monitor-1",
        }
        payload.update(overrides)
        return self.service.create_item(payload, "analyst-1", "analyst")

    def _to_located(self, item):
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        return self.service.act(item["id"], "locate", {"location": "cell-1", "confidence": 0.9}, "field-1", "field_operator", item["version"])

    def test_claim_forms_single_owner_and_handover(self):
        item = self._create()
        item = self.service.act(item["id"], "claim", {}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["payload"]["owner"], "coord-1")
        self.assertEqual(item["owner"], "coord-1")
        item = self.service.act(item["id"], "claim", {}, "coord-2", "coordinator", item["version"], "north")
        self.assertEqual(item["payload"]["owner"], "coord-2")
        claims = [e for e in item["audit"] if e["event_type"] == "claim"]
        self.assertEqual(claims[-1]["payload"]["previous_owner"], "coord-1")

    def test_cross_region_duplicate_sees_conflict_and_owner(self):
        item = self._create(region="north")
        item = self.service.act(item["id"], "claim", {}, "coord-1", "coordinator", item["version"], "north")
        with self.assertRaises(ConflictError) as ctx:
            self._create(region="west")
        self.assertEqual(ctx.exception.code, "duplicate_item")
        self.assertEqual(ctx.exception.details["item_id"], item["id"])
        self.assertEqual(ctx.exception.details["owner"], "coord-1")
        self.assertEqual(ctx.exception.details["region"], "north")

    def test_disposition_requires_current_owner(self):
        item = self._to_located(self._create())
        item = self.service.act(item["id"], "claim", {}, "coord-1", "coordinator", item["version"], "north")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-1"}, "coord-2", "coordinator", item["version"], "north")
        self.assertEqual(ctx.exception.code, "not_owner")
        item = self.service.act(item["id"], "claim", {}, "coord-2", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-1"}, "coord-2", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "suspended")

    def test_measurement_update_stales_authorization_until_reconfirmed(self):
        item = self._to_located(self._create())
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-N1"}, "coord-1", "coordinator", item["version"], "north")
        old_level = item["payload"]["assessment"]["level"]
        item = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -90, "reason": "传感器漂移"}, "m", "monitor", item["version"])
        revision = item["payload"]["measurement_revisions"][-1]
        self.assertEqual(revision["old_assessment"]["level"], old_level)
        self.assertNotEqual(revision["new_assessment"]["level"], old_level)
        self.assertEqual(item["payload"]["assessment"]["level"], revision["new_assessment"]["level"])
        self.assertEqual(item["payload"]["suspend_authorization_status"], "pending_reconfirm")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "coordinate", {"coordination_agreement": "AG-1"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(ctx.exception.code, "authorization_stale")
        item = self.service.act(item["id"], "confirm_suspend", {}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["payload"]["suspend_authorization_status"], "confirmed")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AG-1"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "coordinating")

    def test_offline_sources_merge_dedupe_and_retry(self):
        item = self._create()
        batch = [
            {"source_type": "handheld", "external_id": "M-1", "observed_at": "2026-09-28T03:00:00+00:00", "strength_dbm": -50, "region": "north"},
            {"source_type": "handheld", "external_id": "M-2", "observed_at": "2026-09-28T03:05:00+00:00", "strength_dbm": -52, "region": "north"},
            {"source_type": "handheld", "external_id": "", "observed_at": "2026-09-28T03:10:00+00:00", "strength_dbm": -55},
        ]
        first = self.service.add_sources_batch(item["id"], batch, "field-1", "field_operator", "north")
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["results"][2]["status"], "error")
        retry = self.service.add_sources_batch(item["id"], batch, "field-1", "field_operator", "north")
        self.assertEqual(retry["created"], 0)
        self.assertEqual(retry["duplicates"], 2)
        self.assertEqual(retry["failed"], 1)
        self.assertEqual(len(self.repo.list_sources(item["id"])), 2)

    def test_migration_backfills_owner_and_keeps_audit(self):
        item = self._to_located(self._create())
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-9"}, "coord-9", "coordinator", item["version"], "north")
        untouched = self._create(station_id="ST-11", detected_at="2026-09-28T04:00:00+00:00")
        migrated = self.repo.migrate_owners()
        self.assertEqual(migrated, [{"item_id": item["id"], "owner": "coord-9"}])
        reloaded = self.service.get_item(item["id"])
        self.assertEqual(reloaded["payload"]["owner"], "coord-9")
        self.assertTrue(reloaded["payload"]["owner_backfilled"])
        event_types = [e["event_type"] for e in reloaded["audit"]]
        self.assertIn("created", event_types)
        self.assertIn("suspend", event_types)
        self.assertIn("owner_backfilled", event_types)
        self.assertTrue(verify_chain(reloaded["audit"]))
        self.assertIsNone(self.service.get_item(untouched["id"])["payload"].get("owner"))
        self.assertEqual(self.repo.migrate_owners(), [])


if __name__ == "__main__":
    unittest.main()
