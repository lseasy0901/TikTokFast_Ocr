#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 8.4: OCR 多读法仲裁 + 可疑位放大复看 回归测试

背景（实测）：
    同一个房间号卡片，Tesseract 的不同 PSM 会给出不同读法——
    PSM 6 读 'Gom180'（第二个 G 的横杠没认出来），PSM 11 读 'GGM180'。
    改动前引擎「遇到第一个非空读法就返回」，于是先出现的错误读法成了唯一真相。

本套件覆盖：
    [1] 引擎 iter_readings：按 PSM 顺序产出非空读法，且【按需】产出（可提前停止）
    [2] 多读法仲裁：纠正少者优先，其次大小写折叠少者；同代价不同码 -> 不返回
    [3] 零纠正零折叠即停：干净读法不为后面的 PSM 多花一次 OCR
    [4] 可疑位放大复看：只在读法带小写字形时触发，且只能改动那几位
    [5] 裁决器契约：一帧仍然只 offer() 一次（时序观测不被复读污染）

全部为文本/桩级测试：不读取用户截图、不启动 Tesseract、不依赖本机环境。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from ocr.code_format import CodeFormat, CharClass  # noqa: E402
from ocr.correction import resolve_candidate  # noqa: E402
from ocr.engine import OCREngine, OCREngineError  # noqa: E402
from ocr.room_code import RoomCodeCandidate, RoomCodeRecognizer  # noqa: E402
from ocr.room_code import CandidateResolver, SingleFrameResolver  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {message}", flush=True)
    else:
        _FAILED += 1
        print(f"    [FAIL] {message}", flush=True)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ----------------------------------------------------------------------
# 桩
# ----------------------------------------------------------------------
class _ScriptedEngine:
    """按图像宽度给出预设读法的引擎桩（模拟不同放大倍率下的不同读法）。"""

    def __init__(self, script, default=()):
        #: script: {宽度: [(psm, 文本), ...]}；宽度 0 表示「其它宽度」
        self.script = script
        self.default = default
        self.calls = []          # 每次 iter_readings 的 (宽度,) —— 用于证明调了几次
        self.consumed = []       # 被消费者真正取走的读法数

    def iter_readings(self, image, preprocess=True):
        width = int(image.shape[1])
        self.calls.append(width)
        readings = self.script.get(width, self.default)

        class _Counter:
            pass

        for reading in readings:
            self.consumed.append(reading[0])
            yield reading


class _SingleReadingEngine:
    """只有 recognize() 的旧式引擎桩（证明兼容路径没被改坏）。"""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def recognize(self, image, preprocess=True):
        self.calls += 1
        return self.text


class _CountingResolver(CandidateResolver):
    """记录 offer() 次数，验证「一帧只裁决一次」。"""

    def __init__(self):
        self.offers = []

    def offer(self, candidate):
        self.offers.append(candidate)
        return candidate


def recognizer(engine, resolver=None, scale=1.0):
    return RoomCodeRecognizer(engine, profile="valorant", scale=scale,
                              resolver=resolver or SingleFrameResolver())


def image_of(width, height=40):
    return np.zeros((height, width, 3), dtype=np.uint8)


# ======================================================================
# 1. 引擎：iter_readings
# ======================================================================
def test_iter_readings():
    section("[1] OCREngine.iter_readings：按序产出非空读法")

    class _Fake(OCREngine):
        def __init__(self, script, modes=(6, 7, 11)):
            self.psm_modes = list(modes)
            self.script = script
            self.ran = []

        def _run_psm(self, image, psm):
            self.ran.append(psm)
            value = self.script[psm]
            if isinstance(value, Exception):
                raise value
            return value

    engine = _Fake({6: "Elta Fae Gom180 o", 7: "", 11: "Ebook GGM180 eee"})
    readings = list(engine.iter_readings(image_of(50)))
    check(readings == [(6, "Elta Fae Gom180 o"), (11, "Ebook GGM180 eee")],
          f"只产出非空读法且保持 PSM 顺序: {readings}")
    check(engine.ran == [6, 7, 11], f"全量遍历时三个 PSM 都跑了: {engine.ran}")

    section("[1b] 按需产出：消费者提前停止就不再跑后面的 PSM")
    engine2 = _Fake({6: "GGM180", 7: "GGM180", 11: "GGM180"})
    it = engine2.iter_readings(image_of(50))
    first = next(it)
    check(first == (6, "GGM180"), f"第一个读法来自 PSM 6: {first}")
    check(engine2.ran == [6], f"此时只跑过 PSM 6（不浪费 OCR 调用）: {engine2.ran}")

    section("[1c] 空图像与全部失败")
    # 生成器是惰性的：调用时不执行，异常在【被消费时】抛出。
    # 识别器正是在 for 循环里消费它，所以对外行为与改动前一致。
    engine3 = _Fake({6: "", 7: "", 11: ""}, modes=(6,))
    try:
        list(engine3.iter_readings(image_of(0, 0)))
        check(False, "空图像应当抛 OCREngineError")
    except OCREngineError as exc:
        check("输入图像为空" in str(exc), f"空图像抛 OCREngineError: {exc}")
    check(engine3.ran == [], f"空图像没有被送去 OCR: {engine3.ran}")

    engine4 = _Fake({6: RuntimeError("boom")}, modes=(6,))
    try:
        list(engine4.iter_readings(image_of(50)))
        check(False, "所有 PSM 都失败时应当抛 OCREngineError")
    except OCREngineError as exc:
        check("boom" in str(exc), f"全部失败时抛 OCREngineError: {exc}")

    engine5 = _Fake({6: "", 7: ""}, modes=(6, 7))
    check(list(engine5.iter_readings(image_of(50))) == [],
          "PSM 只是没读出东西（没报错）时返回空列表，不抛异常")

    section("[1d] recognize() 行为不变：仍是第一个非空读法")
    engine6 = _Fake({6: "first", 7: "", 11: "third"})
    check(engine6.recognize(image_of(50)) == "first",
          "recognize() 仍然返回第一个非空读法")


