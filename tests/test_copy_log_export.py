#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
房间码复制记录与 CSV 导出 —— 验证脚本

覆盖：
    A. CopyLog 本身：序号、空值不入账、同一房间码多次入账、毫秒时间、CSV 结构
    B. 写入时机：只有「确认 + 复制成功」才产生记录（未确认读数、复制失败都不入账）
    C. 每次成功复制恰好一条；同一房间码在不同时间再次复制分别记录
    D. 现有去重行为不变（剪贴板连续重复仍被拦、右栏历史仍是去重显示）
    E. GUI 导出：无记录提示、保存对话框、UTF-8 BOM、重复导出、导出后记录保留

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_copy_log_export.py`` 运行，不依赖 pytest。

关键隔离：APPDATA / LOCALAPPDATA 指向临时目录；不联网、不启动直播、
不写用户真实配置；模态对话框与文件对话框一律打桩。
"""

import csv
import os
import re
import sys
import tempfile
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-copylog-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-copylog-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ocr.clipboard import ClipboardManager  # noqa: E402
from ocr.copy_log import CSV_HEADER, CopyLog, format_copy_time  # noqa: E402
import gui.main_window as mw  # noqa: E402

app = QApplication.instance() or QApplication([])

# ======================================================================
# 测试脚手架
# ======================================================================
_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {message}")
        return True
    _FAILED += 1
    print(f"  [FAIL] {message}")
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def pump(rounds: int = 30, delay: float = 0.005) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


def read_csv(path: str):
    """按 UTF-8 BOM 解码并解析 CSV，返回 (原始字节, 行列表)。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8-sig")
    return raw, list(csv.reader(text.splitlines()))


# 毫秒时间格式：HH:MM:SS.mmm
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}$")


class _StubActivationDialog:
    """替身：记录被打开，exec() 不阻塞（模态在无头环境会永久挂起）。"""

    def __init__(self, manager, parent=None):
        pass

    def exec(self):
        return 0

    def result_data(self):
        return None


class _RecordingFileDialog:
    """替身 QFileDialog：记录调用次数，返回预设的保存路径。"""

    result = ""
    calls = []

    @classmethod
    def getSaveFileName(cls, parent, title, directory, filt):
        cls.calls.append({"title": title, "directory": directory, "filter": filt})
        return cls.result, filt


class _RecordingMessageBox:
    """替身 QMessageBox：记录弹窗（无头环境里 exec() 会永久阻塞）。"""

    infos = []
    warnings = []

    @classmethod
    def information(cls, parent, title, text):
        cls.infos.append((title, text))
        return 0

    @classmethod
    def warning(cls, parent, title, text):
        cls.warnings.append((title, text))
        return 0


class _FailingClipboard:
    """替身剪贴板：写入永远失败（模拟系统剪贴板不可用）。"""

    last_error = "clipboard unavailable"

    def copy(self, text: str) -> bool:
        return False


def build_window():
    real = mw.ActivationDialog
    mw.ActivationDialog = _StubActivationDialog
    try:
        window = mw.MainWindow()
        window.show()
    finally:
        mw.ActivationDialog = real
    pump(40)
    window._access_status.set_license_active(None)
    pump(20)
    return window


# ======================================================================
# A. CopyLog 本身
# ======================================================================
def test_copy_log_unit() -> None:
    section("A. CopyLog：序号 / 空值 / 重复 / 时间 / CSV 结构")
    log = CopyLog()
    check(not log and len(log) == 0, "初始为空（可布尔判断、len 为 0）")

    # A1. 空 / 纯空白不入账，也不占序号
    check(log.record("") is None, "空文本不入账")
    check(log.record("   ") is None, "纯空白不入账")
    check(log.record(None) is None, "None 不入账")
    check(len(log) == 0, "被拒绝的写入没有留下任何记录")

    # A2. 序号从 1 连续递增
    first = log.record("AABB1234")
    second = log.record("XYZA5678")
    check(first.index == 1 and second.index == 2, "序号从 1 开始连续递增")

    # A3. 同一房间码在不同时间再次成功复制 → 分别记录（不去重）
    third = log.record("AABB1234")
    check(len(log) == 3, "同一房间码再次复制产生新记录（流水不去重）")
    check(third.index == 3 and third.code == "AABB1234", "第 3 条就是那次重复复制")

    # A4. 毫秒时间格式
    stamp = datetime(2026, 9, 25, 12, 30, 1, 235000)
    check(format_copy_time(stamp) == "12:30:01.235", f"毫秒时间：{format_copy_time(stamp)}")
    check(format_copy_time(datetime(2026, 9, 25, 1, 2, 3, 40000)) == "01:02:03.040",
          "不足 3 位的毫秒补齐为 040（不是 .40）")
    injected = CopyLog().record("QWER9012", now=stamp)
    check(injected.time_text == "12:30:01.235", "记录沿用注入的时刻（可复现）")

    # A5. CSV 结构：表头 + 顺序 + 列序
    rows = list(csv.reader(log.to_csv_text().splitlines()))
    check(tuple(rows[0]) == tuple(CSV_HEADER), f"表头为 {list(CSV_HEADER)}")
    check([r[0] for r in rows[1:]] == ["1", "2", "3"], "序号列按复制先后顺序")
    check([r[2] for r in rows[1:]] == ["AABB1234", "XYZA5678", "AABB1234"],
          "房间码列按复制先后顺序（含重复）")
    check(all(TIME_RE.match(r[1]) for r in rows[1:]), "每一行时间都是 HH:MM:SS.mmm")

    # A6. 导出到文件：UTF-8 BOM + CRLF + 内容一致
    target = os.path.join(tempfile.mkdtemp(prefix="dlv-copylog-"), "记录.csv")
    count = log.export_csv(target)
    check(count == 3, "export_csv 返回写入的记录条数")
    raw, file_rows = read_csv(target)
    check(raw.startswith(b"\xef\xbb\xbf"), "CSV 带 UTF-8 BOM（Excel 中文兼容）")
    check(b"\r\n" in raw, "行结束符为 CRLF")
    check(file_rows == rows, "文件内容与内存渲染完全一致")

    # A7. 导出不清空记录，可重复导出
    log.export_csv(target)
    check(len(log) == 3, "导出后记录仍在（导出不清空）")
    again = os.path.join(os.path.dirname(target), "记录2.csv")
    log.export_csv(again)
    check(read_csv(again)[0] == raw, "重复导出得到相同内容")


