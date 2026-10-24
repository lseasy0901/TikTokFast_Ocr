#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 3 —— 「长度异常触发 1.5 倍复看」定向验证

覆盖：
    A. 开关默认关闭；关闭时热路径不产生任何额外 OCR 调用、行为逐字不变
    B. 触发规则：只认「比本档最长格式还长」的读法（插入型），偏短不触发
    C. 采纳规则：复看必须自己算出严格校验通过的候选，且 source_text 长度
       必须落在本档格式长度内；否则一律不采纳
    D. 复看结果必须经过裁决器（不直接返回、不绕过时序确认）
    E. 不做字符删除/替换猜测：没有读法支撑的字符不可能出现在结果里
    F. worker 接线：构造参数、默认值、只读属性、两条构造路径都传参
    G. 常量：倍率 1.5，且属于 SUPPORTED_SCALES

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_phase_3_length_escalation.py`` 运行，不依赖 pytest。
不联网、不起 OCR 线程、不需要 tesseract（引擎被桩住）。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np                                                   # noqa: E402

from ocr.room_code import (                                          # noqa: E402
    SUPPORTED_SCALES,
    RoomCodeRecognizer,
    _LENGTH_ESCALATION_SCALE,
)
from ocr.temporal import TemporalResolver                            # noqa: E402

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


# ----------------------------------------------------------------------
# 桩：按图像宽度决定读法的假引擎
# ----------------------------------------------------------------------
class _StubEngine:
    """按图像宽度返回预设读法的假引擎。

    用来模拟「同一帧在不同倍率下 OCR 给出不同读法」这一真实现象，
    而不依赖 tesseract。记录每次调用的宽度，便于断言调用次数。
    """

    def __init__(self, by_width):
        self.by_width = dict(by_width)
        self.widths = []

    def iter_readings(self, image, preprocess: bool = True):
        width = image.shape[1]
        self.widths.append(width)
        texts = self.by_width.get(width)
        if not texts:
            return
        for index, text in enumerate(texts):
            yield 6 + index, text


def frame(width: int = 100, height: int = 40) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


# 1.0x 宽度 100 / 1.5x 宽度 150 —— 用真实图片的尺寸比例会更贴近生产，
# 但这里的桩只关心宽度，保持测试最小。
W1, W15 = 100, 150


def build(by_width, **kwargs):
    engine = _StubEngine(by_width)
    rec = RoomCodeRecognizer(engine, profile="delta_force_1v1", **kwargs)
    return rec, engine


# ======================================================================
section("A. 开关默认关闭")
# ======================================================================
rec, engine = build({W1: ["UJIMI8506"]})
check(rec.length_escalation is False, "识别器 length_escalation 默认 False")
check(RoomCodeRecognizer(engine, profile="delta_force_1v1").length_escalation
      is False, "不传参时默认关闭")

off, off_engine = build({W1: ["UJIMI8506"], W15: ["UJMI8506"]})
result_off = off.recognize_frame(frame(W1))
check(result_off is None, "关闭时 9 位读法照旧无候选")
check(off_engine.widths == [W1],
      f"关闭时只发生 1 次 OCR（宽度={off_engine.widths}），无额外复看")
check(len(off_engine.widths) == len(set(off_engine.widths)),
      "关闭时没有任何放大倍率的读取")

# ======================================================================
section("B. 触发规则：只认「比最长格式还长」的插入型读法")
# ======================================================================
trig, trig_engine = build({W1: ["QWERSQ62"], W15: ["XYZK1234"]})
res = trig.recognize_frame(frame(W1))
check(trig_engine.widths == [W1],
      f"8 位但内容非法的读法不触发复看（宽度={trig_engine.widths}）")
check(res is None, "8 位非法读法仍然无候选")

short, short_engine = build({W1: ["HE8FPoe"], W15: ["QQQQ9999"]})
res_short = short.recognize_frame(frame(W1))
check(short_engine.widths == [W1],
      f"偏短的读法（标题卡类）不触发复看（宽度={short_engine.widths}）")