# ======================================================================
# 2/3. 多读法仲裁
# ======================================================================
def test_arbitration():
    section("[2] 读法仲裁：纠正少者优先，其次折叠少者")

    # 真实形态：PSM 6 把第二个 G 读成小写 o，PSM 11 读对
    engine = _ScriptedEngine({100: [(6, "Elta\nFae\nGom180 o"),
                                    (7, ""),
                                    (11, "Ebook\n\nGGM180\n\neee")]})
    result = recognizer(engine).recognize_frame(image_of(100))
    check(result is not None and result.code == "GGM180",
          f"小写折叠的读法被无折叠的读法取代: {result.code if result else None}")
    check(result is not None and result.case_fold_count == 0,
          "最终结果的折叠位数为 0")

    section("[2b] 反向情形（原来靠 PSM 顺序侥幸读对）不被改坏")
    engine2 = _ScriptedEngine({100: [(6, "GGM180 o RRR"), (7, ""), (11, "GoM180")]})
    result2 = recognizer(engine2).recognize_frame(image_of(100))
    check(result2 is not None and result2.code == "GGM180",
          f"PSM 11 的 'GoM180' 不会盖掉 PSM 6 的 'GGM180': "
          f"{result2.code if result2 else None}")

    section("[2c] 纠正次数优先于折叠次数")
    engine3 = _ScriptedEngine({100: [(6, "GGM18O"), (11, "GGM180")]})
    result3 = recognizer(engine3).recognize_frame(image_of(100))
    check(result3 is not None and result3.code == "GGM180",
          f"零纠正读法胜出: {result3.code if result3 else None}")

    section("[2d] 前面的读法给不出候选时，后面的读法可以救回来")
    engine4 = _ScriptedEngine({100: [(6, "NR Peace"), (7, ""), (11, "QAN353")]})
    result4 = recognizer(engine4).recognize_frame(image_of(100))
    check(result4 is not None and result4.code == "QAN353",
          f"PSM 6 无候选时采用 PSM 11 的读法: {result4.code if result4 else None}")

    section("[2e] 两个干净读法不一致：先出现的胜出，不为它多花 OCR")
    # 第一读法已经是零纠正零折叠时不再看后面的 PSM（省一次 200ms 的 OCR）。
    # 代价是「两个干净读法恰好不一致」时取先出现的那个——这与改动前
    # 「取第一个非空读法的候选」行为一致，不会比原来更糟。
    engine5 = _ScriptedEngine({100: [(6, "GGM180"), (11, "XYZ789")]})
    result5 = recognizer(engine5).recognize_frame(image_of(100))
    check(result5 is not None and result5.code == "GGM180",
          f"取先出现的干净读法: {result5.code if result5 else None}")
    check(engine5.calls == [100], f"没有为第二个干净读法再跑 OCR: {engine5.calls}")

    section("[2f] 非干净读法之间代价相同且不一致 -> 不赌，不返回")
    engine6 = _ScriptedEngine({100: [(6, "GGM18O"), (7, ""), (11, "ANM18O")]})
    check(recognizer(engine6).recognize_frame(image_of(100)) is None,
          "两个各带一次纠正、又给出不同房间号的读法时不返回结果")

    section("[3] 零纠正零折叠的读法已是最优：不再为后面的 PSM 花 OCR")
    engine6 = _ScriptedEngine({100: [(6, "QAN353"), (7, "QAN353"), (11, "XYZ789")]})
    result6 = recognizer(engine6).recognize_frame(image_of(100))
    check(result6 is not None and result6.code == "QAN353",
          f"干净读法直接定案: {result6.code if result6 else None}")
    check(engine6.calls == [100], f"只识别了一次: {len(engine6.calls)} 次")
    check(engine6.consumed == [6], f"只取走一个读法: {engine6.consumed}")


