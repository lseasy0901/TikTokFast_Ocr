# -*- coding: utf-8 -*-
"""直播间历史（stream/history.py）：模型 / 仓库 / 服务"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stream.history import (
    HistoryRepository, HistoryService, LiveRoomHistoryItem, extract_room_id,
)


class TestModel(unittest.TestCase):
    def test_display_name_unnamed(self):
        item = LiveRoomHistoryItem(url="https://live.douyin.com/123")
        self.assertEqual(item.display_name(), "未命名直播间")

    def test_display_name_note(self):
        item = LiveRoomHistoryItem(url="u", room_name="今晚比赛直播")
        self.assertEqual(item.display_name(), "今晚比赛直播")

    def test_room_id_fallback_from_url(self):
        item = LiveRoomHistoryItem(url="https://live.douyin.com/96670111738")
        self.assertEqual(item.display_room_id(), "96670111738")

    def test_room_id_prefers_recorded(self):
        item = LiveRoomHistoryItem(url="https://live.douyin.com/123",
                                   room_id="456")
        self.assertEqual(item.display_room_id(), "456")

    def test_extract_room_id_bad_url(self):
        self.assertEqual(extract_room_id("not a url"), "not a url")
        self.assertEqual(extract_room_id(""), "")


class TestRepository(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "h.json"
            repo = HistoryRepository(path)
            repo.replace_all([LiveRoomHistoryItem(url="u1", room_name="n1")])
            repo2 = HistoryRepository(path)
            self.assertEqual(len(repo2.all()), 1)
            self.assertEqual(repo2.all()[0].room_name, "n1")
            self.assertTrue(repo2.all()[0].id)

    def test_migrate_legacy_format(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "h.json"
            legacy = {"streams": [
                {"url": "https://live.douyin.com/123",
                 "nickname": "无畏契约直播间",
                 "last_used": "2026-09-01T10:30:00"},
            ]}
            path.write_text(json.dumps(legacy, ensure_ascii=False),
                            encoding="utf-8")
            repo = HistoryRepository(path)
            items = repo.all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].room_name, "无畏契约直播间")
            self.assertEqual(items[0].room_id, "123")
            # 迁移即落盘为新格式
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 2)

    def test_corrupt_file_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "h.json"
            path.write_text("{not json", encoding="utf-8")
            repo = HistoryRepository(path)
            self.assertEqual(repo.all(), [])


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "h.json"
        self.svc = HistoryService(HistoryRepository(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_success_is_recorded(self):
        self.svc.record_success("https://live.douyin.com/1", "1")
        items = self.svc.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].room_id, "1")
        self.assertEqual(items[0].success_count, 1)

    def test_reenter_updates_count_and_order(self):
        import time
        self.svc.record_success("u1", "1")
        time.sleep(0.02)
        self.svc.record_success("u2", "2")
        self.svc.record_success("u1", "1")
        items = self.svc.items()
        self.assertEqual([it.url for it in items], ["u1", "u2"])
        self.assertEqual(items[0].success_count, 2)

    def test_set_note(self):
        self.svc.record_success("u1")
        self.assertTrue(self.svc.set_note("u1", "今晚比赛直播"))
        self.assertEqual(self.svc.get_note("u1"), "今晚比赛直播")
        self.svc.set_note("u1", "")
        self.assertEqual(self.svc.get_note("u1"), "")

    def test_note_unknown_url(self):
        self.assertFalse(self.svc.set_note("nope", "x"))

    def test_max_items_lru(self):
        for i in range(15):
            self.svc.record_success(f"u{i}")
        self.assertEqual(len(self.svc.items()), 10)
        self.assertEqual(self.svc.items()[0].url, "u14")   # 最近在前

    def test_record_success_empty_url(self):
        self.svc.record_success("")
        self.assertEqual(self.svc.items(), [])

    def test_clear(self):
        self.svc.record_success("u1")
        self.svc.clear()
        self.assertEqual(self.svc.items(), [])


if __name__ == "__main__":
    unittest.main()
