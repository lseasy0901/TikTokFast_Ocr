# -*- coding: utf-8 -*-
"""备注状态机（remark_status：UNSET/SKIPPED/SET）场景测试

需求六场景：
    1. 第一次进入弹窗
    2. 跳过后再次进入不弹
    3. 未备注历史进入会弹
    4. 已备注进入不弹
    5. 失败连接不触发
    6. 迁移旧历史兼容
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stream.history import (  # noqa: E402
    HistoryRepository, HistoryService,
    LiveRoomHistoryItem, RemarkStatus,
)

URL = "https://live.douyin.com/96670111738"


def make_service(tmp, name="h.json"):
    svc = HistoryService(HistoryRepository(Path(tmp.name) / name))
    fired = []
    svc.set_remark_callback(lambda url, room_id: fired.append((url, room_id)))
    return svc, fired


class TestRemarkStatusFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc, self.fired = make_service(self.tmp)

    def tearDown(self):
        self.tmp.cleanup()

    def test_1_first_visit_prompts(self):
        """第一次进入：UNSET → 弹窗"""
        self.svc.record_success(URL, "96670111738")
        item = self.svc.items()[0]
        self.assertEqual(self.fired, [(URL, "96670111738")])
        self.assertEqual(item.remark_status, RemarkStatus.UNSET.value)

    def test_2_skip_then_no_more_prompts(self):
        """跳过 → SKIPPED → 再次进入不弹（修复的核心场景）"""
        self.svc.record_success(URL, "96670111738")
        self.svc.mark_remark_skipped(URL)                 # 模拟用户跳过
        self.fired.clear()
        self.svc.record_success(URL, "96670111738")       # 从历史再次进入
        self.svc.record_success(URL, "96670111738")
        self.assertEqual(self.fired, [])
        self.assertEqual(self.svc.items()[0].remark_status,
                         RemarkStatus.SKIPPED.value)
        self.assertEqual(self.svc.items()[0].success_count, 3)

    def test_3_unset_history_prompts_again(self):
        """未备注历史（UNSET）进入会再次弹（修复的目标行为）"""
        self.svc.record_success(URL, "96670111738")       # 弹过但未处理
        self.fired.clear()
        self.svc.record_success(URL, "96670111738")       # 状态仍是 UNSET
        self.assertEqual(len(self.fired), 1)              # 会再次提示

    def test_4_set_note_no_more_prompts(self):
        """已备注 → SET → 再次进入不弹"""
        self.svc.record_success(URL, "96670111738")
        self.svc.set_note(URL, "今晚比赛直播")
        self.fired.clear()
        self.svc.record_success(URL, "96670111738")
        self.assertEqual(self.fired, [])
        self.assertEqual(self.svc.items()[0].remark_status, RemarkStatus.SET.value)

    def test_5_failed_connection_no_trigger(self):
        """失败连接：record_success 不会被调用 → 无记录、无弹窗"""
        self.svc.record_success("")
        self.assertEqual(self.fired, [])
        self.assertEqual(self.svc.items(), [])

    def test_6_legacy_migration_compat(self):
        """迁移旧历史：有备注 → SET 不弹；无备注 → UNSET 补一次询问"""
        path = Path(self.tmp.name) / "legacy.json"
        legacy = {"streams": [
            {"url": "https://live.douyin.com/111", "nickname": "老备注",
             "last_used": "2026-01-01T10:00:00"},
            {"url": "https://live.douyin.com/222", "nickname": "",
             "last_used": "2026-01-02T10:00:00"},
        ]}
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        svc = HistoryService(HistoryRepository(path))
        fired = []
        svc.set_remark_callback(lambda u, r: fired.append(u))

        svc.record_success("https://live.douyin.com/111")   # 有备注：不弹
        self.assertEqual(fired, [])

        svc.record_success("https://live.douyin.com/222")   # 无备注：补一次询问
        self.assertEqual(fired, ["https://live.douyin.com/222"])
        for it in svc.items():
            if it.url.endswith("222"):
                # 用户未在此测试中处理 → 保持 UNSET；真机上由弹窗出口固化
                self.assertEqual(it.remark_status, RemarkStatus.UNSET.value)


class TestRemarkStatusPersistence(unittest.TestCase):
    def test_status_survives_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "h.json"
            svc = HistoryService(HistoryRepository(path))
            svc.record_success("u1")
            svc.mark_remark_skipped("u1")
            svc.record_success("u2")
            svc.set_note("u2", "n2")

            svc2 = HistoryService(HistoryRepository(path))
            status = {it.url: it.remark_status for it in svc2.items()}
            self.assertEqual(status["u1"], RemarkStatus.SKIPPED.value)
            self.assertEqual(status["u2"], RemarkStatus.SET.value)
        finally:
            tmp.cleanup()

    def test_corrupt_status_falls_back_to_unset(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "h.json"
            item = LiveRoomHistoryItem(url="u1")
            data = {"version": 2, "items": [item.to_dict()]}
            data["items"][0]["remark_status"] = "garbage"
            path.write_text(json.dumps(data), encoding="utf-8")
            repo = HistoryRepository(path)
            self.assertEqual(repo.all()[0].remark_status,
                             RemarkStatus.UNSET.value)
        finally:
            tmp.cleanup()


class TestDialogOutcomes(unittest.TestCase):
    """HistoryButton.on_remark_request 的两个出口正确写回状态"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from gui.history_panel import HistoryButton
        self.svc, self.fired = make_service(self.tmp)
        self.svc.set_remark_callback(lambda u, r: None)   # 真实链路由按钮弹窗
        self.btn = HistoryButton(self.svc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_skip_writes_skipped(self):
        self.svc.record_success(URL, "123")
        from gui import history_panel
        orig_exec = history_panel.RemarkDialog.exec
        history_panel.RemarkDialog.exec = lambda self: 0   # QDialog.Rejected
        try:
            self.btn.on_remark_request(URL, "123")
        finally:
            history_panel.RemarkDialog.exec = orig_exec
        self.assertEqual(self.svc.items()[0].remark_status,
                         RemarkStatus.SKIPPED.value)

    def test_save_writes_set(self):
        self.svc.record_success(URL, "123")
        from gui import history_panel
        orig_exec = history_panel.RemarkDialog.exec
        orig_remark = history_panel.RemarkDialog.remark
        history_panel.RemarkDialog.exec = lambda self: 1    # QDialog.Accepted
        history_panel.RemarkDialog.remark = lambda self: "今晚比赛直播"
        try:
            self.btn.on_remark_request(URL, "123")
        finally:
            history_panel.RemarkDialog.exec = orig_exec
            history_panel.RemarkDialog.remark = orig_remark
        item = self.svc.items()[0]
        self.assertEqual(item.room_name, "今晚比赛直播")
        self.assertEqual(item.remark_status, RemarkStatus.SET.value)

    def test_save_blank_counts_as_skip(self):
        self.svc.record_success(URL, "123")
        from gui import history_panel
        orig_exec = history_panel.RemarkDialog.exec
        history_panel.RemarkDialog.exec = lambda self: 1    # Accepted 但空
        try:
            self.btn.on_remark_request(URL, "123")
        finally:
            history_panel.RemarkDialog.exec = orig_exec
        self.assertEqual(self.svc.items()[0].remark_status,
                         RemarkStatus.SKIPPED.value)


if __name__ == "__main__":
    unittest.main()