# ======================================================================
# 4. 可疑位放大复看
# ======================================================================
def test_escalation():
    section("[4] 读法带小写字形时放大一倍复看")

    # 窄图 = 1.0x（所有 PSM 都把小写 o 读出来）；宽图 = 2.0x（读对）
    engine = _ScriptedEngine({100: [(6, "GoM180")], 200: [(6, "GGM180")]})
    result = recognizer(engine).recognize_frame(image_of(100))
    check(result is not None and result.code == "GGM180",
          f"复看修正了可疑位: {result.code if result else None}")
    check(engine.calls == [100, 200], f"按 1.0x -> 2.0x 的顺序复看: {engine.calls}")
    check(result is not None and result.scale == 2.0,
          f"结果记录了真实读法倍率: {result.scale if result else None}")

    section("[4b] 复看结果只能改动可疑位")
    # 'GoM180' 的可疑位是下标 1；复看若给出别处也不同的码，一律不采纳
    engine2 = _ScriptedEngine({100: [(6, "GoM180")], 200: [(6, "XYZ180")]})
    result2 = recognizer(engine2).recognize_frame(image_of(100))
    check(result2 is not None and result2.code == "GOM180",
          f"别处不同 -> 保持原读法: {result2.code if result2 else None}")

    section("[4c] 复看自己也带可疑位时不采纳")
    engine3 = _ScriptedEngine({100: [(6, "GoM180")], 200: [(6, "GoM180")]})
    result3 = recognizer(engine3).recognize_frame(image_of(100))
    check(result3 is not None and result3.code == "GOM180",
          f"复看没看清 -> 保持原读法: {result3.code if result3 else None}")

    section("[4d] 读法干净时不做复看（不为正常帧增加延迟）")
    engine4 = _ScriptedEngine({100: [(6, "GGM180")]})
    result4 = recognizer(engine4).recognize_frame(image_of(100))
    check(result4 is not None and result4.code == "GGM180", "干净读法结果不变")
    check(engine4.calls == [100], f"没有发起 2.0x 复看: {engine4.calls}")


# ======================================================================
# 5. 契约
# ======================================================================
def test_contracts():
    section("[5] 一帧只向裁决器 offer() 一次")
    resolver = _CountingResolver()
    engine = _ScriptedEngine({100: [(6, "Elta\nGom180 o"), (11, "GGM180")],
                              200: [(6, "GGM180")]})
    recognizer(engine, resolver=resolver).recognize_frame(image_of(100))
    check(len(resolver.offers) == 1,
          f"多读法 + 复看之后仍然只 offer 一次: {len(resolver.offers)} 次")

    section("[5b] recognize_text() 单读法入口不变")
    engine2 = _ScriptedEngine({100: [(6, "GoM180")]})
    rec2 = recognizer(engine2)
    check(rec2.recognize_text("Room Code: ABC123").code == "ABC123",
          "单段文本仍按格式提取房间号")
    check(rec2.recognize_text("nothing here") is None, "无候选文本仍返回 None")

    section("[5c] 只有 recognize() 的旧式引擎仍可用（其它套件的桩）")
    rec3 = recognizer(_SingleReadingEngine("QAN353 e ARIAS"))
    result3 = rec3.recognize_frame(image_of(100))
    check(result3 is not None and result3.code == "QAN353",
          f"单读法引擎走兼容路径: {result3.code if result3 else None}")

    section("[5d] 折叠不计入纠正，且数字位不会折叠")
    fmt = CodeFormat.from_spec("test_6", "LLLDDD")
    folded = resolve_candidate("GoM180", fmt)
    check(folded is not None and folded.case_folds == (1,),
          f"小写 o 记在折叠位上: {folded.case_folds if folded else None}")
    check(folded is not None and folded.correction_count == 0,
          "大小写折叠不计入纠正次数")
    clean = resolve_candidate("GOM180", fmt)
    check(clean is not None and clean.case_folds == ()
          and clean.correction_count == 0, "'GOM180' 既无纠正也无折叠")
    digit = resolve_candidate("GGM1B0", fmt)
    check(digit is not None and digit.case_folds == ()
          and digit.correction_count == 1,
          "数字位的 'B'->'8' 是纠正，不是折叠")

    section("[5e] 候选对象仍然只是数据")
    engine5 = _ScriptedEngine({100: [(6, "GGM180")]})
    result5 = recognizer(engine5).recognize_frame(image_of(100))
    check(isinstance(result5, RoomCodeCandidate), "返回的仍是 RoomCodeCandidate")
    check(result5.repeated is False, "单帧直通时 repeated 恒为 False")


# ======================================================================
# main
# ======================================================================
def main() -> int:
    print("=" * 70, flush=True)
    print("Phase 8.4: OCR reading arbitration suite", flush=True)
    print("=" * 70, flush=True)

    test_iter_readings()
    test_arbitration()
    test_escalation()
    test_contracts()

    print("\n" + "=" * 70, flush=True)
    total = _PASSED + _FAILED
    if _FAILED == 0:
        print(f"RESULT: {_PASSED}/{total} checks passed, 0 failed", flush=True)
    else:
        print(f"RESULT: {_PASSED}/{total} checks passed, {_FAILED} FAILED", flush=True)
    print("=" * 70, flush=True)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
