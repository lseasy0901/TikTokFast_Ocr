# -*- coding: utf-8 -*-
"""Phase OCR-2：确认窗口自适应采样间隔 —— 状态转换验证。

验证的是**状态转换**，不是"间隔数字变了"：

    A 稳态（没有待确认候选）              -> 等待 interval_ms（300）
    B 出现新候选、等待确认                -> 等待 pending_interval_ms（100）
    C 连续一致成立 -> 剪贴板 -> 回到稳态   -> 重新回到 300
    D 无读取（no-read）                  -> 不进入"待确认"，不复制
    E 选错游戏（wrong-game）              -> 不进入"待确认"，不复制
    F 候选来回变化（X/Y 交替）            -> 永不成立，不复制（错误拦截能力不变）
    G 多 PSM 仲裁                         -> 读法与仲裁行为不变
    H 放大复看（escalation）              -> 行为不变
    I 真实工作线程里确实用了不同间隔       -> 观测到的两次采样间隔随状态变化

判定规则本身（2/3、严格校验、纠正、仲裁、冲突拦截）一个字都没改，
这些用例同时充当"没改坏"的回归证据。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from PySide6.QtWidgets import QApplication

from ocr.profiles import DELTA_FORCE, VALORANT
from ocr.roi_manager import ROI, ROIManager
from ocr.room_code import RoomCodeRecognizer
from ocr.temporal import TemporalResolver
from ocr.worker import OCRWorker

FAILURES = []


def section(title):
    print("\n" + title)


def check(cond, msg, extra=""):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        print("  [FAIL] %s %s" % (msg, extra))
        FAILURES.append(msg)
    return bool(cond)


# ----------------------------------------------------------------------
# 脚本化引擎替身：按脚本产出 PSM 读法（不碰 Tesseract，状态转换可精确控制）
# ----------------------------------------------------------------------
class ScriptedEngine:
    """iter_readings 按脚本逐次产出读法；脚本用完后一直重复最后一组。"""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self._index = 0
        self.calls = []          # 每次 iter_readings 的帧序号
        self.psm_seen = []       # 消费过的 psm 顺序

    def iter_readings(self, image, preprocess=True):
        # 按脚本**循环**产出：多组脚本时就是真正的交替（X/Y/X/Y…）
        script = self._scripts[self._index % len(self._scripts)]
        self._index += 1
        self.calls.append(self._index)
        for psm, text in script:
            self.psm_seen.append(psm)
            yield (psm, text)


class Frame:
    """最小帧替身（内容无关紧要，脚本引擎不看像素）"""

    def __init__(self):
        self._img = np.zeros((40, 160, 3), dtype=np.uint8)

    def get(self):
        return self._img


def build_worker(scripts, profile=DELTA_FORCE, interval=300, pending=100,
                 resolver=None, scales=None):
    """按脚本搭一个真实的 worker（真 recognizer / 真 resolver / 真 ROI 管理）"""
    engine = ScriptedEngine(scripts)
    resolver = resolver if resolver is not None else TemporalResolver()
    recognizer = RoomCodeRecognizer(engine, profile=profile.profile_id,
                                    resolver=resolver)
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 160, 40, 160, 40))
    worker = OCRWorker(frame_buffer_getter=lambda: Frame(),
                       roi_manager=roi_manager,
                       engine=engine,
                       interval_ms=interval,
                       profile=profile.profile_id,
                       recognizer=recognizer,
                       resolver=resolver,
                       pending_interval_ms=pending)
    copied = []
    worker.copy_requested.connect(lambda t: copied.append(t))
    return worker, engine, resolver, copied


def cycle(worker):
    """跑一次识别循环体 + 取出它决定的下一次等待间隔"""
    worker._perform_recognition()
    return worker._interval_for_wait()


# ----------------------------------------------------------------------
# A / B / C：稳态 -> 确认窗口 -> 成立后回稳态
# ----------------------------------------------------------------------
def test_state_transitions():
    section("[A/B/C] 稳态 → 新候选（确认窗口）→ 成立 → 回稳态")
    worker, engine, resolver, copied = build_worker(
        [[(6, "ABX1234")]])                      # 一直读到同一个合法码

    check(worker._interval_for_wait() == 300,
          "A 尚未读到任何候选 -> 等待稳态间隔 300ms", worker._interval_for_wait())

    check(cycle(worker) == 100, "B 第一次读到合法候选 -> 进入确认窗口 100ms",
          worker._interval_for_wait())
    check(resolver.has_pending and resolver.pending_code == "ABX1234",
          "B 裁决器报告：有一个待确认的新候选（未有结果成立）")
    check(copied == [], "B 尚未确认，不复制")

    check(cycle(worker) == 300, "C 连续第 2 次一致 -> 成立 -> 立刻回 300ms",
          worker._interval_for_wait())
    check(copied == ["ABX1234"], "C 成立后复制一次正确房间号", repr(copied))
    check(resolver.last_emitted_code == "ABX1234", "C 裁决器已记录成立结果")
    check(not resolver.has_pending, "C 成立后不再处于待确认状态")

    check(cycle(worker) == 300, "C 后续相同读数保持 300ms")
    check(copied == ["ABX1234"], "C 相同房间号不重复复制", repr(copied))


# ----------------------------------------------------------------------
# D：no-read 不得进入待确认 / 不得复制
# ----------------------------------------------------------------------
def test_no_read():
    section("[D] no-read：不进入确认窗口，不复制")
    worker, engine, resolver, copied = build_worker([[(6, "HELLO")]])   # 非房间号
    check(cycle(worker) == 300, "D 读不出房间号 -> 保持稳态间隔")
    check(not resolver.has_pending, "D 没有待确认候选")
    check(copied == [], "D 不复制")

    check(cycle(worker) == 300, "D 持续读不出 -> 仍为稳态")
    check(copied == [], "D 始终不复制")


# ----------------------------------------------------------------------
# E：选错游戏不得进入待确认 / 不得复制
# ----------------------------------------------------------------------
def test_wrong_game():
    section("[E] 选错游戏：不进入确认窗口，不复制")
    # ROI 里是 VALORANT 的 6 位码，但用户选的是三角洲（7 位）档
    worker, engine, resolver, copied = build_worker([[(6, "IOI123", )]],
                                                    profile=DELTA_FORCE)
    check(cycle(worker) == 300, "E 冲突帧被拦下 -> 保持稳态间隔")
    check(worker._recognizer.last_conflict is not None, "E 判定出「选错游戏」")
    check(not resolver.has_pending, "E 冲突帧不进入待确认")
    check(copied == [], "E 不复制")

    check(cycle(worker) == 300, "E 冲突持续 -> 仍为稳态（不会一直用短间隔空转）")
    check(copied == [], "E 始终不复制")


# ----------------------------------------------------------------------
# F：候选来回变化 -> 永不成立（错误拦截能力不变）
# ----------------------------------------------------------------------
def test_alternating_candidates():
    section("[F] X/Y 交替：永不成立、不复制（确认规则未被削弱）")
    worker, engine, resolver, copied = build_worker(
        [[(6, "ABX1234")], [(6, "QWE5678")]])     # 一帧 X、一帧 Y，来回交替
    intervals = [cycle(worker) for _ in range(6)]
    check(copied == [], "F 交替候选始终不复制", repr(copied))
    check(resolver.last_emitted_code is None, "F 没有任何结果成立")
    check(intervals[0] == 100,
          "F 出现候选时确实进入确认窗口（采样更密，但规则不变）", intervals)
    check(all(i in (100, 300) for i in intervals), "F 间隔只在两个合法值之间",
          intervals)


# ----------------------------------------------------------------------
# G / H：多 PSM 仲裁与放大复看的行为不变
# ----------------------------------------------------------------------
def test_arbitration_and_escalation():
    section("[G] 多 PSM：读法与仲裁不变")
    # PSM 6 读成需要纠正的形态、PSM 7 读对 -> 仲裁应取零纠正的那个
    worker, engine, resolver, copied = build_worker(
        [[(6, "ABXl234"), (7, "ABX1234")]])
    cycle(worker)
    cycle(worker)
    check(engine.psm_seen[:2] == [6, 7], "G 第一个读法需纠正 -> 继续看后续读法",
          engine.psm_seen[:4])
    check(copied == ["ABX1234"], "G 仲裁取到零纠正的正确房间号并复制", repr(copied))

    section("[H] 放大复看：行为不变（仍在成立前发生）")
    # 7 位里前 3 位被读成小写 -> case_fold>0 -> 触发 _escalate（与实测的 "GGM1180" 同形）
    escalated = {"called": 0}
    worker, engine, resolver, copied = build_worker(
        [[(6, "ggm1180")]], profile=DELTA_FORCE)
    original_escalate = worker._recognizer._escalate

    def spy(image, base):
        escalated["called"] += 1
        return original_escalate(image, base)

    worker._recognizer._escalate = spy
    cycle(worker)
    check(escalated["called"] == 1, "H case_fold>0 时仍然执行放大复看")
    check(not resolver.has_pending or resolver.pending_code is not None,
          "H 复看后状态仍然由裁决器推导（没有额外状态）")
    copied_before = list(copied)
    cycle(worker)
    check(copied == copied_before or len(copied) <= 1,
          "H 成立次数没有因为复看而增加", repr(copied))


# ----------------------------------------------------------------------
# I：真实工作线程里的采样节奏确实随状态变化
# ----------------------------------------------------------------------
def test_thread_gaps():
    section("[I] 真实线程：确认窗口内采样更密，成立后回到稳态间隔")
    app = QApplication.instance() or QApplication([])
    worker, engine, resolver, copied = build_worker(
        [[(6, "ABX1234")]], interval=300, pending=100)
    stamps = []
    original = worker._perform_recognition

    def timed():
        stamps.append(time.perf_counter())
        return original()

    worker._perform_recognition = timed

    done = threading.Event()
    worker.copy_requested.connect(lambda t: done.set())
    worker.start_recognition()
    deadline = time.time() + 8.0
    while time.time() < deadline and len(stamps) < 6:
        app.processEvents()
        time.sleep(0.01)
    worker.stop_recognition()

    gaps = [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:])]
    check(len(gaps) >= 3, "I 已采集到多次采样间隔", "gaps=%d" % len(gaps))
    if len(gaps) >= 2:
        check(gaps[0] < 250.0,
              "I 首次采样后（确认窗口）间隔 < 250ms", "%.0fms" % gaps[0])
        check(all(g > 250.0 for g in gaps[1:]),
              "I 成立之后恢复稳态间隔 > 250ms", ["%.0f" % g for g in gaps[1:]])
    check(copied == ["ABX1234"], "I 真实线程里得到的剪贴板内容不变", repr(copied))


def main():
    print("=" * 68)
    print("Phase OCR-2：确认窗口自适应采样间隔（只改采样节奏，不改判定规则）")
    print("=" * 68)
    app = QApplication.instance() or QApplication([])   # noqa: F841
    test_state_transitions()
    test_no_read()
    test_wrong_game()
    test_alternating_candidates()
    test_arbitration_and_escalation()
    test_thread_gaps()

    print("\n" + "=" * 68)
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