# ======================================================================
# B/C/D. GUI 写入时机与既有机制不变
# ======================================================================
def test_recording_timing(window) -> None:
    section("B/C/D. 写入时机：确认 + 复制成功才记录")

    log = window._copy_log
    log.clear()
    window._clear_session_codes()

    # B1. OCR 读数（未经确认/未复制）不进入记录
    window._on_ocr_text_updated("AABB1234")
    pump(10)
    check(len(log) == 0, "仅 OCR 读到文本不产生记录")
    check(window._session_list.count() == 1 and not window._session_list.item(0).flags(),
          "右栏历史同样不因读数写入（既有行为不变）")

    # B2. 复制失败 → 不记录
    real_clipboard = window._clipboard_manager
    window._clipboard_manager = _FailingClipboard()
    try:
        window._on_copy_requested("AABB1234")
    finally:
        window._clipboard_manager = real_clipboard
    pump(10)
    check(len(log) == 0, "剪贴板写入失败时该房间码不进入记录")

    # C1. 一次成功复制 = 恰好一条记录
    window._on_copy_requested("AABB1234")
    pump(10)
    check(len(log) == 1 and log.records[0].code == "AABB1234",
          "复制成功后恰好产生一条记录")

    # C2. 同一房间码在不同时间再次成功复制 → 分别记录
    #     走真实 ClipboardManager：连续重复会被它拦下（此时不产生新记录）……
    window._on_copy_requested("AABB1234")
    pump(10)
    check(len(log) == 1, "剪贴板连续重复被拦下时不再追加记录（去重机制未被绕过）")
    check(real_clipboard.copy("AABB1234") is False, "ClipboardManager 连续重复过滤仍然生效")

    # ……但换成别的房间码再换回来，就是两次真实的复制事件
    window._on_copy_requested("XYZA5678")
    window._on_copy_requested("AABB1234")
    pump(10)
    codes = [r.code for r in log.records]
    check(codes == ["AABB1234", "XYZA5678", "AABB1234"],
          f"不同时间再次复制同一房间码各记一条：{codes}")

    # D. 既有显示历史仍是去重视图（不受流水影响）
    check(window._session_list.count() == 3, "右栏历史同样记下了这 3 次事件")
    window._add_session_code("AABB1234")
    pump(10)
    check(window._session_list.count() == 3,
          "右栏历史仍是去重显示：连续重复折叠为一行（既有行为不变）")


