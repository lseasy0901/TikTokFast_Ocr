#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR V2.1 Format-Aware OCR 回归测试

覆盖：
    - 三角洲行动 (LLLDDDD) / 无畏契约 (LLLDDD) 的严格校验
    - 长度、字符类、大小写
    - 从周边文本中提取候选
    - 逐位（位置感知）纠正 + 纠正后仍须通过严格校验
    - I/O/1/0 在允许的位置上保持合法，不被全局替换改坏
    - 歧义/不可解析输入必须返回「无结果」
    - 预处理缩放（可配置、默认关）
    - OCRWorker 集成：只传出通过校验的房间号；识别器仍在工作线程内构造

隔离说明：
    测试使用假引擎，不加载 Tesseract；APPDATA 指向临时目录，
    不读取也不改写真实的 license.json。
"""

import os
import sys
import tempfile
import threading
import time

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ocr-v21-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import ocr.worker as worker_mod  # noqa: E402
from ocr.code_format import CharClass, CodeFormat  # noqa: E402
from ocr.correction import resolve_char  # noqa: E402
from ocr.profiles import (  # noqa: E402
    DEFAULT_PROFILE_ID,
    DELTA_FORCE,
    PROFILES,
    VALORANT,
    GameProfile,
    get_profile,
    profile_ids,
)
from ocr.roi_manager import ROI, ROIManager  # noqa: E402
from ocr.room_code import (  # noqa: E402
    SUPPORTED_SCALES,
    CandidateResolver,
    RoomCodeRecognizer,
    SingleFrameResolver,
    extract_runs,
    normalize_text,
)

_PASSED = 0
_FAILED = 0
app = None


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
# 脚手架：假引擎（不加载 Tesseract）
# ----------------------------------------------------------------------
class _FakeEngine:
    """返回预设文本的假 OCREngine，同时记录收到的图像与调用线程。"""

    def __init__(self, text: str = ""):
        self.text = text
        self.images = []
        self.threads = []

    def recognize(self, image, preprocess: bool = True):
        self.images.append(image)
        self.threads.append(threading.current_thread())
        return self.text


class _RecordingResolver(CandidateResolver):
    """记录每次裁决输入，用来证明「预留接口」真的被调用。"""

    def __init__(self):
        self.offered = []

    def offer(self, candidate):
        self.offered.append(candidate)
        return candidate


class _FakeBuffer:
    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


def recognizer_for(profile, text: str = "", **kwargs):
    """构造 (识别器, 假引擎)。"""
    engine = _FakeEngine(text)
    return RoomCodeRecognizer(engine, profile=profile, **kwargs), engine


def code_of(rec: RoomCodeRecognizer, text: str):
    candidate = rec.recognize_text(text)
    return candidate.code if candidate is not None else None


def candidate_of(rec: RoomCodeRecognizer, text: str):
    return rec.recognize_text(text)


# ======================================================================
# 1. 格式与游戏档
# ======================================================================
def test_formats_and_profiles():
    section("[1] CodeFormat / GameProfile 抽象")

    delta_fmt = DELTA_FORCE.primary_format
    valorant_fmt = VALORANT.primary_format

    check(delta_fmt.length == 7 and delta_fmt.regex == "^[A-Z]{3}[0-9]{4}$",
          f"Delta Force format = 7 位 ^[A-Z]{{3}}[0-9]{{4}}$ (实际 {delta_fmt.describe()})")
    check(valorant_fmt.length == 6 and valorant_fmt.regex == "^[A-Z]{3}[0-9]{3}$",
          f"VALORANT format = 6 位 ^[A-Z]{{3}}[0-9]{{3}}$ (实际 {valorant_fmt.describe()})")
    check(delta_fmt.positions[3] is CharClass.DIGIT and delta_fmt.positions[2] is CharClass.LETTER,
          "逐位字符类：第 2 位字母、第 3 位数字")

    check(set(profile_ids()) == {"auto", "delta_force", "valorant"},
          f"注册了游戏档: {profile_ids()}")
    check(DEFAULT_PROFILE_ID == "auto",
          "默认档是 auto（本迭代还没有游戏选择 UI）")
    check(get_profile(None) is PROFILES[DEFAULT_PROFILE_ID], "get_profile() 返回默认档")
    check(get_profile("valorant") is PROFILES["valorant"], "get_profile('valorant') 命中")

    section("[1b] 默认档：同时覆盖两种格式，长度恰好匹配的整段优先")
    auto_rec, _ = recognizer_for(None)
    check([f.name for f in auto_rec.profile.formats] == ["delta_force_7", "valorant_6"],
          "默认档持有 7 位与 6 位两个格式")

    # 这是「GUI 不传 profile」时的真实行为：两种长度的码都要能认出来，
    # 且 7 位码不能被 6 位格式截断成 6 位
    check(code_of(auto_rec, "ABC1234") == "ABC1234",
          "默认档：7 位码 -> ABC1234（不被 6 位格式截断）")
    check(code_of(auto_rec, "ABC123") == "ABC123", "默认档：6 位码 -> ABC123")
    check(code_of(auto_rec, "IOI1234") == "IOI1234", "默认档：7 位 I/O 码正常")
    check(code_of(auto_rec, "IOI123") == "IOI123", "默认档：6 位 I/O 码正常")
    check(code_of(auto_rec, "ABC123XYZ456") is None,
          "默认档：并列候选仍然无结果（不因为多格式而放松）")

    # OCR 实测会把房间号与相邻文字粘成一串（空格丢失），
    # 此时 7 位读法应当胜过 6 位截断读法
    check(code_of(auto_rec, "ABC1234other") == "ABC1234",
          "默认档：'ABC1234other' -> ABC1234（7 位读法胜过 6 位截断）")
    check(code_of(auto_rec, "ABC123other") == "ABC123",
          "默认档：'ABC123other' -> ABC123")

    try:
        get_profile("no_such_game")
        check(False, "未知档标识应当抛错（不静默回退）")
    except ValueError:
        check(True, "未知档标识抛 ValueError（不静默回退到默认档）")

    # 未来扩张：同一游戏可以有多个格式
    multi = GameProfile(
        "multi_format_demo", "多格式演示",
        formats=(VALORANT.primary_format, DELTA_FORCE.primary_format),
    )
    rec_multi, _ = recognizer_for(multi)
    check(code_of(rec_multi, "ABC123") == "ABC123", "多格式档：首个格式命中 6 位")
    check(code_of(rec_multi, "ABC1234") == "ABC1234", "多格式档：次个格式命中 7 位")

    # 未来扩张：新格式只需一行 spec
    spec_fmt = CodeFormat.from_spec("demo_9", "LLLDDDDDD")
    check(spec_fmt.length == 9 and spec_fmt.regex == "^[A-Z]{3}[0-9]{6}$",
          "from_spec('LLLDDDDDD') 得到 9 位格式")
    try:
        CodeFormat.from_spec("bad", "LLLX")
        check(False, "非法 spec 应当抛错")
    except ValueError:
        check(True, "非法 spec 抛 ValueError")


# ======================================================================
# 2. 三角洲行动：合法 / 长度 / 字符类
# ======================================================================
def test_delta_force_valid():
    section("[2] Delta Force: 合法代码")
    rec, _ = recognizer_for("delta_force")

    for raw in ("ABC1234", "XYZ0000", "IOI1234", "OOO1000", "III0000", "ABC1234\n"):
        candidate = candidate_of(rec, raw)
        expected = raw.strip()
        check(candidate is not None and candidate.code == expected,
              f"'{raw!r}' -> {expected}")
    check(candidate_of(rec, "ABC1234").correction_count == 0,
          "合法代码不需要任何纠正")


def test_delta_force_invalid_length():
    section("[3] Delta Force: 长度不合法")
    rec, _ = recognizer_for("delta_force")
    fmt = rec.profile.primary_format

    for raw in ("", "ABC123", "AB1234", "A1C123"):
        check(code_of(rec, raw) is None, f"过短 '{raw}' -> 无结果")
    check(code_of(rec, "ABCDEFGH") is None, "过长 'ABCDEFGH' -> 无结果")

    check(fmt.accepts("ABC1234") is True, "accepts(): 长度精确相等才通过")
    check(fmt.accepts("ABC123") is False, "accepts(): 短一位不通过")
    check(fmt.accepts("ABC12345") is False, "accepts(): 长一位不通过")
    check(fmt.accepts(" ABC1234") is False, "accepts(): 不 strip，含空格的输入不通过")


def test_delta_force_invalid_class():
    section("[4] Delta Force: 字符类不合法（不可纠正）")
    rec, _ = recognizer_for("delta_force")

    # 'D' 在字母位的纠正表之外，'X' 在数字位的纠正表之外 -> 不可猜，放弃
    for raw in ("ABCD123", "ABC12X4", "ABC123X", "ABabcdef"):
        check(code_of(rec, raw) is None, f"不可纠正 '{raw}' -> 无结果")

    fmt = rec.profile.primary_format
    check(fmt.accepts("abc1234") is False, "accepts(): 小写字母不通过（需先做大小写规范化）")
    check(fmt.accepts_char("A", 0) and not fmt.accepts_char("1", 0),
          "accepts_char(): 字母位只接受字母")
    check(fmt.accepts_char("1", 3) and not fmt.accepts_char("A", 3),
          "accepts_char(): 数字位只接受数字")


# ======================================================================
# 3. 无畏契约：合法 / 长度 / 字符类
# ======================================================================
def test_valorant_valid():
    section("[5] VALORANT: 合法代码")
    rec, _ = recognizer_for("valorant")

    for raw in ("ABC123", "XYZ789", "IOI123", "IOI012", "OOO000"):
        candidate = candidate_of(rec, raw)
        check(candidate is not None and candidate.code == raw, f"'{raw}' -> '{raw}'")
    check(candidate_of(rec, "ABC123").format_name == "valorant_6",
          "候选带上来源格式名")


def test_valorant_invalid_length():
    section("[6] VALORANT: 长度不合法")
    rec, _ = recognizer_for("valorant")
    fmt = rec.profile.primary_format

    for raw in ("", "ABC12", "AB123", "12"):
        check(code_of(rec, raw) is None, f"过短 '{raw}' -> 无结果")
    check(code_of(rec, "HELLOWORLD") is None, "过长且无合法窗口 'HELLOWORLD' -> 无结果")

    check(fmt.accepts("ABC123") is True, "accepts(): 6 位通过")
    check(fmt.accepts("ABC1234") is False, "accepts(): 7 位不通过（对 6 位格式而言）")


def test_valorant_invalid_class():
    section("[7] VALORANT: 字符类不合法（不可纠正）")
    rec, _ = recognizer_for("valorant")

    for raw in ("ABC12D", "ABC1G3", "ABGC12", "ABC12!"):
        check(code_of(rec, raw) is None, f"不可纠正 '{raw}' -> 无结果")


# ======================================================================
# 4. 从周边文本中提取候选
# ======================================================================
def test_extraction_from_surrounding_text():
    section("[8] 候选提取：从周边文本中定位房间号")
    delta_rec, _ = recognizer_for("delta_force")
    valorant_rec, _ = recognizer_for("valorant")

    check(code_of(delta_rec, "Room Code: ABC1234") == "ABC1234",
          "'Room Code: ABC1234' -> ABC1234")
    check(code_of(delta_rec, "ABC1234\n") == "ABC1234", "'ABC1234\\n' -> ABC1234")
    check(code_of(delta_rec, "ABC1234 other") == "ABC1234", "'ABC1234 other' -> ABC1234")
    check(code_of(delta_rec, "  房间号 ABC1234  ") == "ABC1234",
          "混入中文与空白仍能提取")
    check(code_of(valorant_rec, "Room Code: ABC123") == "ABC123",
          "VALORANT 'Room Code: ABC123' -> ABC123")

    # OCR 在房间号中间插了空格 -> 拼回（长度必须精确相等）
    check(code_of(delta_rec, "ABC 1234") == "ABC1234", "'ABC 1234' 拼回 ABC1234")
    check(code_of(valorant_rec, "ABC 123") == "ABC123", "'ABC 123' 拼回 ABC123")

    # 拼回要求长度精确相等，因此不会把普通句子拼成房间号
    check(code_of(delta_rec, "Room Code ABC1234") == "ABC1234",
          "'Room Code ABC1234' 不会与前面的词错误拼接")

    check(normalize_text("ABC1234\n\n") == "ABC1234", "清洗：折叠换行与空白")
    check(normalize_text("Room  Code:\tABC1234") == "Room Code: ABC1234",
          "清洗：保留分词边界，不做字符替换")
    check(extract_runs("Room Code: ABC1234") == ["Room", "Code", "ABC1234"],
          "分词：非字母数字作为分隔符")


# ======================================================================
# 5. 逐位（位置感知）纠正
# ======================================================================
def test_position_aware_correction():
    section("[9] 逐位纠正：数字位上的字母形字符")
    valorant_rec, _ = recognizer_for("valorant")
    delta_rec, _ = recognizer_for("delta_force")

    # 需求里的例子：VALORANT 原始 OCR "ABC12O" -> O 在数字位不合法 -> ABC120
    candidate = candidate_of(valorant_rec, "ABC12O")
    check(candidate is not None and candidate.code == "ABC120",
          "VALORANT 'ABC12O' -> 'ABC120'（O 在数字位纠正为 0）")
    check(candidate.correction_count == 1, "记录到 1 处纠正")
    check(candidate.corrected is True and "0" in candidate.correction_summary(),
          f"纠正摘要可读: {candidate.correction_summary()}")

    for raw, expected in (
        ("ABC12l", "ABC121"),   # 小写 L -> 数字位的 1
        ("ABC12I", "ABC121"),   # I -> 1
        ("ABC1S3", "ABC153"),   # S -> 5
        ("ABC12Z", "ABC122"),   # Z -> 2
        ("ABC12B", "ABC128"),   # B -> 8
        ("ABC1|3", "ABC113"),   # 竖线 -> 1
    ):
        check(code_of(valorant_rec, raw) == expected,
              f"VALORANT '{raw}' -> '{expected}'")

    section("[9b] 逐位纠正：字母位上的数字形字符")
    # 三角洲格式是 LLLDDDD，前 3 位是字母位、后 4 位是数字位，
    # 因此下面的数字形字符都出现在字母位上（位置 0-2）
    for raw, expected in (
        ("AB11234", "ABI1234"),   # 第 2 位 1 -> I
        ("1BC1234", "IBC1234"),   # 第 0 位 1 -> I
        ("0BC1234", "OBC1234"),   # 第 0 位 0 -> O
        ("A2C1234", "AZC1234"),   # 第 1 位 2 -> Z
        ("A5C1234", "ASC1234"),   # 第 1 位 5 -> S
    ):
        check(code_of(delta_rec, raw) == expected,
              f"Delta Force '{raw}' -> '{expected}'")

    section("[9c] 同一字符在不同位置有不同结论")
    # 第 2 位是字母位（'1' -> 'I'），第 3 位是数字位（'O' -> '0'）
    candidate = candidate_of(delta_rec, "IO1O234")
    check(candidate is not None and candidate.code == "IOI0234",
          "Delta Force 'IO1O234' -> 'IOI0234'（同一串里两个方向各纠正一次）")
    check(candidate.correction_count == 2, "恰好 2 处纠正")

    section("[9d] 纠正仍然受严格校验约束")
    # 整个字母前缀被读成数字形字符 —— 实测 Tesseract 就是这么读的，
    # 映射是单射，因此 3 处纠正是可以接受的（上限之内）
    candidate = candidate_of(delta_rec, "1011000")
    check(candidate is not None and candidate.code == "IOI1000",
          "Delta Force '1011000' -> 'IOI1000'（整个字母前缀被误读为数字）")
    check(candidate.correction_count == 3, "记录到 3 处纠正")

    # 超过上限（4 处，连数字位也要改）-> 放弃
    check(candidate_of(valorant_rec, "121O23") is None,
          "需要 4 处纠正（超过上限 3）-> 无结果")
    check(candidate_of(delta_rec, "121O234") is None,
          "Delta Force 需要 4 处纠正 -> 无结果")

    # 恰好用满额度内（2 处）仍然接受，且结果必须合法
    borderline = candidate_of(valorant_rec, "A12345")
    check(borderline is not None and borderline.code == "AIZ345",
          "2 处纠正（上限之内）-> 'AIZ345'")

    # 其它实测到的 Tesseract 误读（见 correction.MAX_CORRECTIONS 的注释）
    for rec, raw, expected in (
        (delta_rec, "0000000", "OOO0000"),
        (valorant_rec, "101123", "IOI123"),
    ):
        check(code_of(rec, raw) == expected, f"实测误读 '{raw}' -> '{expected}'")

    # 纯数字噪声即使在同样额度下也必须无结果（映射表之外的字符不猜）
    for rec, raw in ((delta_rec, "1234567"), (delta_rec, "9876543"),
                     (valorant_rec, "123456"), (valorant_rec, "987654")):
        check(code_of(rec, raw) is None, f"噪声 '{raw}' -> 无结果")

    # 纠正结果必须自身通过严格校验
    for raw in ("ABC12O", "ABC12l", "AB11234", "IO1O234", "A1C1234"):
        rec = delta_rec if len(raw) == 7 else valorant_rec
        candidate = candidate_of(rec, raw)
        if candidate is None:
            check(False, f"'{raw}' 应当能纠正出结果")
            continue
        fmt = rec.profile.primary_format
        check(fmt.accepts(candidate.code) and len(candidate.code) == fmt.length,
              f"'{raw}' 的纠正结果 '{candidate.code}' 通过严格校验")

    check(resolve_char("O", CharClass.DIGIT) == "0"
          and resolve_char("O", CharClass.LETTER) == "O",
          "resolve_char(): 同一字符按位置给出不同结论")
    check(resolve_char("G", CharClass.DIGIT) is None,
          "resolve_char(): 映射表之外的字符返回 None（不猜测）")


# ======================================================================
# 6. I/O/1/0 必须保持合法，禁止全局替换
# ======================================================================
def test_io10_stay_valid():
    section("[10] I/O/1/0 在允许的位置上保持原样（禁止全局替换）")
    valorant_rec, _ = recognizer_for("valorant")
    delta_rec, _ = recognizer_for("delta_force")

    cases = (
        # VALORANT 是 LLLDDD：字母位上的 I/O 合法，数字位上的 0/1 合法
        (valorant_rec, "IOI123"),
        (valorant_rec, "OIO012"),
        (valorant_rec, "III000"),
        (valorant_rec, "OOO000"),
        # Delta Force 是 LLLDDDD：同理
        (delta_rec, "OOO1000"),
        (delta_rec, "III0000"),
        (delta_rec, "IOI0000"),
    )
    for rec, raw in cases:
        candidate = candidate_of(rec, raw)
        check(candidate is not None and candidate.code == raw,
              f"'{raw}' 原样保留")
        check(candidate.correction_count == 0 and candidate.corrected is False,
              f"'{raw}' 不需要任何纠正（全局 O->0 / I->1 会改坏它）")

    # 明确对照：全局替换会产生的错误结果
    check(code_of(valorant_rec, "IOI123") != "101123",
          "确认没有发生 O->0 / I->1 的全局替换")


# ======================================================================
# 7. 歧义 / 无法解析 -> 无结果
# ======================================================================
def test_ambiguous_returns_none():
    section("[11] 歧义或无法解析 -> 无结果（绝不返回非法房间号）")
    valorant_rec, _ = recognizer_for("valorant")
    delta_rec, _ = recognizer_for("delta_force")

    for raw in ("", None, "hello world", "!!!!", "  ", "12345678"):
        check(code_of(valorant_rec, raw) is None, f"{raw!r} -> 无结果")

    # 同一段文本里出现两个同样「零纠正」的候选 -> 无法分辨，不赌
    check(code_of(valorant_rec, "ABC123XYZ456") is None,
          "'ABC123XYZ456' 两个零纠正候选并列 -> 无结果（不赌其中一个）")
    check(code_of(delta_rec, "ABC1234XYZ5678") is None,
          "Delta Force 'ABC1234XYZ5678' 两个零纠正候选并列 -> 无结果")

    # 只有一个候选、且它的纠正代价更低时，仍然照常返回
    check(code_of(valorant_rec, "garbage ABC123") == "ABC123",
          "'garbage ABC123' 只有一个合法候选 -> 正常返回")

    # 数字位的字形超出有限映射表
    check(code_of(delta_rec, "1234567") is None,
          "'1234567' 需要 '3'->? 表里没有 -> 无结果")

    # 结果要么是合法房间号，要么什么都没有
    for raw in ("ABC123", "ABC12O", "garbage", "ABC123XYZ456", "ABC12D"):
        candidate = candidate_of(valorant_rec, raw)
        if candidate is None:
            check(True, f"'{raw}' -> 无结果（合法）")
        else:
            check(valorant_rec.profile.primary_format.accepts(candidate.code),
                  f"'{raw}' -> '{candidate.code}' 且通过严格校验")


# ======================================================================
# 8. 预处理缩放
# ======================================================================
def test_resize_preprocessing():
    section("[12] 预处理缩放（可配置、默认保守）")
    frame = np.zeros((40, 100, 3), dtype=np.uint8)

    check(SUPPORTED_SCALES == (1.0, 1.5, 2.0),
          f"提供少量固定档位: {SUPPORTED_SCALES}")

    default_rec, engine = recognizer_for("delta_force", "ABC1234")
    check(default_rec.scale == 1.0, "默认 1.0x（不放大）")
    check(default_rec.prepare(frame) is frame, "1.0x 是恒等操作，不复制图像")
    default_rec.recognize_frame(frame)
    check(engine.images[0].shape == (40, 100, 3), "1.0x 时引擎收到原始尺寸")

    for scale, expected_shape in ((1.5, (60, 150, 3)), (2.0, (80, 200, 3))):
        rec, engine = recognizer_for("delta_force", "Room Code: ABC1234", scale=scale)
        candidate = rec.recognize_frame(frame)
        check(engine.images[0].shape == expected_shape,
              f"{scale}x 时引擎收到 {expected_shape[1]}x{expected_shape[0]}")
        check(candidate is not None and candidate.code == "ABC1234",
              f"{scale}x 不影响识别结果")
        check(candidate.scale == scale, f"候选记录了缩放 {scale}x")

    for bad in (0.5, 3.0, 4.0):
        try:
            recognizer_for("delta_force", scale=bad)
            check(False, f"非法缩放 {bad} 应当抛错")
        except ValueError:
            check(True, f"非法缩放 {bad} 抛 ValueError")


# ======================================================================
# 9. 预留的时序裁决接口
# ======================================================================
def test_resolver_hook():
    section("[13] 预留的候选裁决接口（本迭代不实现多帧）")
    resolver = _RecordingResolver()
    rec, _ = recognizer_for("valorant", resolver=resolver)

    check(rec.resolver is resolver, "识别器使用注入的裁决器")
    check(isinstance(recognizer_for("valorant")[0].resolver, SingleFrameResolver),
          "默认使用单帧直通裁决器")

    code_of(rec, "ABC123")
    code_of(rec, "hello world")
    check(len(resolver.offered) == 2, "每一帧都会经过裁决器（含无结果帧）")
    check(resolver.offered[0] is not None and resolver.offered[0].code == "ABC123",
          "裁决器收到严格校验过的候选")
    check(resolver.offered[1] is None, "无结果帧以 None 交给裁决器")


# ======================================================================
# 10. OCRWorker 集成
# ======================================================================
def test_worker_integration():
    section("[14] OCRWorker 集成：只传出通过校验的房间号")
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    engine = _FakeEngine("Room Code: ABC12O")
    worker = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: _FakeBuffer(frame),
        roi_manager=roi_manager,
        engine=engine,
        interval_ms=50,
        profile="valorant",
    )

    check(worker._recognizer is None,
          "构造 OCRWorker 不构造识别器（仍在工作线程内延迟创建）")

    texts = []
    copies = []
    worker.text_updated.connect(lambda t: texts.append(t))
    worker.copy_requested.connect(lambda t: copies.append(t))

    worker.start_recognition()
    _pump_until(lambda: "ABC120" in texts, timeout=10.0)

    check("ABC120" in texts, f"OCR 原始 'Room Code: ABC12O' -> 传出 'ABC120' (收到 {texts})")
    check(worker._recognizer is not None,
          "识别器在识别开始后才存在（工作线程内创建）")
    check(worker._recognizer.profile.profile_id == "valorant",
          "游戏档被正确传入识别器")
    check(copies == ["ABC120"], f"只有合法房间号被送去复制 (收到 {copies})")

    # 换成不可识别的文本：应传出空串，且不触发复制
    engine.text = "hello world"
    copies.clear()
    _pump_until(lambda: "" in texts, timeout=10.0)

    check("" in texts, f"不可识别文本传出空串 (收到 {texts})")
    check(copies == [], f"空结果不触发剪贴板复制 (收到 {copies})")
    check(all(t is not threading.main_thread() for t in engine.threads),
          "识别始终发生在工作线程，不在 GUI 线程")
    check(worker.error_count == 0, "整个过程无错误")

    worker.stop_recognition()
    worker.wait(2000)
    check(worker.is_recognizing() is False, "工作线程已停止")


def _pump_until(predicate, timeout: float) -> bool:
    """驱动 Qt 事件循环直到条件成立（跨线程信号需要投递到主线程）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


# ======================================================================
# main
# ======================================================================
def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])

    print("=" * 70, flush=True)
    print("OCR V2.1: format-aware OCR suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print("=" * 70, flush=True)

    test_formats_and_profiles()
    test_delta_force_valid()
    test_delta_force_invalid_length()
    test_delta_force_invalid_class()
    test_valorant_valid()
    test_valorant_invalid_length()
    test_valorant_invalid_class()
    test_extraction_from_surrounding_text()
    test_position_aware_correction()
    test_io10_stay_valid()
    test_ambiguous_returns_none()
    test_resize_preprocessing()
    test_resolver_hook()
    test_worker_integration()

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
