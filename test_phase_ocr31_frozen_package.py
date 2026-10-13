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
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "dist", "DouyinLowLatencyViewer")
INTERNAL = os.path.join(DIST, "_internal")
FAILURES = []

# 与 douyin_viewer.spec / ocr.onnx_engine 必须一致
ONNX_SUBDIR = "onnx"
ONNX_MODEL_NAME = "PP-OCRv6_medium_rec_fp32.onnx"
ONNX_CHARSET_NAME = "charset.txt"
PACKAGED_ONNX_DIR = os.path.join(INTERNAL, "runtime", ONNX_SUBDIR)


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

    # ONNX Runtime 是生产 OCR 后端：两个产物必须随包，且落在定位器探测的位置。
    model = os.path.join(PACKAGED_ONNX_DIR, ONNX_MODEL_NAME)
    charset = os.path.join(PACKAGED_ONNX_DIR, ONNX_CHARSET_NAME)
    check(os.path.isdir(PACKAGED_ONNX_DIR),
          "打包内含 runtime/%s/" % ONNX_SUBDIR, PACKAGED_ONNX_DIR)
    for label, path in (("模型", model), ("字符表", charset)):
        check(os.path.isfile(path),
              "打包内含 %s %s" % (label, os.path.basename(path)), path)
        if os.path.isfile(path):
            check(os.path.getsize(path) > 0,
                  "%s 非空（0 字节占位会被 ONNXModelLocator 判为未提供）"
                  % os.path.basename(path),
                  "%d 字节" % os.path.getsize(path))

    # ONNX Runtime 的原生库由 hook-onnxruntime 收集。漏收时构建不会失败，
    # 只会打出一个 import 就崩的包，所以在这里显式核对。
    ort_capi = os.path.join(INTERNAL, "onnxruntime", "capi")
    for name in ("onnxruntime.dll", "DirectML.dll"):
        check(os.path.isfile(os.path.join(ort_capi, name)),
              "打包内含 onnxruntime/capi/%s" % name)

    # 已退役的 Paddle/CUDA 运行时不得到包里 —— 复用旧 dist/ 时它们会残留在
    # 产物树里，而「必需文件都在」发现不了这种多余。
    for relpath in (os.path.join("runtime", "tesseract"),
                    os.path.join("runtime", "ppocr"),
                    "paddle", "nvidia"):
        check(not os.path.exists(os.path.join(INTERNAL, relpath)),
              "打包产物内不再含 %s（已退役）" % relpath)

    ff_dir = os.path.join(INTERNAL, "runtime", "ffmpeg")
    # RELEASE-02-B5-CLEANUP：runtime/ffmpeg/ 下只剩 ffprobe.exe（分辨率探测回退）。
    # ffmpeg.exe 已随旧 `_uhd` 拉流链路移除；播放用外部 FFmpeg 7 运行时（不随包）。
    for name in ("ffprobe.exe",):
        check(os.path.isfile(os.path.join(ff_dir, name)),
              "打包内含 %s" % name)
    check(not os.path.exists(os.path.join(ff_dir, "ffmpeg.exe")),
          "打包产物内不再含 runtime/ffmpeg/ffmpeg.exe（旧 `_uhd` 拉流链路已移除）")
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
    section("[B2] 打包后的代码就是 ONNX 实现（从归档读出 ocr.onnx_engine 比对）")
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
    check("ocr.onnx_engine" in ocr_mods and "ocr.worker" in ocr_mods,
          "含 ocr.onnx_engine 与 ocr.worker")

    if "ocr.onnx_engine" not in toc:
        # 旧产物树（Paddle 时代）里没有这个模块：这本身就是一条失败，
        # 但要让检查**报告**它而不是抛 KeyError 中断整个用例。
        check(False, "打包归档内含 ocr.onnx_engine（缺失，无法继续比对）",
              repr([n for n in toc if n.startswith("ocr")][:8]))
        return

    packaged_names = _walk_code_names(z.extract("ocr.onnx_engine"))
    markers = ("ONNXEngine", "ONNXEngineError", "ONNXModelLocator",
               "ONNXModelNotFoundError", "ensure_session_for_roi",
               "iter_readings", "recognize", "shutdown", "MODEL_NAME",
               "REC_IMAGE_SHAPE")
    for m in markers:
        check(m in packaged_names, "打包的 ocr.onnx_engine 含生产后端符号 %s" % m)

    # 与当前源码逐名对比：证明打包的就是这一版实现
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ocr", "onnx_engine.py"), encoding="utf-8").read()
    src_names = _walk_code_names(compile(src, "ocr/onnx_engine.py", "exec"))
    missing = sorted(src_names - packaged_names)
    check(not missing, "打包模块的名字集合与当前源码一致（差异: %s）" % (missing[:5] or "无"))
    print("  打包 ocr.onnx_engine 名字数=%d，源码名字数=%d"
          % (len(packaged_names), len(src_names)))


