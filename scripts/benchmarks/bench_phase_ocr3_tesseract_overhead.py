# -*- coding: utf-8 -*-
"""Phase OCR-3：Tesseract 单次调用固定开销分析（**只测量，不改生产代码**）。

A 路（生产方式）：OCREngine._run_psm → pytesseract.image_to_string
    每次调用 = numpy→PIL → 写临时图片 → 起 tesseract.exe → 加载语言模型
              → 识别 → 读回临时 txt → 删临时文件

B 路（常驻候选）：ctypes 直接调用**随包已有的** libtesseract-5.dll
    （runtime/tesseract/ 里现在就有，不引入任何新的 native 依赖）
    进程内常驻：模型只加载一次，之后每次只做 SetImage + GetUTF8Text

B' 路（无临时文件的单次调用）：tesseract stdin stdout（仍是每调用一个进程，
    用来单独量化"临时文件 I/O"的占比）

子命令：
    lifecycle   证明每次调用是否重启进程 / 重建临时文件 / 重新加载模型
    decompose   A 路各部分耗时拆解（Python 侧 / 进程启动 / 模型加载 / 识别）
    resident    B 路耗时（初始化一次 + 每次调用），并与 A 路对照
    stdin       B' 路耗时
    correctness B 路 / B' 路与 A 路输出逐字对比（多 PSM、多图、重复调用、异常输入）
    processes   进程生命周期与僵尸进程检查
    all         全部跑一遍

输入图像与 OCR-1 完全一致（同一张合成卡、同一尺寸），参数取自生产
OCREngine._build_config，保证数据可与 OCR-1/OCR-2 直接比较。
"""

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

from bench_phase_ocr1_latency import make_card          # 与 OCR-1 同一张卡
from ocr.engine import OCREngine, TesseractLocator

#: 与 OCR-1 的 ROI 尺寸一致
ROI_W, ROI_H = 260, 70
#: 与 OCR-1 一致的两类输入
CARDS = {
    "clean": "ABX1234",
    "hard": "IOI1000",
}
PSMS = [6, 7, 11]


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def stats(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)

    def p(x):
        return v[min(n - 1, int((n - 1) * x))]
    return {"n": n, "mean": round(statistics.fmean(v), 2), "min": round(v[0], 2),
            "p50": round(p(0.5), 2), "p95": round(p(0.95), 2),
            "p99": round(p(0.99), 2), "max": round(v[-1], 2),
            "std": round(statistics.pstdev(v), 2) if n > 1 else 0.0}


def timeit(fn, n):
    ts = []
    out = None
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        ts.append((time.perf_counter() - t) * 1000.0)
    return ts, out


def engine_config(psm):
    """生产同款 Tesseract 配置（取自 OCREngine._build_config）"""
    eng = OCREngine.__new__(OCREngine)          # 不触发定位逻辑
    eng.whitelist = OCREngine.DEFAULT_WHITELIST
    eng.language = "eng"
    return eng._build_config(psm)


def tesseract_exe():
    return TesseractLocator.find_tesseract()


def tessdata_dir():
    return TesseractLocator.find_tessdata(tesseract_exe())


# ----------------------------------------------------------------------
# A 路：生产方式（每次一个进程 + 临时文件）
# ----------------------------------------------------------------------
def prod_run(image, psm, engine):
    return engine._run_psm(image, psm)


