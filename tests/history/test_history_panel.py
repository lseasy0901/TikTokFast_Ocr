# -*- coding: utf-8 -*-
"""HistoryPopup 生命周期：打开 → 关闭 → 重开，连续 20 次无 RuntimeError"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from gui.history_panel import HistoryButton, HistoryPopup   # noqa: E402
from stream.history import HistoryService, HistoryRepository  # noqa: E402


class TestPopupLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile_dir = __import__("tempfile").TemporaryDirectory()
        from pathlib import Path
        self.svc = HistoryService(
            HistoryRepository(Path(self.tmp.name) / "h.json"))
        self.svc.record_success("https://live.douyin.com/123", "123")
        self.entered = []
        self.btn = HistoryButton(self.svc)
        self.btn.enterRequested.connect(self.entered.append)

    def tearDown(self):
        if self.btn._popup is not None:
            self.btn._popup.close()
        self.tmp.cleanup()

    def _open_and_close_by_close(self):
        """打开 → 显式 close（模拟外部点击关闭/再次点击 toggle）"""
        self.btn.click()
        popup = self.btn._popup
        self.assertIsNotNone(popup)
        self.assertTrue(popup.isVisible())
        popup.close()
        # WA_DeleteOnClose：close 后 C++ 对象销毁，destroyed 信号同步清引用
        QApplication.processEvents()
        self.assertIsNone(self.btn._popup)

    def test_open_close_20_cycles_no_runtime_error(self):
        """打开→关闭→再打开 ×20：不得出现 Internal C++ object already deleted"""
        for i in range(20):
            self._open_and_close_by_close()

    def test_toggle_via_button_20_cycles(self):
        """按钮 toggle 语义（开→点按钮关→再点开）×20"""
        for _ in range(20):
            self.btn.click()                    # 开
            self.assertIsNotNone(self.btn._popup)
            self.assertTrue(self.btn._popup.isVisible())
            self.btn.click()                    # 关（toggle 分支 close()）
            QApplication.processEvents()
            self.assertIsNone(self.btn._popup)

    def test_enter_closes_and_allows_reopen(self):
        """[进入] 关闭弹窗后可再次打开，且信号正常转发"""
        for _ in range(5):
            self.btn.click()
            self.btn._popup._on_enter("https://live.douyin.com/123")
            QApplication.processEvents()
            self.assertIsNone(self.btn._popup)
        self.assertEqual(len(self.entered), 5)


if __name__ == "__main__":
    unittest.main()
