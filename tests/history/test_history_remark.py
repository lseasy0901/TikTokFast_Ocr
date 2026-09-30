# -*- coding: utf-8 -*-
"""首次进入直播间备注询问（需求四场景测试）

场景：
    1. 新直播间成功进入       -> 触发 remark_request
    2. 已存在备注 / 再次进入  -> 不触发
    3. 跳过                   -> 历史保存成功、remark 为空
    4. 失败连接               -> 不触发（record_success 不会被调用）

另覆盖 RemarkDialog 的保存/跳过两条出口。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stream.history import HistoryRepository, HistoryService  # noqa: E402
from gui.history_panel import RemarkDialog  # noqa: E402

URL = "https://live.douyin.com/96670111738"


class TestRemarkRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = HistoryService(
            HistoryRepository(Path(self.tmp.name) / "h.json"))
        self.requests = []          # (url, room_id)
        self.svc.set_remark_callback(
            lambda url, room_id: self.requests.append((url, room_id)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_1_first_visit_triggers_request(self):
        self.svc.record_success(URL, "96670111738")
        self.assertEqual(self.requests, [(URL, "96670111738")])

    def test_2_existing_note_no_request(self):
        self.svc.record_success(URL, "96670111738")
        self.svc.set_note(URL, "无畏契约直播间")
        self.requests.clear()
        self.svc.record_success(URL, "96670111738")     # 再次进入
        self.assertEqual(self.requests, [])

    def test_3_skip_still_saves_history(self):
        self.svc.record_success(URL, "96670111738")
        self.requests.clear()                            # 模拟用户跳过：不调 set_note
        items = self.svc.items()
        self.assertEqual(len(items), 1)                  # 历史已保存
        self.assertEqual(items[0].room_name, "")         # remark 为空
        self.assertEqual(items[0].success_count, 1)

    def test_4_failed_connection_no_request(self):
        """失败连接：main_window 只在 _handle_connect_success 调 record_success，
        失败路径到不了 service —— 这里验证"从未 record 的 URL"不产生记录也不触发。"""
        self.svc.record_success("")                      # 空URL（防御）
        self.assertEqual(self.requests, [])
        self.assertEqual(self.svc.items(), [])           # 失败的 URL 不在历史里
        self.svc.record_success("https://live.douyin.com/other", "other")
        self.assertEqual([u for u, _ in self.requests], ["https://live.douyin.com/other"])

    def test_callback_exception_does_not_break_record(self):
        def bad_cb(url, room_id):
            raise RuntimeError("boom")
        self.svc.set_remark_callback(bad_cb)
        self.svc.record_success(URL, "96670111738")      # 不应外抛
        self.assertEqual(len(self.svc.items()), 1)       # 记录仍在

    def test_no_callback_registered_is_noop(self):
        svc = HistoryService(HistoryRepository(Path(self.tmp.name) / "h2.json"))
        svc.record_success(URL, "96670111738")           # 未注册回调：静默
        self.assertEqual(len(svc.items()), 1)


class TestRemarkDialog(unittest.TestCase):
    def _dialog(self):
        return RemarkDialog("96670111738")

    def test_save_path(self):
        d = self._dialog()
        d._input.setText("今晚比赛直播")
        self.assertEqual(d.remark(), "今晚比赛直播")

    def test_skip_or_blank_is_empty(self):
        d = self._dialog()
        self.assertEqual(d.remark(), "")
        d._input.setText("   ")
        self.assertEqual(d.remark(), "")                 # 空白视同跳过


if __name__ == "__main__":
    unittest.main()