# ======================================================================
# E. 导出交互
# ======================================================================
def test_export_ui(window) -> None:
    section("E. 导出 CSV：无记录提示 / 保存 / 重复导出 / 记录保留")

    real_dialog, real_box = mw.QFileDialog, mw.QMessageBox
    mw.QFileDialog, mw.QMessageBox = _RecordingFileDialog, _RecordingMessageBox
    _RecordingFileDialog.calls = []
    _RecordingMessageBox.infos = []
    _RecordingMessageBox.warnings = []
    try:
        # E1. 无记录：只提示，不打开保存对话框
        window._copy_log.clear()
        window._on_export_csv_clicked()
        pump(10)
        check(len(_RecordingMessageBox.infos) == 1, "无记录时给出提示")
        check("还没有" in _RecordingMessageBox.infos[0][1]
              or "没有" in _RecordingMessageBox.infos[0][1],
              f"提示文案说明没有可导出的记录：{_RecordingMessageBox.infos[0][1]!r}")
        check(not _RecordingFileDialog.calls, "无记录时不打开保存对话框")

        # 造三条记录（两个房间码，其中 AABB1234 复制了两次）
        window._copy_log.record("AABB1234", now=datetime(2026, 9, 25, 12, 30, 1, 235000))
        window._copy_log.record("XYZA5678", now=datetime(2026, 9, 25, 12, 30, 2, 251000))
        window._copy_log.record("AABB1234", now=datetime(2026, 9, 25, 12, 30, 3, 218000))

        # E2. 选择保存位置后导出
        target = os.path.join(tempfile.mkdtemp(prefix="dlv-copylog-ui-"), "导出.csv")
        _RecordingFileDialog.result = target
        window._on_export_csv_clicked()
        pump(10)
        check(len(_RecordingFileDialog.calls) == 1, "点「导出 CSV」会打开保存对话框")
        check(os.path.isfile(target), "按用户选择的位置写出文件")

        raw, rows = read_csv(target)
        check(raw.startswith(b"\xef\xbb\xbf"), "导出的 CSV 带 UTF-8 BOM")
        check(tuple(rows[0]) == ("序号", "复制时间", "房间码"), f"表头：{rows[0]}")
        check(rows[1] == ["1", "12:30:01.235", "AABB1234"], f"第 1 行：{rows[1]}")
        check(rows[2] == ["2", "12:30:02.251", "XYZA5678"], f"第 2 行：{rows[2]}")
        check(rows[3] == ["3", "12:30:03.218", "AABB1234"], f"第 3 行：{rows[3]}")

        # E3. 导出后记录保留，可再次导出同样的内容
        check(len(window._copy_log) == 3, "导出不清空记录")
        second = os.path.join(os.path.dirname(target), "导出-第二次.csv")
        _RecordingFileDialog.result = second
        window._on_export_csv_clicked()
        pump(10)
        check(os.path.isfile(second) and open(second, "rb").read() == raw,
              "重复导出可用，且内容一致")

        # E4. 用户取消保存：记录原样保留，不报错
        _RecordingFileDialog.result = ""
        window._on_export_csv_clicked()
        pump(10)
        check(len(window._copy_log) == 3, "取消导出后记录不变")
        check(not _RecordingMessageBox.warnings, "取消导出不算错误，不弹失败提示")

        # E5. 写入失败（目录不存在）→ 提示失败，记录仍保留
        _RecordingFileDialog.result = os.path.join(
            tempfile.mkdtemp(prefix="dlv-copylog-bad-"), "没有这个目录", "x.csv")
        window._on_export_csv_clicked()
        pump(10)
        check(len(_RecordingMessageBox.warnings) == 1, "写入失败时给出失败提示")
        check(len(window._copy_log) == 3, "写入失败后记录仍完整保留")
    finally:
        mw.QFileDialog, mw.QMessageBox = real_dialog, real_box


# ======================================================================
# F. 界面接线
# ======================================================================
def test_button_wiring(window) -> None:
    section("F. 「导出 CSV」按钮挂在现有 OCR 面板上")

    btn = window._export_csv_btn
    check(btn.parent() is not None, "按钮有父控件（不会被 Python 提前回收）")
    check(window._ocr_panel.isAncestorOf(btn), "按钮位于现有 OCR 面板内，未新增独立界面")
    check(btn.isVisible() and btn.width() > 0 and btn.height() > 0,
          f"按钮可点击地渲染出来（{btn.width()}×{btn.height()}）")
    check("导出" in btn.text(), f"按钮文案清晰：{btn.text()!r}")
    check("每一次成功复制" in btn.toolTip(),
          "tooltip 说明导出的是每次成功复制的流水（与去重列表区分开）")

    # 点按钮 = 走 _on_export_csv_clicked：无记录时给提示而不是静默什么都不做
    real_dialog, real_box = mw.QFileDialog, mw.QMessageBox
    mw.QFileDialog, mw.QMessageBox = _RecordingFileDialog, _RecordingMessageBox
    _RecordingFileDialog.calls = []
    _RecordingMessageBox.infos = []
    try:
        window._copy_log.clear()
        btn.click()
        pump(10)
        check(len(_RecordingMessageBox.infos) == 1, "点击按钮即可触发导出流程（无记录时给出提示）")
    finally:
        mw.QFileDialog, mw.QMessageBox = real_dialog, real_box


def main() -> int:
    print("房间码复制记录与 CSV 导出 —— 定向验证")
    test_copy_log_unit()

    window = build_window()
    try:
        test_recording_timing(window)
        test_export_ui(window)
        test_button_wiring(window)
    finally:
        window.close()
        pump(10)

    print(f"\n结果：{_PASSED} 通过 / {_FAILED} 失败")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
