import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "west",
            "strength_dbm": -45,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_claim_forms_unique_follow_up_and_later_sees_conflict(self):
        item = self.service.create_item(self.payload, "analyst-1", "analyst")
        # 协调员领取主办，形成唯一跟进人
        claimed = self.service.claim(item["id"], "coord-1", "coordinator", "west")
        self.assertEqual(claimed["payload"]["follow_up"]["actor"], "coord-1")
        self.assertEqual(claimed["payload"]["follow_up"]["role"], "coordinator")
        # 另一区协调员同时提交，后到者看到冲突和最新跟进人
        with self.assertRaises(ConflictError) as context:
            self.service.claim(item["id"], "coord-2", "coordinator", "east")
        self.assertEqual(context.exception.code, "already_claimed")
        self.assertEqual(context.exception.details["follow_up"]["actor"], "coord-1")
        # 非协调员不能领取
        with self.assertRaises(DomainError) as forbidden:
            self.service.claim(item["id"], "analyst-1", "analyst", "west")
        self.assertEqual(forbidden.exception.status, 403)

    def test_measurement_correction_recomputes_and_reverts_suspension(self):
        item = self.service.create_item(self.payload, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-W-1"}, "coord-1", "coordinator", item["version"], "west")
        self.assertEqual(item["status"], "suspended")
        # 测量更新：原评估失效重算，已批准停用授权需重新确认
        result = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -110, "reason": "drift"}, "analyst-1", "analyst", item["version"])
        self.assertEqual(result["payload"]["assessment"]["level"], "low")
        self.assertEqual(result["status"], "located")
        self.assertNotIn("suspend_authorization", result["payload"])
        self.assertTrue(result["audit"][-1]["payload"]["reverted"])
        # 重新确认停用授权
        reconfirmed = self.service.act(result["id"], "suspend", {"authorization_code": "REG-W-2"}, "coord-1", "coordinator", result["version"], "west")
        self.assertEqual(reconfirmed["status"], "suspended")
        self.assertEqual(reconfirmed["payload"]["suspend_authorization"], "REG-W-2")

    def test_sync_sources_merges_by_source_dedups_and_keeps_confirmed_on_failure(self):
        item = self.service.create_item(self.payload, "analyst-1", "analyst")
        measurements = [
            {"source_type": "sensor", "external_id": "S-1", "observed_at": "2026-09-27T10:00:00+00:00", "strength_dbm": -60, "region": "west"},
            {"source_type": "sensor", "external_id": "S-1", "observed_at": "2026-09-27T10:00:00+00:00", "strength_dbm": -60, "region": "west"},
            {"source_type": "sensor", "external_id": "S-1", "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -65, "region": "west"},
            {"source_type": "sensor", "external_id": "S-2", "observed_at": "2026-09-27T10:30:00+00:00", "strength_dbm": -58, "region": "east"},
        ]
        result = self.service.sync_sources(item["id"], {"measurements": measurements}, "field-1", "field_operator", "west")
        statuses = {entry["index"]: entry["status"] for entry in result["confirmed"]}
        self.assertEqual(statuses[0], "created")
        self.assertEqual(statuses[1], "duplicate")
        self.assertEqual(statuses[2], "merged")
        self.assertEqual(len(result["confirmed"]), 3)
        # 写入失败的 S-2 保留在待重试列表
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["pending"][0]["error"], "region_mismatch")
        # 已确认部分已落库：S-1 合并为一行
        sources = self.repo.list_sources(item["id"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["external_id"], "S-1")
        self.assertEqual(sources[0]["observed_at"], "2026-09-27T12:00:00+00:00")
        # 重试未完成项（不带区域，绕过区域校验）
        retry = self.service.sync_sources(item["id"], {"measurements": [measurements[3]]}, "field-1", "field_operator", None)
        self.assertEqual(len(retry["confirmed"]), 1)
        self.assertEqual(retry["confirmed"][0]["status"], "created")
        self.assertEqual(len(retry["pending"]), 0)
        sources = self.repo.list_sources(item["id"])
        self.assertEqual(len(sources), 2)

    def test_backfill_follow_up_from_latest_disposition_keeps_audit(self):
        item1 = self.service.create_item(self.payload, "analyst-1", "analyst")
        item1 = self.service.act(item1["id"], "assess", {}, "analyst-1", "analyst", item1["version"])
        item1 = self.service.act(item1["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item1["version"])
        item2 = self.service.create_item({**self.payload, "station_id": "ST-02"}, "monitor-1", "monitor")
        item2 = self.service.act(item2["id"], "assess", {}, "monitor-1", "monitor", item2["version"])
        item2 = self.service.act(item2["id"], "locate", {"location": "cell-3", "confidence": 0.8}, "field-2", "field_operator", item2["version"])
        item2 = self.service.act(item2["id"], "suspend", {"authorization_code": "REG-W-9"}, "coord-1", "coordinator", item2["version"], "west")
        # 迁移补出归属
        result = self.service.backfill_follow_up("migration", "coordinator")
        self.assertEqual(result["count"], 2)
        # item1 归属最近处置（locate 的 field-1），item2 归属 suspend 的 coord-1
        backfilled = {entry["item_id"]: entry["follow_up"]["actor"] for entry in result["backfilled"]}
        self.assertEqual(backfilled[item1["id"]], "field-1")
        self.assertEqual(backfilled[item2["id"]], "coord-1")
        # 历史审计仍可查，且追加了迁移事件
        audit1 = self.repo.audit_trail(item1["id"])
        types = [event["event_type"] for event in audit1]
        self.assertIn("created", types)
        self.assertIn("assess", types)
        self.assertIn("locate", types)
        self.assertIn("follow_up_backfilled", types)
        # 迁移幂等
        again = self.service.backfill_follow_up("migration", "coordinator")
        self.assertEqual(again["count"], 0)


if __name__ == "__main__":
    unittest.main()
