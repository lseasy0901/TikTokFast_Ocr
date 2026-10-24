# -*- coding: utf-8 -*-
"""Phase OCR-1：OCR → Clipboard 延迟测量 harness（诊断用，**不改生产代码**）。

测量对象（全部走**真实生产代码路径**）：
    OCRWorker(QThread) → _perform_recognition → ROI 裁剪 → recognizer.prepare
    → engine._run_psm（每个 PSM 一次 Tesseract）→ RoomCodeRecognizer._decide
    （候选提取/逐位纠正/严格校验 + 多读法仲裁）→ _detect_conflict
    → TemporalResolver.offer（时序一致性）→ copy_requested（跨线程信号）
    → ClipboardManager.copy（真实系统剪贴板 setText）

做法：只在实例/类上**包装**若干方法记录时刻与耗时，不改任何实现、不改阈值、
不改参数。所有时间戳同源于一个 perf_counter 基准。

场景：
    clean   清晰合成房间号卡（合成字体）
    hud     低对比 + 模糊 + 噪声（更接近直播 HUD，逼出多 PSM / 纠正路径）
    wrong   ROI 里是另一款游戏的码，但用户选了这款（走「选错游戏」判定路径）
    noread  ROI 里没有房间号（无读取路径）

用法：
    python bench_phase_ocr1_latency.py --scenario clean --iters 100 --out clean.json
    python bench_phase_ocr1_latency.py --scenario hud  --iters 100 --interval 100 --out hud100.json
"""

import argparse
import json
import os
import statistics
import sys
import threading
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import ocr.room_code as room_code_mod
from ocr.clipboard import ClipboardManager
from ocr.engine import OCREngine
from ocr.profiles import DELTA_FORCE, VALORANT
from ocr.roi_manager import ROI, ROIManager
from ocr.room_code import RoomCodeRecognizer
from ocr.temporal import TemporalResolver
from ocr.worker import OCRWorker

#: 源帧尺寸（与生产一致：1080p）
FRAME_W, FRAME_H = 1920, 1080
#: ROI 尺寸：房间号卡片在画面里通常就这么大
ROI_W, ROI_H = 260, 70
ROI_X, ROI_Y = 520, 300
#: 期望码（三角洲档：3 字母 + 4 数字）
EXPECTED = "ABX1234"