# ----------------------------------------------------------------------
# 3. 冻结模式下运行（子进程：设 frozen 标志 + 指向打包目录 + 极简 PATH）
# ----------------------------------------------------------------------
_CHILD = r'''
import os, sys
sys.path.insert(0, "@ROOT@")
sys.frozen = True
sys._MEIPASS = os.path.join("@ROOT@", "dist", "DouyinLowLatencyViewer", "_internal")

import cv2
import numpy as np
from ocr.onnx_engine import ONNXEngine, ONNXModelLocator, MODEL_NAME
from ocr.roi_manager import ROI

# 合成自检图：与 scripts/ocr_frozen_selfcheck.py 用的是同一张，本机 ONNX 后端
# 实测逐字识别为 "AB1234"，所以这里可以断言**具体字符**而不只是"非空"——
# 字符表(charset.txt)顺序错位只会输出乱码，不会抛异常。
IMG = np.full((80, 320, 3), 245, dtype=np.uint8)
cv2.putText(IMG, "AB1234", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
            (10, 10, 10), 3)
ROI_BOX = ROI(0, 0, IMG.shape[1], IMG.shape[0], IMG.shape[1], IMG.shape[0])

print("MODEL_NAME=%s" % MODEL_NAME)
model_path = ONNXModelLocator.find_model_path()
print("MODEL_PATH=%s" % model_path)
print("CHARSET_PATH=%s" % ONNXModelLocator.find_charset_path())
# 开发兜底目录（仅非冻结环境可达）：冻结布局下绝不能命中它。
print("DEVDIR_USED=%s" % os.path.normcase(model_path).startswith(
    os.path.normcase(r"D:\TIikTok_Ocr\_onnx_export_lab")))

eng = ONNXEngine()
print("INIT_MS=%s" % eng.init_ms)
eng.ensure_session_for_roi(ROI_BOX, IMG)
print("PROVIDER=%s" % eng.backend_status["provider_actual"])
print("WIDTH=%s" % eng.pinned_width)
text = eng.recognize(IMG).replace(" ", "")
print("TEXT1=%r" % text)
eng.shutdown()
print("AFTER_SHUTDOWN=%s" % eng.backend_status["closed"])

# 释放后重新构造（新对象新 session），证明可反复启停
eng2 = ONNXEngine()
eng2.ensure_session_for_roi(ROI_BOX, IMG)
text2 = eng2.recognize(IMG).replace(" ", "")
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
    # 子进程的 stdout 与 stderr 合流，而 ONNX Runtime 会写出**不带换行**的告警
    # （它自己带 ANSI 转义），于是紧接着的 print 会被粘在那条告警的尾部，不再
    # 位于行首。所以这里一律用「任意位置匹配 NAME=value」，不要求行首 ——
    # 实测踩过：PROVIDER 与 RESTART_TEXT 被粘住，WIDTH/TEXT1 侥幸没被粘。
    def field(name):
        found = re.findall(r"%s=(\S+)" % re.escape(name), out)
        return found[-1] if found else None

    check(field("DEVDIR_USED") == "False",
          "模型未回退到开发兜底目录（_onnx_export_lab）", field("MODEL_PATH"))

    model_path = field("MODEL_PATH")
    check(bool(model_path)
          and os.path.normcase(PACKAGED_ONNX_DIR) in os.path.normcase(model_path),
          "模型从**打包目录**解析（runtime/%s/）" % ONNX_SUBDIR, model_path)

    check(field("INIT_MS") is not None and field("WIDTH") is not None,
          "引擎状态快照可用（init_ms / pinned_width）",
          "INIT_MS=%s WIDTH=%s" % (field("INIT_MS"), field("WIDTH")))

    provider = field("PROVIDER")
    check(provider in ("DmlExecutionProvider", "CPUExecutionProvider"),
          "选中了受支持的 provider（DirectML 主路径 / CPU 回退）", provider)

    # 断言**具体字符**而不只是"非空"：charset.txt 的项数或顺序错位只会把每个
    # 索引解成别的字，输出仍是"一段非空文本"，只断言非空会放过它。
    # 准确率本身由离线基准与真机验收负责，这里只钉住这一张已知答案的合成图。
    check(field("TEXT1") == "'AB1234'",
          "生产引擎在冻结布局下识别出正确文本 AB1234", field("TEXT1"))

    check(field("AFTER_SHUTDOWN") == "True", "shutdown 后句柄释放",
          field("AFTER_SHUTDOWN"))
    check(field("RESTART_TEXT") == "'AB1234'",
          "释放后重新构造仍识别出 AB1234", field("RESTART_TEXT"))
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