# ----------------------------------------------------------------------
# B 路：常驻引擎（ctypes → 随包的 libtesseract-5.dll）
# ----------------------------------------------------------------------
class ResidentTesseract:
    """常驻 Tesseract（进程内，无子进程、无临时文件）。

    **仅用于 benchmark**：验证"把模型加载从每次调用里拿掉能省多少"。
    这是测量原型，不是生产实现（线程安全、异常恢复等尚未按生产标准打磨）。
    """

    def __init__(self, datapath, language="eng", bgr=True):
        rt = os.path.dirname(tesseract_exe())
        self._dll_path = os.path.join(rt, "libtesseract-5.dll")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.abspath(rt))
        self._dll = ctypes.CDLL(self._dll_path)
        d = self._dll
        d.TessBaseAPICreate.restype = ctypes.c_void_p
        d.TessBaseAPIInit3.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_char_p]
        d.TessBaseAPIInit3.restype = ctypes.c_int
        d.TessBaseAPISetVariable.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                             ctypes.c_char_p]
        d.TessBaseAPISetPageSegMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.TessBaseAPISetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int]
        d.TessBaseAPIGetUTF8Text.argtypes = [ctypes.c_void_p]
        d.TessBaseAPIGetUTF8Text.restype = ctypes.c_void_p
        d.TessDeleteText.argtypes = [ctypes.c_void_p]
        d.TessBaseAPIDelete.argtypes = [ctypes.c_void_p]
        d.TessBaseAPIClear.argtypes = [ctypes.c_void_p]
        self._bgr = bgr
        self._handle = d.TessBaseAPICreate()
        # 关掉 C 库自己的调试输出（否则 "Estimating resolution as N" 会把
        # stderr 刷满）；只影响日志，不影响识别。
        d.TessBaseAPISetVariable(self._handle, b"debug_file",
                                 os.devnull.encode())
        t = time.perf_counter()
        rc = d.TessBaseAPIInit3(self._handle, datapath.encode(), language.encode())
        self.init_ms = (time.perf_counter() - t) * 1000.0
        if rc != 0:
            raise RuntimeError("TessBaseAPIInit3 失败 rc=%d" % rc)

    def run(self, image, psm, whitelist, source_resolution=None):
        d, h = self._dll, self._handle
        d.TessBaseAPISetVariable(h, b"tessedit_char_whitelist",
                                 whitelist.encode())
        if source_resolution:
            d.TessBaseAPISetSourceResolution(h, int(source_resolution))
        d.TessBaseAPISetPageSegMode(h, int(psm))
        arr = np.ascontiguousarray(image)
        hgt, wid = arr.shape[:2]
        ptr = arr.ctypes.data_as(ctypes.c_void_p)
        d.TessBaseAPISetImage(h, ptr, wid, hgt, 3, wid * 3)
        p = d.TessBaseAPIGetUTF8Text(h)
        text = ctypes.string_at(p).decode("utf-8", errors="replace") if p else ""
        if p:
            d.TessDeleteText(p)
        return text.strip()

    def close(self):
        try:
            self._dll.TessBaseAPIDelete(self._handle)
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# B' 路：tesseract stdin stdout（无临时文件，仍每调用一个进程）
# ----------------------------------------------------------------------
def stdin_run(image, psm):
    import cv2
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        return ""
    cmd = [tesseract_exe(), "stdin", "stdout", "-l", "eng"] + \
        engine_config(psm).split()
    p = subprocess.run(cmd, input=buf.tobytes(), stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", errors="replace").strip()


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------
def cmd_lifecycle(args):
    """证明每次调用：新建进程、重建临时图片、重新加载模型"""
    exe = tesseract_exe()
    print("tesseract.exe =", exe)
    print("tessdata      =", tessdata_dir())
    tmpdir = tempfile.gettempdir()
    engine = OCREngine()
    card = make_card(CARDS["clean"], False)

    def tess_files():
        try:
            return sorted(f for f in os.listdir(tmpdir) if f.startswith("tess_"))
        except OSError:
            return []

    def tess_procs():
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tesseract.exe",
                                  "/NH"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL).stdout.decode(
                                     "utf-8", errors="replace")
            return out.lower().count("tesseract.exe")
        except Exception:  # noqa: BLE001
            return -1

    print("\n--- 调用前 ---")
    print("  临时 tess_* 文件: %d   tesseract.exe 进程: %d"
          % (len(tess_files()), tess_procs()))

    # 用"进程存活期间的临时文件"证明每次调用都会写临时图
    import pytesseract
    seen = {}
    orig = pytesseract.pytesseract.run_tesseract

    def spy(*a, **kw):
        # pytesseract 用关键字参数调用：input_filename / output_filename_base
        inp = kw.get("input_filename") or (a[0] if a else None)
        seen["input_exists"] = bool(inp) and os.path.isfile(inp)
        seen["input_size"] = os.path.getsize(inp) if seen["input_exists"] else 0
        seen["procs_during"] = tess_procs()
        seen["cmd"] = [tesseract_exe(), inp, kw.get("output_filename_base")]
        return orig(*a, **kw)

    pytesseract.pytesseract.run_tesseract = spy
    try:
        for _ in range(3):
            engine._run_psm(card, 6)
    finally:
        pytesseract.pytesseract.run_tesseract = orig

    print("\n--- 单次调用期间（由 pytesseract.run_tesseract 入口观测）---")
    print("  子进程内使用的临时输入文件存在: %s (大小 %d 字节)"
          % (seen.get("input_exists"), seen.get("input_size", 0)))
    print("  run_tesseract 被调用时 tesseract.exe 进程数: %s（此刻尚未 Popen）"
          % seen.get("procs_during"))

    # 采样线程：在连续调用过程中记录观察到的最大 tesseract.exe 进程数
    import threading
    samples = {"max": 0, "n": 0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            c = tess_procs()
            samples["n"] += 1
            samples["max"] = max(samples["max"], c)
            time.sleep(0.01)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    t0 = time.perf_counter()
    for _ in range(20):
        engine._run_psm(card, 6)
    elapsed = (time.perf_counter() - t0) / 20 * 1000
    stop.set()
    th.join(timeout=1)
    print("  20 次连续调用期间：采样 %d 次，观察到最多 %d 个 tesseract.exe 进程"
          "（每次调用 %.1fms：进程随调随起随灭）"
          % (samples["n"], samples["max"], elapsed))
    seen["max_procs_observed"] = samples["max"]
    seen["per_call_ms"] = round(elapsed, 1)

    print("\n--- 调用后 ---")
    print("  残留 tess_* 文件: %d   tesseract.exe 进程: %d"
          % (len(tess_files()), tess_procs()))
    print("\n结论依据：pytesseract 每次调用都会 (1) 用 PIL 写一张临时图、"
          "(2) 新起一个 tesseract.exe、(3) 读回 <base>.txt 后删除临时文件。")
    return {"temporary_files_left": len(tess_files()),
            "processes_left": tess_procs(),
            "temp_input_exists_during_call": bool(seen.get("input_exists"))}


def cmd_decompose(args):
    n = args.repeats
    exe = tesseract_exe()
    engine = OCREngine()
    card = make_card(CARDS["clean"], False)
    tiny = np.zeros((1, 1, 3), dtype=np.uint8)

    # 预写两张 PNG 供 CLI 直接读（把 Python 侧 I/O 从 CLI 计时里排除）
    import cv2
    roi_png = os.path.join(tempfile.gettempdir(), "ocr3_roi.png")
    tiny_png = os.path.join(tempfile.gettempdir(), "ocr3_tiny.png")
    cv2.imwrite(roi_png, card)
    cv2.imwrite(tiny_png, tiny)

    cfg = engine_config(6)
    res = {}

    def cli(png):
        p = subprocess.run([exe, png, "stdout", "-l", "eng"] + cfg.split(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p.returncode

    def version():
        return subprocess.run([exe, "--version"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def py_tempwrite():
        """复刻 pytesseract 的 Python 侧工作：numpy→PIL→写 PNG 临时文件。

        与 pytesseract.save() 完全同构：NamedTemporaryFile(delete=False) 生成
        基名，实际写到 "<基名>_input.<ext>"（Windows 上不能重复打开同一个文件）。
        """
        import pytesseract as pt
        img, ext = pt.pytesseract.prepare(card)
        with tempfile.NamedTemporaryFile(prefix="tess_", delete=False) as f:
            name = f.name
        input_file = "%s_input.%s" % (name, ext)
        try:
            img.save(input_file, format=img.format)
            return input_file
        finally:
            for path in (input_file, name):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    for label, fn, reps in (
            ("A 生产方式 pytesseract（1 次调用）", lambda: prod_run(card, 6, engine), n),
            ("  ├ Python 侧：PIL 转换 + 写临时图", py_tempwrite, n),
            ("  ├ 进程启动（tesseract --version）", version, n),
            ("  ├ ＋模型加载 + 极小图识别（1x1 CLI）", lambda: cli(tiny_png), n),
            ("  └ ＋真实 ROI 识别（CLI 直读 PNG）", lambda: cli(roi_png), n),
            ("B' stdin/stdout（无临时文件）", lambda: stdin_run(card, 6), n),
    ):
        ts, _ = timeit(fn, reps)
        res[label] = stats(ts)
        print("%-40s n=%-3d p50=%-7.2f p95=%-7.2f max=%-7.2f ms" % (
            label, len(ts), res[label]["p50"], res[label]["p95"], res[label]["max"]))

    a = res["A 生产方式 pytesseract（1 次调用）"]["p50"]
    py = res["  ├ Python 侧：PIL 转换 + 写临时图"]["p50"]
    sp = res["  ├ 进程启动（tesseract --version）"]["p50"]
    md = res["  ├ ＋模型加载 + 极小图识别（1x1 CLI）"]["p50"]
    roi = res["  └ ＋真实 ROI 识别（CLI 直读 PNG）"]["p50"]
    print("\n拆解（p50，ms）：")
    print("  进程启动(DLL)         ≈ %.1f" % sp)
    print("  模型加载+极小图识别    ≈ %.1f  （其中模型加载 ≈ %.1f）" % (md, md - sp))
    print("  ROI 识别增量           ≈ %.1f" % (roi - md))
    print("  Python 侧(临时文件等)  ≈ %.1f" % py)
    print("  合计 vs 实测 A         : %.1f + %.1f + %.1f + %.1f = %.1f  (实测 %.1f)"
          % (sp, md - sp, roi - md, py, sp + (md - sp) + (roi - md) + py, a))
    return res


def _bench_resident(args):
    n = args.repeats
    engine = OCREngine()
    res = {}
    rt = ResidentTesseract(tessdata_dir())
    res["init_ms"] = round(rt.init_ms, 2)
    print("B 常驻引擎初始化（模型加载一次）: %.2f ms" % rt.init_ms)
    try:
        for name, code in CARDS.items():
            card = make_card(code, False)
            for psm in PSMS:
                ts, text = timeit(lambda: rt.run(card, psm, OCREngine.DEFAULT_WHITELIST), n)
                key = "resident_%s_psm%d" % (name, psm)
                res[key] = stats(ts)
                res[key + "_text"] = text
                prod_ts, prod_text = timeit(lambda: prod_run(card, psm, engine), n)
                res["prod_%s_psm%d" % (name, psm)] = stats(prod_ts)
                res["prod_%s_psm%d_text" % (name, psm)] = prod_text
                print("%-22s psm=%-2d  A(生产)=%7.2fms  B(常驻)=%6.2fms  加速 %.1fx  %s"
                      % (name, psm, res["prod_%s_psm%d" % (name, psm)]["p50"],
                         res[key]["p50"],
                         res["prod_%s_psm%d" % (name, psm)]["p50"] / res[key]["p50"],
                         "文本一致" if prod_text == text else "文本不一致!"))

        # 1 / 3 / 6 次连续调用的累计（对应快路径 / 多 PSM / 放大复看）
        card = make_card(CARDS["clean"], False)
        for k in (1, 3, 6):
            def prod_k():
                return [prod_run(card, PSMS[i % 3], engine) for i in range(k)]

            def res_k():
                return [rt.run(card, PSMS[i % 3], OCREngine.DEFAULT_WHITELIST)
                        for i in range(k)]

            ts_a, _ = timeit(prod_k, max(3, n // 3))
            ts_b, _ = timeit(res_k, max(3, n // 3))
            res["cum_%d_prod" % k] = stats(ts_a)
            res["cum_%d_resident" % k] = stats(ts_b)
            print("连续 %d 次调用: A=%7.2fms  B=%6.2fms  （每次省 %.1fms）"
                  % (k, statistics.median(ts_a), statistics.median(ts_b),
                     statistics.median(ts_a) - statistics.median(ts_b)))
    finally:
        rt.close()
    return res


def cmd_resident(args):
    return _bench_resident(args)


def cmd_stdin(args):
    engine = OCREngine()
    res = {}
    n = args.repeats
    for name, code in CARDS.items():
        card = make_card(code, False)
        for psm in PSMS:
            ts, text = timeit(lambda: stdin_run(card, psm), n)
            res["stdin_%s_psm%d" % (name, psm)] = stats(ts)
            res["stdin_%s_psm%d_text" % (name, psm)] = text
            _, prod_text = timeit(lambda: prod_run(card, psm, engine), 3)
            print("%-6s psm=%-2d A=%7.2fms  B-stdin=%7.2fms  %s"
                  % (name, psm, statistics.median(
                      timeit(lambda: prod_run(card, psm, engine), 10)[0]),
                     res["stdin_%s_psm%d" % (name, psm)]["p50"],
                     "文本一致" if prod_text == text else "文本不一致!"))
    return res


def cmd_correctness(args):
    """B 路 / B' 路是否保持 A 路的全部能力（多 PSM、多图、重复、异常输入）"""
    engine = OCREngine()
    rt = ResidentTesseract(tessdata_dir())
    ok = True
    rows = []
    try:
        for name, code in CARDS.items():
            for psm in PSMS:
                card = make_card(code, False)
                _, a = timeit(lambda: prod_run(card, psm, engine), 3)
                b = rt.run(card, psm, OCREngine.DEFAULT_WHITELIST)
                c = stdin_run(card, psm)
                same = (a == b == c)
                ok = ok and same
                rows.append((name, psm, a, b, c, same))
                print("%-6s psm=%-2d A=%-10r B=%-10r B'=%-10r %s"
                      % (name, psm, a, b, c, "一致" if same else "不一致"))

        print("\n--- 重复调用稳定性（同一图同一 PSM 连跑 10 次）---")
        card = make_card(CARDS["clean"], False)
        outs = {rt.run(card, 7, OCREngine.DEFAULT_WHITELIST) for _ in range(10)}
        print("  常驻引擎 10 次输出集合: %r -> %s" % (outs, "稳定" if len(outs) == 1 else "不稳定"))
        ok = ok and len(outs) == 1
        outs2 = {prod_run(card, 7, engine).strip() for _ in range(10)}
        print("  生产方式 10 次输出集合: %r -> %s" % (outs2, "稳定" if len(outs2) == 1 else "不稳定"))

        print("\n--- 异常输入 ---")
        black = np.zeros((ROI_H, ROI_W, 3), dtype=np.uint8)
        a_black = prod_run(black, 6, engine)
        b_black = rt.run(black, 6, OCREngine.DEFAULT_WHITELIST)
        print("  全黑图: A=%r B=%r （都应约等于空）" % (a_black, b_black))
        ok = ok and (a_black.strip() == b_black.strip())
        odd = np.zeros((3, 300, 3), dtype=np.uint8)      # 极扁的图
        a_odd = prod_run(odd, 6, engine)
        b_odd = rt.run(odd, 6, OCREngine.DEFAULT_WHITELIST)
        print("  3x300 图: A=%r B=%r" % (a_odd, b_odd))
        ok = ok and (a_odd.strip() == b_odd.strip())

        print("\n--- whitelist / config 是否仍然生效 ---")
        latin = make_card("ABX1234", False)
        b_wl = rt.run(latin, 6, "AB")            # 只允许 A/B -> 应显著改变输出
        b_full = rt.run(latin, 6, OCREngine.DEFAULT_WHITELIST)
        print("  whitelist=AB 输出=%r ；默认 whitelist 输出=%r -> %s"
              % (b_wl, b_full, "配置生效" if b_wl != b_full else "配置未生效?"))
        ok = ok and b_wl != b_full

        print("\n--- 多次连续调用（200 次）后是否仍稳定 ---")
        card = make_card(CARDS["clean"], False)
        first = rt.run(card, 6, OCREngine.DEFAULT_WHITELIST)
        last = None
        for _ in range(200):
            last = rt.run(card, 6, OCREngine.DEFAULT_WHITELIST)
        print("  首=%r 末=%r -> %s" % (first, last, "稳定" if first == last else "漂移"))
        ok = ok and first == last
    finally:
        rt.close()
    print("\n总体：%s" % ("B 路保持 A 路全部能力" if ok else "存在差异（见上）"))
    return {"all_match": ok, "rows": rows}


def cmd_extras(args):
    """常驻方案的额外风险测量：内存占用、并发、真实业务路径累计耗时"""
    import threading
    card = make_card(CARDS["clean"], False)
    res = {}

    def rss_mb():
        """当前进程工作集（MB）——用 CSV 输出取最后一列 "Mem Usage" """
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % os.getpid(),
                              "/FO", "CSV", "/NH"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
        try:
            field = out.strip().strip('"').split('","')[-1]     # "123,456 K"
            return round(int(field.split()[0].replace(",", "")) / 1024.0, 1)
        except Exception:  # noqa: BLE001
            return None

    print("--- 内存 ---")
    before = rss_mb()
    rt = ResidentTesseract(tessdata_dir())
    after = rss_mb()
    res["rss_before_mb"] = before
    res["rss_after_init_mb"] = after
    print("  常驻引擎初始化前 RSS=%s MB，初始化后 RSS=%s MB（模型常驻内存增量 ≈ %s MB）"
          % (before, after, (after - before) if (after and before) else "?"))

    print("\n--- 并发：两个线程各自持有独立引擎 ---")
    errors = []

    def worker_thread(idx, n=15):
        try:
            eng = ResidentTesseract(tessdata_dir())
            for _ in range(n):
                if eng.run(card, 6, OCREngine.DEFAULT_WHITELIST) != "ABX1234":
                    errors.append("线程%d 输出异常" % idx)
            eng.close()
        except Exception as e:  # noqa: BLE001
            errors.append("线程%d 异常: %s" % (idx, e))

    ts = [threading.Thread(target=worker_thread, args=(i,)) for i in range(2)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    res["two_independent_engines_errors"] = errors
    res["two_independent_engines_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    print("  2 线程 × 各自引擎 × 15 次调用：总 %.0fms，错误 %s"
          % (res["two_independent_engines_ms"], errors or "无"))

    print("\n--- 并发：两个线程共享同一个引擎句柄（在子进程里测，崩溃不影响本进程）---")
    code = (
        "import sys, threading\n"
        "sys.path.insert(0, '@ROOT@')\n"
        "from bench_phase_ocr3_tesseract_overhead import ResidentTesseract, tessdata_dir, CARDS\n"
        "from bench_phase_ocr1_latency import make_card\n"
        "from ocr.engine import OCREngine\n"
        "card = make_card(CARDS['clean'], False)\n"
        "print('child: engine init', flush=True)\n"
        "rt = ResidentTesseract(tessdata_dir())\n"
        "bad = []\n"
        "def run():\n"
        "    for _ in range(40):\n"
        "        t = rt.run(card, 6, OCREngine.DEFAULT_WHITELIST)\n"
        "        if t != 'ABX1234': bad.append(repr(t))\n"
        "ts = [threading.Thread(target=run) for _ in range(2)]\n"
        "[t.start() for t in ts]; [t.join() for t in ts]\n"
        "print('SHARED_RESULT bad=%d' % len(bad), bad[:3], flush=True)\n"
    ).replace("@ROOT@", os.path.abspath("."))
    # 子进程输出重定向到**文件**（不用管道：万一子进程卡死，管道读取会一起挂住）
    child_log = os.path.join(tempfile.gettempdir(), "ocr3_shared_child.log")
    with open(child_log, "wb") as fh:
        proc = subprocess.Popen([sys.executable, "-u", "-c", code],
                                stdout=fh, stderr=subprocess.STDOUT)
    try:
        proc.wait(timeout=90)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    with open(child_log, "rb") as fh:
        child_out = fh.read().decode("utf-8", "replace").strip()
    res["shared_handle_timed_out"] = timed_out
    res["shared_handle_exit_code"] = proc.returncode
    res["shared_handle_output"] = child_out[-300:]
    print("  共享句柄、2 线程 × 40 次并发：%s（退出码 %s）"
          % ("**90s 未返回 = 卡死**" if timed_out else "正常结束", proc.returncode))
    print("  子进程输出: %r" % child_out[-200:])
    if timed_out:
        rt.close()
        return res
    p = type("R", (), {"returncode": proc.returncode, "stdout": child_out.encode(),
                       "stderr": b""})()
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    res["shared_handle_exit_code"] = p.returncode
    res["shared_handle_stdout"] = out.strip()[-300:]
    res["shared_handle_stderr"] = err.strip()[-300:]
    print("  退出码=%s  输出=%r  错误尾部=%r"
          % (p.returncode, res["shared_handle_stdout"][-120:],
             res["shared_handle_stderr"][-160:]))
    rt.close()

    print("\n--- 真实业务路径累计耗时（同样的调用次数，A vs B）---")
    engine = OCREngine()
    rows = {}
    for k, label in ((2, "快路径：2 次识别 × 1 个 PSM"),
                     (6, "多读法：2 次识别 × 3 个 PSM"),
                     (12, "放大复看：2 次识别 × 6 次调用")):
        def a_k():
            return [prod_run(card, PSMS[i % 3], engine) for i in range(k)]

        def b_k():
            import itertools
            rt2 = ResidentTesseract(tessdata_dir())
            try:
                return [rt2.run(card, PSMS[i % 3], OCREngine.DEFAULT_WHITELIST)
                        for i in range(k)]
            finally:
                rt2.close()

        ta, _ = timeit(a_k, 5)
        tb, _ = timeit(b_k, 5)
        rows[k] = {"prod_p50": stats(ta)["p50"], "resident_p50": stats(tb)["p50"]}
        print("  %-26s %d 次：A=%7.1fms  B=%6.1fms（B 含每次重建引擎的 %.0fms 初始化）"
              % (label, k, rows[k]["prod_p50"], rows[k]["resident_p50"], 56.5))
        rows[k]["stripped_resident_p50"] = round(
            rows[k]["resident_p50"] - 56.5, 2)
    res["business_paths"] = rows
    return res


def cmd_processes(args):
    def count():
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tesseract.exe", "/NH"],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
        return out.lower().count("tesseract.exe")

    engine = OCREngine()
    card = make_card(CARDS["clean"], False)
    res = {}
    print("起始 tesseract.exe 进程数: %d" % count())
    res["before"] = count()

    for _ in range(10):
        engine._run_psm(card, 6)
    res["after_prod"] = count()
    print("A 路 10 次调用后进程数: %d（每次调用起一个进程，正常退出不留残）" % res["after_prod"])

    for _ in range(10):
        stdin_run(card, 6)
    res["after_stdin"] = count()
    print("B' 路 10 次调用后进程数: %d" % res["after_stdin"])

    rt = ResidentTesseract(tessdata_dir())
    for _ in range(10):
        rt.run(card, 6, OCREngine.DEFAULT_WHITELIST)
    res["during_resident"] = count()
    print("B 路（常驻）10 次调用后进程数: %d（进程内引擎，不产生任何子进程）"
          % res["during_resident"])
    rt.close()
    res["after_resident_close"] = count()
    print("B 路关闭后进程数: %d" % res["after_resident_close"])
    return res


# ----------------------------------------------------------------------
# B'' 路：常驻 helper 进程（模型加载一次 + 保持崩溃隔离）
# ----------------------------------------------------------------------
_HELPER_SRC = r'''
import struct, sys
sys.path.insert(0, "@ROOT@")
import numpy as np
from bench_phase_ocr3_tesseract_overhead import ResidentTesseract, tessdata_dir
rt = ResidentTesseract(tessdata_dir())
print("READY", flush=True)
inp, out = sys.stdin.buffer, sys.stdout.buffer
while True:
    head = inp.read(20)
    if len(head) < 20:
        break
    w, h, psm, wl_len, n = struct.unpack("<5i", head)
    wl = inp.read(wl_len).decode()
    img = np.frombuffer(inp.read(n), dtype="uint8").reshape((h, w, 3))
    b = rt.run(img, psm, wl).encode("utf-8")
    out.write(struct.pack("<i", len(b)) + b); out.flush()
'''


def cmd_helper(args):
    """常驻 helper 进程：模型只加载一次，且识别崩溃不会带走主进程"""
    import struct
    root = os.path.abspath(".")
    src = _HELPER_SRC.replace("@ROOT@", root)
    child_err = os.path.join(tempfile.gettempdir(), "ocr3_helper_err.log")
    err_fh = open(child_err, "wb")
    proc = subprocess.Popen([sys.executable, "-u", "-c", src],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=err_fh)
    t0 = time.perf_counter()
    ready = proc.stdout.readline()
    init_ms = (time.perf_counter() - t0) * 1000.0
    print("B'' 常驻 helper：启动 + 模型加载一次 = %.1f ms（%s）"
          % (init_ms, ready.decode().strip()))

    card = make_card(CARDS["clean"], False)
    h, w = card.shape[:2]
    wl = OCREngine.DEFAULT_WHITELIST.encode()
    payload = card.tobytes()
    res = {"helper_init_ms": round(init_ms, 1)}
    for psm in PSMS:
        header = struct.pack("<5i", w, h, psm, len(wl), len(payload))  # 20 字节
        ts = []
        text = None
        for _ in range(args.repeats):
            t = time.perf_counter()
            proc.stdin.write(header); proc.stdin.write(wl); proc.stdin.write(payload)
            proc.stdin.flush()
            n = struct.unpack("<i", proc.stdout.read(4))[0]
            text = proc.stdout.read(n).decode("utf-8")
            ts.append((time.perf_counter() - t) * 1000.0)
        res["helper_psm%d" % psm] = stats(ts)
        res["helper_psm%d_text" % psm] = text.strip()
        print("  psm=%-2d 每次往返 p50=%-6.2f p95=%-6.2f max=%-6.2f ms  输出=%r"
              % (psm, res["helper_psm%d" % psm]["p50"],
                 res["helper_psm%d" % psm]["p95"],
                 res["helper_psm%d" % psm]["max"], text.strip()))
    proc.stdin.close()
    proc.wait(timeout=10)
    res["helper_exit_code"] = proc.returncode
    print("  helper 退出码=%s（stdin 关闭后自行退出，无残留）" % proc.returncode)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", required=True,
                    choices=["lifecycle", "decompose", "resident", "stdin",
                             "correctness", "processes", "extras", "helper", "all"])
    ap.add_argument("--repeats", type=int, default=30)
    ap.add_argument("--out")
    args = ap.parse_args()

    out = {}
    if args.cmd in ("lifecycle", "all"):
        out["lifecycle"] = cmd_lifecycle(args)
    if args.cmd in ("decompose", "all"):
        out["decompose"] = cmd_decompose(args)
    if args.cmd in ("resident", "all"):
        out["resident"] = _bench_resident(args)
    if args.cmd in ("stdin", "all"):
        out["stdin"] = cmd_stdin(args)
    if args.cmd in ("correctness", "all"):
        out["correctness"] = cmd_correctness(args)
    if args.cmd in ("processes", "all"):
        out["processes"] = cmd_processes(args)
    if args.cmd in ("extras", "all"):
        out["extras"] = cmd_extras(args)
    if args.cmd in ("helper", "all"):
        out["helper"] = cmd_helper(args)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print("\n[done] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

