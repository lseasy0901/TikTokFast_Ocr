#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
稳定版诊断补强 —— 丢帧计数（G2）与 worker 异常入诊断（G4）验证

覆盖：
    A. frame_buffer 丢帧计数：只计数、不改变覆盖行为
    B. 诊断：worker 级异常写入诊断记录（备注列），列结构不变
    C. 诊断：最终丢帧统计写入诊断记录
    D. worker 接线：纯旁路，拿不到缓冲区 / getter 抛错时都不冒泡
    E. 正常路径行为与补强前一致（关闭诊断 = 零开销、零记录）

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_frame_drops_and_worker_errors.py`` 运行，不依赖 pytest。
不联网、不起 OCR 线程、不需要 tesseract。
"""

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-drops-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-drops-local-")

import numpy as np                                               # noqa: E402

from ocr.diagnostics import (                                    # noqa: E402
    CSV_HEADER,
    FRAME_DROP_PREFIX,
    WORKER_ERROR_PREFIX,
    OCRDiagnostics,
)
from stream.frame_buffer import LatestFrameBuffer                # noqa: E402

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
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def frame(value: int) -> np.ndarray:
    return np.full((6, 8, 3), value, dtype=np.uint8)


# ======================================================================
section("A. frame_buffer：丢帧只计数，不改变覆盖行为")
# ======================================================================
buf = LatestFrameBuffer()
check(buf.dropped_count == 0 and buf.update_count == 0, "初始计数为 0")

buf.update(frame(1))
check(buf.dropped_count == 0, "第一帧（没有旧帧可覆盖）不计丢帧")
check(buf.update_count == 1, "update_count 记到 1")

buf.update(frame(2))
check(buf.dropped_count == 1, "覆盖一帧**未被读过**的帧 -> 丢帧 +1")

check(int(buf.get()[0, 0, 0]) == 2, "get() 仍返回最新帧（单槽覆盖语义不变）")
check(buf.dropped_count == 1, "get() 本身不改变丢帧计数")

buf.update(frame(3))
check(buf.dropped_count == 1, "覆盖一帧**已被读过**的帧 -> 不记丢帧")

buf.update(frame(4))
check(buf.dropped_count == 2, "再覆盖未读帧 -> 丢帧 +1")

buf.get()
buf.get()
buf.get()
buf.update(frame(5))
check(buf.dropped_count == 2, "多次 get() 不产生额外丢帧（读过就是读过）")

check(buf.update(frame(6)) is None, "update() 返回值仍为 None")
check(int(buf.get()[0, 0, 0]) == 6, "始终留最新一帧")
check(buf.update_count == 6, "update_count 累计到 6")

# clear() 的语义：与既有行为一致（只重置 frame_id），统计值不重置
before_dropped = buf.dropped_count
buf.clear()
check(buf.frame_id == 0, "clear() 仍重置 frame_id（既有行为）")
check(buf.dropped_count == before_dropped, "clear() 不重置会话级丢帧统计")
check(buf.has_frame is False, "clear() 后缓冲为空")

buf.update(frame(7))
check(buf.dropped_count == before_dropped, "clear() 后的第一帧不算丢帧")

# 属性只读
try:
    buf.dropped_count = 0
    check(False, "dropped_count 应为只读属性")
except AttributeError:
    check(True, "dropped_count 是只读属性")

# 丢帧率可算（验收时看的比值）
check(buf.update_count > 0, "update_count 可作为丢帧率分母")


# ======================================================================
section("B. 诊断：worker 级异常写入诊断记录")
# ======================================================================
off = OCRDiagnostics(enabled=False)
off.note_worker_error("boom")
check(len(off.records) == 0, "诊断关闭时写 worker 异常是空操作")

diag = OCRDiagnostics(enabled=True)
diag.note_worker_error("引擎初始化失败: boom")
check(len(diag.records) == 1, "无执行上下文时，worker 异常自成一条记录")

text = diag.to_csv_text()
lines = [ln for ln in text.splitlines() if ln.strip()]
check(len(lines[0].split(",")) == len(CSV_HEADER) == 13,
      "CSV 列结构不变（仍为 13 列）")
check(len(lines[1].split(",")) == 13, "事件行的列数也是 13")
check(WORKER_ERROR_PREFIX in lines[1], "异常写在备注列，带 worker异常: 前缀")
check("boom" in lines[1], "异常原文被完整保留")

# 有执行上下文时挂到该执行，不额外起行
diag2 = OCRDiagnostics(enabled=True)
trace = diag2.begin(profile_id="delta_force_1v1")
diag2.note_worker_error("主循环异常")
diag2.finish(trace)
check(len(diag2.records) == 1, "有执行上下文时不为异常另起一行")
check(WORKER_ERROR_PREFIX in diag2.to_csv_text(), "异常仍写进该行的备注列")


# ======================================================================
section("C. 诊断：最终丢帧统计写入诊断记录")
# ======================================================================
diag3 = OCRDiagnostics(enabled=True)
diag3.note_frame_drops(1234, 5678)
line = [ln for ln in diag3.to_csv_text().splitlines() if ln.strip()][1]
check(FRAME_DROP_PREFIX in line, "丢帧统计写进备注列，带 丢帧: 前缀")
check("1234/5678" in line, "同时给出丢帧数与总帧数")
check("21.7%" in line, "并给出丢帧率")

diag4 = OCRDiagnostics(enabled=True)
diag4.note_frame_drops(0, 0)
line0 = [ln for ln in diag4.to_csv_text().splitlines() if ln.strip()][1]
check("0/0" in line0 and "0.0%" in line0, "总帧数为 0 时不除零（写作 0.0%）")

off2 = OCRDiagnostics(enabled=False)
off2.note_frame_drops(9, 9)
check(len(off2.records) == 0, "诊断关闭时写丢帧统计是空操作")


# ======================================================================
section("D. worker 接线：纯旁路，绝不冒泡")
# ======================================================================
from ocr.worker import OCRWorker                                 # noqa: E402


class _RoiStub:
    pass


# D1. 未接诊断时两个辅助方法都是安全空操作
w_none = OCRWorker(lambda: None, _RoiStub(), profile="delta_force_1v1")
check(w_none._diagnostics is None, "默认不接诊断记录器")
w_none._note_worker_error("x")          # 不应抛
w_none._note_frame_drops()              # 不应抛
check(True, "未接诊断时两个记录方法都是安全空操作")

# D2. 接了诊断但关闭时：不产生记录，也不抛
w_off = OCRWorker(lambda: None, _RoiStub(), profile="delta_force_1v1",
                  diagnostics=off)
w_off._note_worker_error("y")
w_off._note_frame_drops()
check(len(off.records) == 0, "诊断关闭时 worker 侧不产生任何记录")

# D3. 拿不到缓冲区 / 缓冲区没有计数属性 -> 静默跳过
w_nodef = OCRWorker(lambda: None, _RoiStub(), profile="delta_force_1v1",
                    diagnostics=OCRDiagnostics(enabled=True))
w_nodef._note_frame_drops()
check(True, "getter 返回 None 时静默跳过")

w_bad = OCRWorker(lambda: object(), _RoiStub(), profile="delta_force_1v1",
                  diagnostics=OCRDiagnostics(enabled=True))
w_bad._note_frame_drops()
check(True, "缓冲区没有 dropped_count 属性时静默跳过（不抛）")


def _boom():
    raise RuntimeError("getter 炸了")


w_boom = OCRWorker(_boom, _RoiStub(), profile="delta_force_1v1",
                   diagnostics=OCRDiagnostics(enabled=True))
w_boom._note_frame_drops()
w_boom._note_worker_error("z")
check(True, "getter 抛异常时诊断不冒泡（识别/退出路径不受影响）")

# D4. 接上真实缓冲区：最终统计能落进 CSV
live_buf = LatestFrameBuffer()
live_buf.update(frame(1))
live_buf.update(frame(2))       # 丢 1
live_buf.update(frame(3))       # 丢 1
diag5 = OCRDiagnostics(enabled=True)
w_live = OCRWorker(lambda: live_buf, _RoiStub(), profile="delta_force_1v1",
                   diagnostics=diag5)
w_live._note_frame_drops()
check(len(diag5.records) == 1, "会话结束时写出 1 条丢帧统计")
check("丢帧:2/3" in diag5.to_csv_text(),
      "统计值与 frame_buffer 的实际计数一致（2/3）")


# ======================================================================
section("E. 正常路径行为与补强前一致")
# ======================================================================
# 诊断关闭时，frame_buffer 的读写语义与从前逐字相同
plain = LatestFrameBuffer()
plain.update(frame(11))
plain.update(frame(12))
check(int(plain.get()[0, 0, 0]) == 12, "关闭诊断时仍是单槽留最新帧")
check(plain.get() is plain.get(), "get() 返回同一对象（不复制、不重建）")
check(plain.has_frame is True, "has_frame 语义不变")
plain.clear()
check(plain.has_frame is False and plain.frame_id == 0, "clear() 语义不变")

# 计数是 O(1) 的整数自增：不引入任何额外拷贝或锁竞争
check(plain.update_count == 2 and plain.dropped_count == 1,
      "计数只在 update() 内做整数自增，无额外分配")

print(f"\n{'=' * 70}")
print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
print("=" * 70)
sys.exit(1 if _FAILED else 0)