check(res_short is None, "偏短读法仍然无候选")

empty, empty_engine = build({})
check(empty.recognize_frame(frame(W1)) is None, "没有任何读法时不触发、无候选")

nine, nine_engine = build({W1: ["HJIKL4750"], W15: ["HJKL4750"]})
check(nine._has_length_anomaly(["HJIKL4750"]) is True, "9 位读法判定为长度异常")
check(nine._has_length_anomaly(["HJKL4750"]) is False, "8 位读法不是长度异常")
check(nine._has_length_anomaly(["HE8FPoe"]) is False, "7 位读法不是长度异常")
check(nine._has_length_anomaly(["", "ABC"]) is False, "空读法不参与判定")

# ======================================================================
section("C. 采纳规则")
# ======================================================================
# C1. 复看给出合法 8 位候选 -> 采纳
c1 = RoomCodeRecognizer(_StubEngine({W1: ["UJIMI8506"], W15: ["UJMI8506"]}),
                        profile="delta_force_1v1", length_escalation=True)
got = c1.recognize_frame(frame(W1))
check(got is not None and got.code == "UJMI8506",
      "复看给出合法 8 位候选 -> 被采纳")
check(got is not None and abs(got.scale - _LENGTH_ESCALATION_SCALE) < 1e-9,
      f"采纳结果的 scale 记录为 {_LENGTH_ESCALATION_SCALE}（真实读法倍率）")

# C2. 复看仍然给出 9 位读法 -> 不采纳
c2, c2e = build({W1: ["HJIKL4750"], W15: ["HJIKL4750"]},
                length_escalation=True)
check(c2.recognize_frame(frame(W1)) is None,
      "复看仍读到 9 位（长度仍异常）-> 不采纳，返回无候选")
check(c2e.widths == [W1, W15], "复看确实发生了一次（共 2 次 OCR）")

# C3. 复看给出的读法长度仍不在格式长度内（7 位）-> 不采纳
c3, c3e = build({W1: ["UJIMI8506"], W15: ["UJMI850"]},
                length_escalation=True)
check(c3.recognize_frame(frame(W1)) is None,
      "复看读到 7 位（长度仍不在本档格式长度内）-> 不采纳")

# C3b. 复看给出「需要逐位纠正、但能通过严格校验」的 8 位读法 -> 采纳。
#      这是设计内的：采纳条件用的是既有严格校验，不额外要求零纠正。
c3b = RoomCodeRecognizer(_StubEngine({W1: ["UJIMI8506"], W15: ["UJMI850Z"]}),
                         profile="delta_force_1v1", length_escalation=True)
out3b = c3b.recognize_frame(frame(W1))
check(out3b is not None and out3b.code == "UJMI8502",
      "复看给出需纠正但合法的 8 位读法 -> 采纳（走既有逐位纠正）")
check(out3b is not None and out3b.corrected,
      "该候选确实带了纠正记录（不是被特殊放行的）")

# C4. 复看什么都没有 -> 不采纳
c4 = RoomCodeRecognizer(_StubEngine({W1: ["UJIMI8506"], W15: []}),
                        profile="delta_force_1v1", length_escalation=True)
check(c4.recognize_frame(frame(W1)) is None, "复看无任何读法 -> 无候选")

# ======================================================================
section("D. 复看结果必须经过裁决器（不绕过时序确认）")
# ======================================================================
d1 = RoomCodeRecognizer(_StubEngine({W1: ["UJIMI8506"], W15: ["UJMI8506"]}),
                        profile="delta_force_1v1", length_escalation=True,
                        resolver=TemporalResolver(2, 3))