# ----------------------------------------------------------------------
# 合成 ROI 图像
# ----------------------------------------------------------------------
def make_card(text: str, degraded: bool) -> np.ndarray:
    """画一张房间号卡（BGR，尺寸 = ROI 尺寸）。"""
    img = np.full((ROI_H, ROI_W, 3), 245, dtype=np.uint8)
    if degraded:
        # 低对比背景 + 轻微渐变，模拟 HUD 上的半透明底板
        img = np.full((ROI_H, ROI_W, 3), 150, dtype=np.uint8)
        for y in range(ROI_H):
            img[y, :] = np.clip(150 + (y * 30 // ROI_H), 0, 255)
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thick = 1.4, 3
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    org = ((ROI_W - tw) // 2, (ROI_H + th) // 2)
    cv2.putText(img, text, org, font, scale, (10, 10, 10), thick, cv2.LINE_AA)
    if degraded:
        img = cv2.GaussianBlur(img, (3, 3), 0.8)
        noise = np.random.default_rng(1234).normal(0, 9, img.shape)
        img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return img


def make_frame(card: np.ndarray) -> np.ndarray:
    """把卡片贴进一张 1920x1080 帧（ROI 之外填充噪声，模拟真实画面）。"""
    frame = np.full((FRAME_H, FRAME_W, 3), 40, dtype=np.uint8)
    frame[ROI_Y:ROI_Y + ROI_H, ROI_X:ROI_X + ROI_W] = card
    return frame


class FixedBuffer:
    """最小帧缓冲替身：get() 语义与 LatestFrameBuffer 一致（返回当前帧）。

    可以按固定轮次换帧：房间号变了（换房间）时，"新码出现 → 剪贴板"这一
    业务周期会重新走一遍，这样一次运行里能采到多个周期的样本。
    """

    def __init__(self, frames, rotate_every=0):
        self._frames = list(frames)
        self._rotate_every = int(rotate_every)
        self._gets = 0
        self._index = 0

    def get(self):
        if self._rotate_every > 0 and self._gets and self._gets % self._rotate_every == 0:
            self._index = (self._index + 1) % len(self._frames)
        self._gets += 1
        return self._frames[self._index]

    @property
    def frame_id(self):
        return self._gets


class Recorder:
    """所有阶段样本 + 每次迭代记录（线程安全）。"""

    def __init__(self, t0):
        self.t0 = t0
        self.lock = threading.Lock()
        self.marks_ms = {}         # 阶段 -> [ms]
        self.iterations = []       # 每次迭代一条
        self.current = None

    def ms(self):
        return (time.perf_counter() - self.t0) * 1000.0

    def add(self, stage, value_ms):
        with self.lock:
            self.marks_ms.setdefault(stage, []).append(value_ms)

    def start_iter(self):
        with self.lock:
            self.current = {"t_start": self.ms(), "ocr_calls": [], "stages": {}}
            self.iterations.append(self.current)
            return self.current

    def stage(self, name, value_ms):
        with self.lock:
            it = self.current
            if it is None:
                return
            it["stages"][name] = it["stages"].get(name, 0.0) + value_ms

    def ocr_call(self, psm, value_ms):
        with self.lock:
            if self.current is not None:
                self.current["ocr_calls"].append([psm, round(value_ms, 2)])

    def stage_offer(self, code):
        """记录本帧交给时序裁决器的候选（时序之前）"""
        with self.lock:
            if self.current is not None:
                self.current["offered"] = code

    def stage_confirmed(self, code):
        """记录本帧时序裁决正式成立（非 repeated）"""
        with self.lock:
            if self.current is not None:
                self.current["confirmed"] = code

    def finish_iter(self, **fields):
        with self.lock:
            if self.current is not None:
                self.current.update(fields)
                self.current["t_end"] = self.ms()


def peak_rss_mb():
    """本进程峰值工作集（MB）——Windows PROCESS_MEMORY_COUNTERS。"""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    try:
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                               ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return None
        return round(pmc.PeakWorkingSetSize / (1024.0 * 1024.0), 1)
    except Exception:  # noqa: BLE001
        return None


def install_wrappers(rec, worker, engine, recognizer, last_iter_end):
    """包装生产方法记录时刻（不改实现，只在外层计时）。"""
    def wrap(obj, name, stage, extra=None):
        original = getattr(obj, name)

        def wrapper(*a, **kw):
            t = time.perf_counter()
            try:
                return original(*a, **kw)
            finally:
                dt = (time.perf_counter() - t) * 1000.0
                rec.add(stage, dt)
                if extra:
                    extra(dt, *a)

        setattr(obj, name, wrapper)
        return original

    # 迭代边界
    orig_perform = worker._perform_recognition

    def perform():
        rec.start_iter()
        t = time.perf_counter()
        try:
            return orig_perform()
        finally:
            ms = (time.perf_counter() - t) * 1000.0
            rec.finish_iter(pass_ms=ms)
            last_iter_end["ms"] = rec.ms()

    worker._perform_recognition = perform

    wrap(worker, "_crop_roi", "roi")
    wrap(recognizer, "prepare", "preprocess")
    wrap(recognizer, "_decide", "arbitration")
    wrap(recognizer, "_detect_conflict", "conflict")
    wrap(recognizer, "_escalate", "escalate")
    wrap(recognizer, "_resolve_text", "resolve_text")

    # 每个 PSM 一次 Tesseract：既记阶段耗时也记调用次数
    def ocr_extra(dt, image, psm, *rest):
        rec.add("tesseract", dt)
        rec.ocr_call(psm, dt)

    wrap(engine, "_run_psm", "tesseract_total", extra=ocr_extra)

    # 纠正+严格校验核心（纯计算）
    orig_resolve = room_code_mod.resolve_candidate

    def resolve_candidate(*a, **kw):
        t = time.perf_counter()
        try:
            return orig_resolve(*a, **kw)
        finally:
            rec.add("correction_validation", (time.perf_counter() - t) * 1000.0)

    room_code_mod.resolve_candidate = resolve_candidate
    # room_code 内部用的是模块级名字，替换后需要让 _match_format 看到新函数：
    # 它通过全局查找，因此直接替换模块属性即可生效。

    # TemporalResolver.offer：记录耗时并标记「本次是否正式成立（非 repeated）」
    resolver = recognizer.resolver

    def offer(candidate):
        t = time.perf_counter()
        # 裁决器的**输入** = 本帧「已通过严格校验的候选」（时序之前）：
        # 这是「第一次得到正确候选」的正确定义
        rec.stage_offer(candidate.code if candidate is not None else None)
        out = orig_offer(candidate)
        rec.add("temporal", (time.perf_counter() - t) * 1000.0)
        if out is not None and not out.repeated:
            rec.stage_confirmed(out.code)
        return out

    orig_offer = resolver.offer
    resolver.offer = offer
    rec.orig_offer = orig_offer
    return orig_resolve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roi-image", default=None,
                    help="用**真实 ROI 截图**跑（文件路径）。给了它就不再合成卡："
                         "整幅图当作帧，ROI = 整幅图。用于真实抖音 ROI 的 A/B。")
    ap.add_argument("--scenario", default="clean",
                    choices=["clean", "hud", "wrong", "noread", "hard", "lower",
                             "alternate"])
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--backend", default="persistent",
                    choices=["persistent", "legacy"],
                    help="OCR 引擎后端（OCR-3 A/B 用）")
    ap.add_argument("--pending-interval", type=int, default=300,
                    help="确认窗口内的采样间隔（默认 300 = 与稳态相同 = 关闭自适应；"
                         "OCR-2 生产默认是 100）")
    ap.add_argument("--rotate-every", type=int, default=3,
                    help="每 N 次识别换一个房间号（0 = 不换），用于采集多个确认周期")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.scenario == "clean":
        code, degraded, profile = EXPECTED, False, DELTA_FORCE
    elif args.scenario == "hud":
        code, degraded, profile = EXPECTED, True, DELTA_FORCE
    elif args.scenario == "hard":
        # 字形容易混淆的码（I/O/S 密集）：实测 Tesseract 会读成数字形而触发
        # 逐位纠正 / 大小写折叠 → 走多 PSM + 放大复看的**慢路径**
        code, degraded, profile = "IOI1000", False, DELTA_FORCE
    elif args.scenario == "lower":
        # 小写字形（本该大写）：触发 case_fold -> 放大 2x 复看的**最慢路径**
        code, degraded, profile = "ggm180", False, DELTA_FORCE
    elif args.scenario == "alternate":
        # 两个合法码逐帧交替（X/Y/X/Y…）：时序裁决器按设计永不成立
        # —— 用来验证"采样更密"没有削弱错误拦截能力（false positive 检查）
        code, degraded, profile = "ABX1234", False, DELTA_FORCE
        args.rotate_every = 1
    elif args.scenario == "wrong":
        # ROI 里是 VALORANT 的 6 位码，但用户选的是三角洲（7 位）档
        code, degraded, profile = "IOI123", False, DELTA_FORCE
    else:
        code, degraded, profile = "HELLO", False, DELTA_FORCE   # 没有房间号

    app = QApplication.instance() or QApplication([])
    t0 = time.perf_counter()
    rec = Recorder(t0)

    # 轮换用的码表：都是各自档位的合法码
    if args.scenario == "alternate":
        codes = ["ABX1234", "QWE5678"]
    elif args.scenario == "wrong":
        codes = ["IOI123", "SAS456", "KOK789"]
    elif args.scenario == "noread":
        codes = ["HELLO", "WORLD", "TESTS"]
    elif args.scenario == "hard":
        codes = ["IOI1000", "OOO000", "SOS1234", "IOI5678"]
    elif args.scenario == "lower":
        codes = ["ggm180", "ooo000", "abc1234", "sof567"]
    else:
        codes = ["ABX1234", "QWE5678", "TRY2468", "PLM1357"]
    if args.roi_image:
        # 真实 ROI：整幅图当作"帧"，ROI 覆盖整幅图（尺寸以文件为准，不做任何假设）
        import cv2
        data = np.fromfile(args.roi_image, dtype=np.uint8)
        real = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if real is None or real.size == 0:
            print("无法读取 ROI 图片: %s" % args.roi_image)
            return {"error": "roi image unreadable"}
        frames = [real]
        roi_box = (0, 0, real.shape[1], real.shape[0])
        args.rotate_every = 0
        print("[roi-image] %s -> %dx%d" % (args.roi_image, real.shape[1], real.shape[0]))
    else:
        frames = [make_frame(make_card(c, degraded)) for c in codes]
        roi_box = (ROI_X, ROI_Y, ROI_W, ROI_H)
    buffer = FixedBuffer(frames, rotate_every=args.rotate_every)
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(roi_box[0], roi_box[1], roi_box[2], roi_box[3],
                            FRAME_W, FRAME_H))

    engine = OCREngine(backend=args.backend)
    resolver = TemporalResolver()
    recognizer = RoomCodeRecognizer(engine, profile=profile.profile_id,
                                    resolver=resolver)
    worker = OCRWorker(frame_buffer_getter=lambda: buffer,
                       roi_manager=roi_manager,
                       engine=engine,
                       interval_ms=args.interval,
                       profile=profile.profile_id,
                       recognizer=recognizer,
                       resolver=resolver,
                       # OCR-2：确认窗口内的采样间隔。等于 interval 即等价于关闭自适应
                       pending_interval_ms=args.pending_interval)

    clipboard = ClipboardManager()
    copies = []
    orig_copy = clipboard.copy
    last_iter_end = {"ms": None}

    def on_copy_requested(text):
        """与生产一致：跨线程信号在 GUI 线程执行，这里同步调用真实剪贴板写入"""
        t_start = time.perf_counter()
        ok = orig_copy(text)
        t_end = time.perf_counter()
        rec.add("clipboard", (t_end - t_start) * 1000.0)
        copies.append({
            "text": text,
            "ok": ok,
            "slot_ms": (t_start - t0) * 1000.0,
            "end_ms": (t_end - t0) * 1000.0,
            "write_ms": (t_end - t_start) * 1000.0,
            # 上一个 OCR pass 结束 → GUI 槽函数开始：跨线程信号投递 + 调度
            "after_pass_ms": ((t_start - t0) * 1000.0 - last_iter_end["ms"])
            if last_iter_end["ms"] is not None else None,
        })

    install_wrappers(rec, worker, engine, recognizer, last_iter_end)

    # 记录每次迭代的识别结果（供正确性统计与「首次正确→剪贴板」定位）
    last = {}
    orig_recognize = recognizer.recognize_frame

    def recognize_frame(image):
        out = orig_recognize(image)
        rec.finish_iter(
            code=(out.code if out is not None else None),
            corrections=(out.correction_count if out is not None else None),
            case_folds=(out.case_fold_count if out is not None else None),
            repeated=(out.repeated if out is not None else None),
            conflict=(recognizer.last_conflict is not None),
        )
        return out

    recognizer.recognize_frame = recognize_frame

    worker.copy_requested.connect(on_copy_requested)
    worker.text_updated.connect(lambda t: None)
    worker.game_mismatch.connect(lambda: rec.add("mismatch", 1.0))

    stop_requested = {"done": False}

    def check_done():
        """跑满 iters 次迭代就停（比按时间估算可靠）"""
        if stop_requested["done"]:
            return
        if len(rec.iterations) >= args.iters or time.perf_counter() - t0 > 600:
            stop_requested["done"] = True
            worker.stop_recognition()
            QTimer.singleShot(200, app.quit)

    poller = QTimer()
    poller.setInterval(100)
    poller.timeout.connect(check_done)
    poller.start()

    worker.start_recognition()
    app.exec()

    # ---------------------------------------------------------------- 统计
    def stats(vals):
        v = sorted(vals)
        if not v:
            return None
        n = len(v)

        def p(x):
            return v[min(n - 1, int((n - 1) * x))]
        return {"n": n, "mean": round(statistics.fmean(v), 2), "min": round(v[0], 2),
                "p50": round(p(0.5), 2), "p90": round(p(0.9), 2),
                "p95": round(p(0.95), 2), "p99": round(p(0.99), 2),
                "max": round(v[-1], 2),
                "std": round(statistics.pstdev(v), 2) if n > 1 else 0.0}

    iterations = rec.iterations
    ocr_calls = sum(len(it["ocr_calls"]) for it in iterations)
    offered = [it for it in iterations if it.get("offered")]
    confirmed = [it for it in iterations if it.get("confirmed")]

    # ---- 业务周期：每个「新码第一次被读出 → 时序成立 → 剪贴板」算一个样本 ----
    cycles = []
    pending = None
    for idx, it in enumerate(iterations):
        if it.get("confirmed"):
            if pending is not None and pending[1] == it["confirmed"]:
                first = iterations[pending[0]]
                cycles.append({
                    "code": it["confirmed"],
                    "first_iter": pending[0],
                    "confirm_iter": idx,
                    "first_start_ms": first["t_start"],
                    "first_pass_ms": first.get("pass_ms"),
                    "interval_wait_ms": (iterations[pending[0] + 1]["t_start"]
                                         - first["t_end"]) if pending[0] + 1 <= idx else None,
                    "second_pass_ms": it.get("pass_ms"),
                    "confirm_end_ms": it["t_end"],
                    "ocr_calls_first": len(first["ocr_calls"]),
                    "ocr_calls_second": len(it["ocr_calls"]),
                })
            pending = None
        elif it.get("offered") is not None and (pending is None
                                               or pending[1] != it["offered"]):
            pending = (idx, it["offered"])

    # 剪贴板事件按发生顺序对应到周期（每次新码成立恰好一次复制）
    for cycle, cp in zip(cycles, copies):
        cycle["clipboard_end_ms"] = cp["end_ms"]
        cycle["clipboard_write_ms"] = cp["write_ms"]
        cycle["e2e_ms"] = cp["end_ms"] - cycle["first_start_ms"]
        cycle["wait_after_first_pass_ms"] = cp["end_ms"] - (
            cycle["first_start_ms"] + (cycle["first_pass_ms"] or 0))

    warm = cycles[1:] if len(cycles) > 1 else cycles      # 去掉首次（Tesseract 冷加载）

    # 「第一次得到正确候选」= 时序裁决器第一次收到正确码的那一轮（时序之前）
    first_correct = next((it for it in iterations if it.get("offered") == code), None)
    first_confirmed = next((it for it in iterations if it.get("confirmed")), None)

    def _idx(item):
        return iterations.index(item) if item is not None else None

    ic, ifc = _idx(first_correct), _idx(first_confirmed)
    interval_wait = None
    if ic is not None and ifc is not None and ifc > ic:
        interval_wait = iterations[ic + 1]["t_start"] - first_correct["t_end"]
    second_pass = first_confirmed.get("pass_ms") if first_confirmed else None

    out = {
        "scenario": args.scenario,
        "backend": args.backend,
        "roi_image": args.roi_image,
        "peak_rss_mb": peak_rss_mb(),
        "code_in_roi": code,
        "expected_profile": profile.profile_id,
        "interval_ms": args.interval,
        "iterations": len(iterations),
        "ocr_calls": ocr_calls,
        "ocr_calls_per_iteration": round(ocr_calls / max(1, len(iterations)), 3),
        "iterations_with_candidate": len(offered),
        "iterations_confirmed": len(confirmed),
        "no_read_iterations": len(iterations) - len(offered),
        "copies": copies,
        "first_correct_iter_start_ms": first_correct["t_start"] if first_correct else None,
        "first_correct_pass_ms": first_correct.get("pass_ms") if first_correct else None,
        "first_correct_iter_end_ms": first_correct["t_end"] if first_correct else None,
        "confirmed_iter_start_ms": first_confirmed["t_start"] if first_confirmed else None,
        "confirmed_iter_end_ms": first_confirmed["t_end"] if first_confirmed else None,
        # ---- 核心业务指标：第一次看到正确码 → 剪贴板写入完成 ----
        "e2e_first_correct_to_clipboard_ms": (
            copies[0]["end_ms"] - first_correct["t_start"]
            if first_correct is not None and copies else None),
        # 扣掉「第一次识别本身」的耗时：剩下的就是等待
        "wait_after_first_correct_ms": (
            copies[0]["end_ms"] - first_correct["t_end"]
            if first_correct is not None and copies else None),
        "decomposition_ms": {
            "first_pass": first_correct.get("pass_ms") if first_correct else None,
            "interval_wait": interval_wait,
            "second_pass": second_pass,
            "clipboard_write": copies[0]["write_ms"] if copies else None,
            "after_pass_signal_hop": copies[0].get("after_pass_ms") if copies else None,
        },
        "clipboard_write_ms": stats([c["write_ms"] for c in copies]),
        # ---- 采样节奏与 CPU 代理指标 ----
        # wait_gap = 上一次识别结束 → 下一次识别开始的等待（就是 interval 那一段）
        "wait_gap_ms": stats([iterations[i + 1]["t_start"] - iterations[i]["t_end"]
                              for i in range(len(iterations) - 1)]),
        # 占空比 = 该次运行里"识别耗时"占墙钟的比例（≈ OCR 独占一个核的比例）
        "duty_cycle": ((sum(it.get("pass_ms", 0.0) for it in iterations)
                        / max(1.0, rec.ms())) if iterations else None),
        # ---- 业务周期的分布（每个新码一次）----
        "cycles": cycles,
        "cycle_count": len(cycles),
        "cycle_e2e_ms": stats([c["e2e_ms"] for c in cycles]),
        "cycle_e2e_warm_ms": stats([c["e2e_ms"] for c in warm]),
        "cycle_wait_after_first_pass_ms": stats([c["wait_after_first_pass_ms"] for c in warm]),
        "cycle_interval_wait_ms": stats([c["interval_wait_ms"] for c in warm
                                         if c["interval_wait_ms"] is not None]),
        "cycle_second_pass_ms": stats([c["second_pass_ms"] for c in warm]),
        "stages_ms": {k: stats(v) for k, v in sorted(rec.marks_ms.items())},
        "iterations_detail": iterations,
    }

    print(json.dumps({k: v for k, v in out.items() if k != "iterations_detail"},
                     ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print("[done] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
