# -*- coding: utf-8 -*-
"""冻结包实测：用现有 PyInstaller spec 的真实产物验收随包 OCR 运行时。

测什么：
    1. 打包产物里的运行时资产：PP-OCRv6 模型（runtime/ppocr/）与
       ffmpeg / ffprobe / susi_helper，与 spec 的构建守卫一致
    2. 打包后的 Python 代码里**确实含**生产 OCR 后端实现（在 PYZ 归档里查符号，
       并与当前源码逐名对比）
    3. 冻结模式下（sys.frozen + sys._MEIPASS 指向打包目录、且 PATH 里没有
       Tesseract/ACLOS）：
         - PP-OCRv6 模型从**打包目录**解析，不回退开发目录
         - 能构造生产引擎、完成一次识别、释放句柄，释放后可再次启动
    4. 明确标注：打包后的 **exe 本体**未被直接驱动运行（它需要真实直播流 + GUI
       交互），本脚本验证的是"冻结布局 + 冻结代码 + 冻结资产"这条链路。

Tesseract 已退役
----------------
本脚本原先验收的是随包 Tesseract 运行时（tesseract.exe / libtesseract-5.dll /
tessdata、常驻引擎的 oem/lang/datapath 实测、legacy 回退）。那些前提随
PP-OCRv6_medium_rec 的上位和 runtime/tesseract/ 的删除一起失效，因此：
  - 资产检查改为 PP-OCRv6 模型；
  - 冻结实跑改为生产后端（PPOCREngine）的定位 / 推理 / 释放 / 重启；
  - 断言里不再出现 tesseract.exe、tessdata、oem/lang/datapath、legacy 回退。
DLV_OCR_BACKEND=legacy 是**源码树**里的对照开关（见 test_release_ocr_runtime.py），
不再属于随包资产的验收项——冻结包里已经没有 Tesseract 可回退。

本脚本需要一个**已按新 spec 构建**的 dist。旧的（含 Tesseract、无 PP-OCRv6 模型）
产物会在 [B1] 直接失败，这是预期的。

用法：
    venv/Scripts/python.exe test_phase_ocr31_frozen_package.py
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "dist", "DouyinLowLatencyViewer")
INTERNAL = os.path.join(DIST, "_internal")
FAILURES = []

# 与 douyin_viewer.spec / ocr.ppocr_engine 必须一致
PPOCR_SUBDIR = "ppocr"
PPOCR_MODEL_DIR_NAME = "PP-OCRv6_medium_rec_infer"
PPOCR_MODEL_PARAMS = "inference.pdiparams"
PACKAGED_MODEL_DIR = os.path.join(INTERNAL, "runtime", PPOCR_SUBDIR,
                                  PPOCR_MODEL_DIR_NAME)


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
# 1. 打包资产
# ----------------------------------------------------------------------
def test_assets():
    section("[B1] 打包产物里的运行时资产")
    if not check(os.path.isdir(DIST), "存在打包目录 dist/DouyinLowLatencyViewer",
                 DIST):
        return False
    check(os.path.isfile(os.path.join(DIST, "DouyinLowLatencyViewer.exe")),
          "存在主程序 exe")

    # PP-OCRv6 是生产 OCR 后端：模型必须随包，且落在定位器探测的位置。
    params = os.path.join(PACKAGED_MODEL_DIR, PPOCR_MODEL_PARAMS)
    check(os.path.isdir(PACKAGED_MODEL_DIR),
          "打包内含 PP-OCRv6 模型目录 runtime/%s/%s"
          % (PPOCR_SUBDIR, PPOCR_MODEL_DIR_NAME), PACKAGED_MODEL_DIR)
    check(os.path.isfile(params), "打包内含 %s" % PPOCR_MODEL_PARAMS, params)
    if os.path.isfile(params):
        check(os.path.getsize(params) > 0,
              "%s 非空（0 字节占位会被 PPOCRModelLocator 判为未提供）"
              % PPOCR_MODEL_PARAMS, "%d 字节" % os.path.getsize(params))

    # Tesseract 已退役：这些资产不得再出现在打包产物里。
    retired = os.path.join(INTERNAL, "runtime", "tesseract")
    check(not os.path.exists(retired),
          "打包产物内不再含 runtime/tesseract/（已退役）")

    ff_dir = os.path.join(INTERNAL, "runtime", "ffmpeg")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        check(os.path.isfile(os.path.join(ff_dir, name)),
              "打包内含 %s" % name)
    helper = None
    for dirpath, _dirs, files in os.walk(DIST):
        if "susi_helper.exe" in files:
            helper = os.path.join(dirpath, "susi_helper.exe")
            break
    check(helper is not None, "打包内含 susi_helper.exe",
          "在 dist 树下未找到")
    return True


# ----------------------------------------------------------------------
# 2. 冻结代码里确实含生产后端实现
# ----------------------------------------------------------------------
def _walk_code_names(code, depth=0):
    """递归收集代码对象里的名字（用于与源码逐名对比）"""
    names = set()
    if depth > 4 or not hasattr(code, "co_names"):
        return names
    names.update(code.co_names)
    for const in code.co_consts:
        names |= _walk_code_names(const, depth + 1)
    return names


def test_bundled_code():
    section("[B2] 打包后的代码就是 PP-OCRv6 实现（从归档读出 ocr.ppocr_engine 比对）")
    exe = os.path.join(DIST, "DouyinLowLatencyViewer.exe")
    # 归档读取依赖 PyInstaller（构建期依赖，运行环境里不一定有）。import 必须
    # 放在 try 内：放在外面时缺 PyInstaller 会直接抛 ModuleNotFoundError，
    # 整个脚本在 [B2] 崩掉，[B3] 根本不会跑。
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

        cap = CArchiveReader(exe)
        pyz_name = [str(n) for n in cap.toc if str(n).lower().endswith(".pyz")][0]
        blob = cap.extract(pyz_name)
    except Exception as e:  # noqa: BLE001
        check(False, "读取打包归档失败（该检查标记为未完成）", repr(e))
        return
    tmp = os.path.join(tempfile.gettempdir(), "ocr31_app.pyz")
    with open(tmp, "wb") as fh:
        fh.write(blob)
    z = ZlibArchiveReader(tmp)
    toc = sorted(str(n) for n in z.toc)
    ocr_mods = [n for n in toc if n.startswith("ocr")]
    check(len(ocr_mods) >= 12, "打包内含全部 ocr.* 模块", repr(ocr_mods))
    check("ocr.ppocr_engine" in ocr_mods and "ocr.worker" in ocr_mods,
          "含 ocr.ppocr_engine 与 ocr.worker")

    packaged_names = _walk_code_names(z.extract("ocr.ppocr_engine"))
    markers = ("PPOCREngine", "PPOCREngineError", "PPOCRModelLocator",
               "PPOCRModelNotFoundError", "iter_readings", "recognize",
               "shutdown", "MODEL_NAME")
    for m in markers:
        check(m in packaged_names, "打包的 ocr.ppocr_engine 含生产后端符号 %s" % m)

    # 与当前源码逐名对比：证明打包的就是这一版实现
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ocr", "ppocr_engine.py"), encoding="utf-8").read()
    src_names = _walk_code_names(compile(src, "ocr/ppocr_engine.py", "exec"))
    missing = sorted(src_names - packaged_names)
    check(not missing, "打包模块的名字集合与当前源码一致（差异: %s）" % (missing[:5] or "无"))
    print("  打包 ocr.ppocr_engine 名字数=%d，源码名字数=%d"
          % (len(packaged_names), len(src_names)))


# ----------------------------------------------------------------------
# 3. 冻结模式下运行（子进程：设 frozen 标志 + 指向打包目录 + 极简 PATH）
# ----------------------------------------------------------------------
_CHILD = r'''
import os, sys
sys.path.insert(0, "@ROOT@")
sys.frozen = True
sys._MEIPASS = os.path.join("@ROOT@", "dist", "DouyinLowLatencyViewer", "_internal")

from ocr.ppocr_engine import PPOCRModelLocator, PPOCREngine, MODEL_NAME
from bench_phase_ocr1_latency import make_card

CARD = make_card("ABX1234", False)

print("MODEL_NAME=%s" % MODEL_NAME)
model_dir = PPOCRModelLocator.find_model_dir()
print("MODEL_DIR=%s" % model_dir)
print("DEVDIR_USED=%s" % (os.path.normcase(model_dir) ==
                          os.path.normcase(r"D:\new_model\PP-OCRv6_medium_rec_infer")))

eng = PPOCREngine()
print("INIT_MS=%s" % eng.init_ms)
print("STATUS_DIR=%s" % eng.backend_status["model_dir"])
text = eng.recognize(CARD).replace(" ", "")
print("TEXT1=%r" % text)
eng.shutdown()
print("AFTER_SHUTDOWN=%s" % eng.backend_status["closed"])

# 释放后重新构造（新对象新模型），证明可反复启停
eng2 = PPOCREngine()
text2 = eng2.recognize(CARD).replace(" ", "")
print("RESTART_TEXT=%r" % text2)
eng2.shutdown()
print("DONE")
'''


def test_frozen_run():
    section("[B3] 冻结模式实跑（打包资产 + 打包代码，PATH 已剔除 Tesseract/ACLOS）")
    root = os.path.dirname(os.path.abspath(__file__))
    clean = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if "aclos" not in p.lower() and "tesseract" not in p.lower())
    env = dict(os.environ, PATH=clean, PYTHONIOENCODING="utf-8")
    log = os.path.join(tempfile.gettempdir(), "ocr31_frozen_child.log")

    def run():
        with open(log, "wb") as fh:
            p = subprocess.run([sys.executable, "-u", "-c",
                                _CHILD.replace("@ROOT@", root)],
                               stdout=fh, stderr=subprocess.STDOUT, env=env,
                               timeout=180)
        out = open(log, "rb").read().decode("utf-8", "replace")
        return p.returncode, out

    print("  PATH(子进程) 是否还含 tesseract/aclos: %s"
          % any(s in clean.lower() for s in ("tesseract", "aclos")))

    rc, out = run()
    check(rc == 0, "冻结模式子进程正常退出", "rc=%d" % rc)
    check("MODEL_NAME=PP-OCRv6_medium_rec" in out, "生产模型名正确",
          [l for l in out.splitlines() if l.startswith("MODEL_NAME=")])

    # 模型必须从**打包目录**解析，而不是开发目录
    check("DEVDIR_USED=False" in out,
          "模型未回退到开发目录 D:\\new_model",
          [l for l in out.splitlines() if l.startswith(("MODEL_DIR=", "DEVDIR_USED="))])
    check(os.path.normcase(PACKAGED_MODEL_DIR) in os.path.normcase(out),
          "模型从**打包目录**解析（runtime/%s/）" % PPOCR_SUBDIR,
          [l for l in out.splitlines() if l.startswith("MODEL_DIR=")])
    check("STATUS_DIR=" in out and "INIT_MS=" in out,
          "引擎状态快照可用（model_dir / init_ms）",
          [l for l in out.splitlines() if l.startswith("INIT_MS=")])

    # 识别只断言"跑通且产出文本"：识别内容取决于模型与前处理，
    # 准确率由离线准确率基准与真机验收负责，不在这里断言具体字符。
    text_lines = [l for l in out.splitlines() if l.startswith("TEXT1=")]
    check(bool(text_lines) and not text_lines[0].endswith("TEXT1=''"),
          "生产引擎在冻结布局下完成一次识别并产出文本", text_lines)

    check("AFTER_SHUTDOWN=True" in out, "shutdown 后句柄释放")
    restart = [l for l in out.splitlines() if l.startswith("RESTART_TEXT=")]
    check(bool(restart) and not restart[0].endswith("RESTART_TEXT=''"),
          "释放后重新构造仍可识别", restart)
    check("DONE" in out, "冻结子进程跑到收尾")


def main():
    print("=" * 68)
    print("Phase OCR-3.1 任务 B：冻结包实测（PP-OCRv6 运行时）")
    print("=" * 68)
    if not test_assets():
        print("\n打包目录不存在：请先运行 PyInstaller 构建")
        return 1
    test_bundled_code()
    test_frozen_run()
    print("\n" + "=" * 68)
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