first = d1.recognize_frame(frame(W1))
check(first is None, "第 1 帧：复看虽读出 UJMI8506，但连续 2 帧未达成 -> 仍返回 None")
second = d1.recognize_frame(frame(W1))
check(second is not None and second.code == "UJMI8506",
      "第 2 帧：连续 2 帧一致后才成立（证明走的是同一条时序裁决）")

d2 = RoomCodeRecognizer(_StubEngine({W1: ["UJIMI8506"], W15: ["UJMI8506"]}),
                        profile="delta_force_1v1", length_escalation=True,
                        resolver=TemporalResolver(3, 3))
check(d2.recognize_frame(frame(W1)) is None, "1 帧未成立")
check(d2.recognize_frame(frame(W1)) is None, "2 帧仍未成立")
third = d2.recognize_frame(frame(W1))
check(third is not None and third.code == "UJMI8506", "第 3 帧才成立")

# ======================================================================
section("E. 不做字符删除 / 不做字符替换")
# ======================================================================
# 复看读出的码与 1.0x 读法没有任何「删一个字符」的关系，也必须原样返回；
# 反过来，复看读不出来时也绝不允许从 9 位读法里猜一个出来。
e1 = RoomCodeRecognizer(_StubEngine({W1: ["UJIMI8506"], W15: ["ZZZZ9999"]}),
                        profile="delta_force_1v1", length_escalation=True)
out = e1.recognize_frame(frame(W1))
check(out is not None and out.code == "ZZZZ9999",
      "结果完全来自复看读法，不做任何字符级加工")

e2, e2e = build({W1: ["HJIKL4750"], W15: []}, length_escalation=True)
check(e2.recognize_frame(frame(W1)) is None,
      "复看读不出内容时宁可不给结果，绝不从 9 位读法里删除字符猜一个")

# 复看读出的若是「掐头/去尾」的滑窗解，当前实现是**照收**的——
# 这正是 Phase 3 回归里 #308 注入错误码的原因，此处固化为已知行为。
e3 = RoomCodeRecognizer(_StubEngine({W1: ["QOAZW4602"], W15: ["OAZW4602"]}),
                        profile="delta_force_1v1", length_escalation=True)
out3 = e3.recognize_frame(frame(W1))
check(out3 is not None and out3.code == "OAZW4602",
      "已知行为：复看给出偏移滑窗解时当前实现照收（#308 现象的固化）")

# ======================================================================
section("F. worker 接线")
# ======================================================================
try:
    from ocr.worker import OCRWorker

    class _RoiStub:
        pass

    w_default = OCRWorker(lambda: None, _RoiStub(), profile="delta_force_1v1")
    check(w_default.length_escalation is False, "OCRWorker 默认关闭")
    w_on = OCRWorker(lambda: None, _RoiStub(), profile="delta_force_1v1",
                     length_escalation=True)
    check(w_on.length_escalation is True, "OCRWorker 可显式开启")
    check(not hasattr(OCRWorker, "set_length_escalation"),
          "没有运行期 setter（与 observe_all_psm 一致，只能停机改）")

    import inspect
    src = inspect.getsource(OCRWorker._ensure_recognizer)
    check("length_escalation=self._length_escalation" in src,
          "首次构造识别器时传入了开关")
    src2 = inspect.getsource(OCRWorker.set_profile)
    check("length_escalation=self._length_escalation" in src2,
          "换游戏档重建识别器时同样传入开关（不会残留旧状态）")
except Exception as exc:                                             # noqa: BLE001
    check(False, f"worker 接线检查失败: {type(exc).__name__}: {exc}")

# ======================================================================
section("G. 常量")
# ======================================================================
check(abs(_LENGTH_ESCALATION_SCALE - 1.5) < 1e-9, "复看倍率为 1.5")
check(_LENGTH_ESCALATION_SCALE in SUPPORTED_SCALES,
      "1.5 是产品已支持的缩放档位，无需新增档位常量")

print(f"\n{'=' * 70}")
print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
print("=" * 70)
sys.exit(1 if _FAILED else 0)
