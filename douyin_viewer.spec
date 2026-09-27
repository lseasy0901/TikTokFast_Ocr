# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Douyin Low Latency Viewer
Windows standalone EXE packaging
"""

import glob
import importlib.util
import os
import sys

# Get the absolute path of the project directory
PROJECT_ROOT = os.path.abspath(".")

# ---------------------------------------------------------------------------
# Client version (Phase 0)
#
# Single source of truth: utils/version.py. It is exec'd rather than imported so
# that this spec keeps working in a build environment that has no runtime
# dependencies installed -- the module holds one literal assignment and no
# imports by design. Never write a version string a second time here: the client
# reads the same file at runtime, and the update check compares the two.
# ---------------------------------------------------------------------------
_APP_VERSION_FILE = os.path.join(PROJECT_ROOT, "utils", "version.py")
_version_ns = {}
with open(_APP_VERSION_FILE, "r", encoding="utf-8") as _f:
    exec(_f.read(), _version_ns)
APP_VERSION = _version_ns["__version__"]

print("=" * 72)
print("Building DouyinLowLatencyViewer %s" % APP_VERSION)
print("=" * 72)

# ---------------------------------------------------------------------------
# Required build inputs (Phase 7.2-8G)
#
# Some of the bundled artifacts are deliberately NOT in Git -- they are gitignored
# and provisioned per deployment or built locally:
#
#   license_public_key.pem                        (.gitignore:22)
#   susi_helper/target/release/susi_helper.exe    (Rust target tree, gitignored)
#   runtime/ffmpeg/ffmpeg.exe                     (large third-party binary)
#   runtime/ffmpeg/*.dll                          (their required companion DLLs)
#   runtime/ppocr/PP-OCRv6_medium_rec_infer/      (production OCR model, large)
#
# PyInstaller only *warns* when a `datas` source is missing and then produces an
# EXE anyway. Each of those omissions is invisible here and fatal on a customer's
# machine: no helper -> activation never works; no FFmpeg -> the video pipeline
# dies at spawn; no ffprobe -> the resolution probe silently degrades to a wrong
# fixed frame size; no PP-OCRv6 model -> the production OCR backend cannot load.
# Verify all of them up front and stop the build.
#
# The Tesseract set (runtime/tesseract/tesseract.exe, its ~26 companion DLLs and
# tessdata/eng.traineddata) used to be on this list. It is gone as of the
# PP-OCRv6_medium_rec migration: that is now the production OCR backend, and the
# assets were deleted from the repository so they no longer occupy project space.
# ocr.engine.OCREngine / TesseractLocator are retained only as the diagnostic and
# DLV_OCR_BACKEND=legacy comparison path, and have no bundled runtime to resolve
# to in a frozen build.
#
# config.yaml is intentionally not in this list: it is tracked in Git, so it
# cannot go missing from a clone.
#
# runtime/ffmpeg/ffprobe.exe is bundled (and tracked) as of the 1.0.1 resolution
# fix, replacing the 0-byte placeholder that used to sit there. It is NOT
# optional: utils.runtime_paths.resolve_ffprobe_path() has no PATH fallback in a
# frozen bundle, so without it get_video_info() reports no resolution and
# gui/main_window.py falls back to a hardcoded 1920x1080 -- while
# stream/douyin.py._pick_url() selects Douyin's HD1 tier, which is frequently not
# 1920x1080 (the room used to verify this fix is a 720x1280 portrait stream).
# FFmpegReader then read 6,220,800-byte frames out of a 2,764,800-byte-frame pipe:
# the rawvideo stream desynchronizes into a tiled/repeated picture.
#
# ffprobe.exe is the ACLOS *shared* FFmpeg build (n5.1.3, --enable-shared), a
# different build from ffmpeg.exe above, and puts the av*/sw* libraries in
# _FFPROBE_DLL_NAMES in its import table. Shipping the exe alone reproduces the
# exact failure class this list exists to prevent: with no ACLOS install on PATH
# it dies at process start with "error while loading shared libraries"
# (0xC0000135) and the app falls back to 1920x1080 anyway. Hence the closure
# requirement below.
# ---------------------------------------------------------------------------
_PUBLIC_KEY_SRC = os.path.join(PROJECT_ROOT, "license_public_key.pem")
_SUSI_HELPER_SRC = os.path.join(
    PROJECT_ROOT, "susi_helper", "target", "release", "susi_helper.exe"
)
_SUSI_HELPER_DEST = os.path.join("susi_helper", "target", "release")

# runtime/ assets. The destinations mirror what the client resolves at runtime:
#   utils.runtime_paths.resolve_ffmpeg_path()   -> <MEIPASS>/runtime/ffmpeg/ffmpeg.exe
# which is why ffmpeg.exe must keep this exact relative path.
_FFMPEG_DIR = os.path.join(PROJECT_ROOT, "runtime", "ffmpeg")
_FFMPEG_SRC = os.path.join(_FFMPEG_DIR, "ffmpeg.exe")
_FFMPEG_DEST = os.path.join("runtime", "ffmpeg")

# ffprobe.exe is the *shared* ACLOS FFmpeg build (n5.1.3) and is a different
# build from ffmpeg.exe. It resolves to <MEIPASS>/runtime/ffmpeg/ffprobe.exe via
# utils.runtime_paths.resolve_ffprobe_path(), which is why it must land in
# _FFMPEG_DEST beside ffmpeg.exe.
_FFPROBE_SRC = os.path.join(_FFMPEG_DIR, "ffprobe.exe")

# ffprobe.exe's companion set, required by name. These live in the same directory
# as ffmpeg.exe's DLLs, so the two closures are told apart by name: every entry
# here is subtracted from the ffmpeg glob below, which keeps the "FFmpeg runtime
# is incomplete" count meaning exactly what it meant before.
_FFPROBE_DLL_NAMES = (
    "avformat-59.dll",  # core library -- ffprobe links the format layer directly
    "avcodec-59.dll",
    "avdevice-59.dll",
    "avfilter-8.dll",
    "avutil-57.dll",
    "swresample-4.dll",
    "swscale-6.dll",
)
_FFPROBE_CORE = os.path.join(_FFMPEG_DIR, _FFPROBE_DLL_NAMES[0])
_FFPROBE_DLLS = [
    os.path.join(_FFMPEG_DIR, name)
    for name in _FFPROBE_DLL_NAMES
    if os.path.isfile(os.path.join(_FFMPEG_DIR, name))
]

# ffmpeg.exe is not a stock FFmpeg build -- it is the ACLOS recorder's, and it
# hard-links against its sibling DLLs: libpag.dll and o264rtenc.dll first, then
# the ANGLE (libEGL/libGLESv2) and MSVC runtimes they pull in. Those are ordinary
# imports, not delay-loads, so the loader demands them at process start; without
# them ffmpeg.exe dies with STATUS_DLL_NOT_FOUND (0xC0000135) before it parses a
# single argument. The build only ever worked because the developer machine had
# C:\Program Files (x86)\ACLOS\Cross\recorder-release on PATH. Vendored beside
# ffmpeg.exe and gitignored (.gitignore).
_FFMPEG_DLL_REQUIRED = os.path.join(_FFMPEG_DIR, "libpag.dll")
_FFMPEG_DLLS = sorted(
    path
    for path in glob.glob(os.path.join(_FFMPEG_DIR, "*.dll"))
    if os.path.basename(path).lower() not in _FFPROBE_DLL_NAMES
)
_FFMPEG_DLL_MIN = 7

# ---------------------------------------------------------------------------
# PP-OCRv6 model (the production OCR backend)
#
# PP-OCRv6_medium_rec replaced the retired Tesseract runtime as the production
# OCR model, so the build now has to ship the model itself. It must land exactly
# where the client probes for it:
#
#   ocr.ppocr_engine.PPOCRModelLocator._bundled_candidates()
#       -> <base>/runtime/ppocr/PP-OCRv6_medium_rec_infer
#
# where <base> is sys._MEIPASS in a frozen onedir build -- the same contract the
# retired ocr.engine.TesseractLocator had for runtime/tesseract/.
#
# The predicate below mirrors PPOCRModelLocator.is_usable_model_dir() rather than
# calling it: this spec deliberately runs in a build interpreter that may have no
# runtime dependencies installed, so it must not import the ocr package. "Usable"
# is therefore restated in the same terms the locator uses (directory exists and
# inference.pdiparams is present and non-empty), because a 0-byte placeholder
# must never count as provisioned -- that is how the Tesseract set used to be
# faked into a build.
#
# The bundling itself used to be unwritten, and the "PP-OCRv6 packaging phase"
# note here said so. It is written now -- the model directory, the Paddle runtime
# and the CUDA closure all land in `datas` below (see the Phase 1.1 block further
# down for why each one has to be listed by hand):
#   1. the model goes to _PPOCR_MODEL_DIR, which the gate above already requires,
#   2. (_PPOCR_MODEL_DIR, _PPOCR_DEST) puts it on the frozen probe path,
#   3. paddlepaddle-gpu plus the nvidia-* CUDA wheels that
#      ocr.ppocr_engine._ensure_cuda_dlls() puts on the DLL search path are
#      collected out of the build environment and shipped alongside it.
# Until (1) happens the gate below fails the build, which is the point: an EXE
# without the model reports "OCR 组件缺失" on every customer machine.
# ---------------------------------------------------------------------------
_PPOCR_SUBDIR = "ppocr"                      # mirrors ocr.ppocr_engine._PPOCR_SUBDIR
_PPOCR_MODEL_DIR_NAME = "PP-OCRv6_medium_rec_infer"
_PPOCR_MODEL_DIR = os.path.join(PROJECT_ROOT, "runtime", _PPOCR_SUBDIR,
                                _PPOCR_MODEL_DIR_NAME)
_PPOCR_MODEL_PARAMS = os.path.join(_PPOCR_MODEL_DIR, "inference.pdiparams")
_PPOCR_DEST = os.path.join("runtime", _PPOCR_SUBDIR)

# PyInstaller copies a *directory* datas source as "dest + the files' paths
# relative to that source directory" -- the source directory's own name is
# dropped. So dest=_PPOCR_DEST would flatten the three model files into
# runtime/ppocr/ and the locator's probe path
# (<base>/runtime/ppocr/PP-OCRv6_medium_rec_infer) would find nothing. The
# model directory name has to be part of the destination.
# Measured, not assumed: a build with dest=_PPOCR_DEST produced exactly
# _internal/runtime/ppocr/inference.pdiparams.
_PPOCR_MODEL_DEST = os.path.join(_PPOCR_DEST, _PPOCR_MODEL_DIR_NAME)


def require_build_inputs(required):
    """Abort the build when a required bundled artifact is absent.

    `required` is a sequence of (label, path) pairs. Raises FileNotFoundError
    naming every missing path exactly, so the fix is unambiguous.
    """
    missing = [(label, path) for label, path in required if not os.path.isfile(path)]
    if not missing:
        return

    report = [
        "",
        "=" * 72,
        "BUILD ABORTED: required file(s) are missing.",
        "",
        "PyInstaller would otherwise emit an EXE that is broken on a clean machine:",
        "no helper -> activation never works; no FFmpeg -> the video pipeline dies",
        "at spawn; no ffprobe -> frames are sized from a hardcoded resolution.",
        "",
    ]
    for label, path in missing:
        report += ["  missing %s" % label, "    %s" % path, ""]

    report += [
        "Looked under: %s" % PROJECT_ROOT,
        '(this spec uses os.path.abspath("."), so run the build from the',
        " repository root)",
        "",
        "How to provision:",
        "  license_public_key.pem",
        "      copy the deployment's public key to %s" % _PUBLIC_KEY_SRC,
        "  susi_helper",
        "      run `cargo build --release` in susi_helper/, or copy the pinned",
        "      prebuilt binary to %s" % _SUSI_HELPER_SRC,
        "  runtime/ffmpeg/ffmpeg.exe",
        "      copy the FFmpeg build to %s" % _FFMPEG_SRC,
        "      plus its %d companion DLLs (ffmpeg.exe is the ACLOS build, not a" % _FFMPEG_DLL_MIN,
        "      stock one; without them it fails with STATUS_DLL_NOT_FOUND)",
        "      e.g. from C:\\Program Files (x86)\\ACLOS\\Cross\\recorder-release\\*.dll",
        "  runtime/ffmpeg/ffprobe.exe",
        "      copy that build's ffprobe.exe to %s" % _FFPROBE_SRC,
        "      plus its %d companion DLLs (%s)." % (
            len(_FFPROBE_DLL_NAMES), ", ".join(_FFPROBE_DLL_NAMES)),
        "      ffprobe.exe is the shared ACLOS build, not ffmpeg.exe's; without",
        "      those it dies at process start (0xC0000135) and the app silently",
        "      falls back to a hardcoded 1920x1080 frame size.",
        "  runtime/ppocr/",
        "      copy the %s model directory to %s" % (
            _PPOCR_MODEL_DIR_NAME, _PPOCR_MODEL_DIR),
        "      (inference.pdiparams must be present and non-empty).",
        "      PP-OCRv6_medium_rec is the production OCR backend; the Paddle/PaddleX",
        "      runtime that loads it is supplied by the PP-OCRv6 packaging phase.",
        "=" * 72,
        "",
    ]
    raise FileNotFoundError("\n".join(report))


require_build_inputs(
    (
        ("license_public_key.pem", _PUBLIC_KEY_SRC),
        ("susi_helper executable", _SUSI_HELPER_SRC),
        ("FFmpeg executable", _FFMPEG_SRC),
        ("FFmpeg runtime library", _FFMPEG_DLL_REQUIRED),
        ("FFprobe executable", _FFPROBE_SRC),
        ("FFprobe runtime library", _FFPROBE_CORE),
        ("PP-OCRv6 model", _PPOCR_MODEL_PARAMS),
    )
)

# pytesseract is an *importable* dependency, so require_build_inputs() (which only
# stats files) cannot see this failure. PyInstaller treats a missing hidden import
# as a warning, not an error: the build succeeds, ships no pytesseract, and every
# customer sees "OCR 组件缺失" the moment they press 识别. That exact defect
# shipped once; fail the build instead of rediscovering it on a user's machine.
#
# The check runs in the spec's own interpreter, which is the same one PyInstaller
# uses to walk the import graph, so "resolves here" == "gets bundled".
_pytesseract_missing = []
for _mod in ("pytesseract", "PIL.Image"):
    if importlib.util.find_spec(_mod) is None:
        _pytesseract_missing.append(_mod)

if _pytesseract_missing:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the OCR Python dependency is missing.",
                "",
                "  not importable in the build interpreter (%s):"
                % sys.executable,
                "    %s" % ", ".join(_pytesseract_missing),
                "",
                "pytesseract is the entry point to the legacy OCREngine path",
                "(DLV_OCR_BACKEND=legacy) and is still declared in requirements.txt.",
                "PyInstaller only *warns* about an unresolvable hidden import, so",
                "without this guard the build succeeds and produces an installer",
                "whose OCR never starts on any machine -- including this one.",
                "PIL.Image is listed alongside it because pytesseract imports it at",
                "module scope; without it the frozen import fails the same way.",
                "",
                "Fix: install the runtime requirements into the build environment",
                "  python -m pip install -r requirements.txt",
                "=" * 72,
                "",
            ]
        )
    )

# A present-but-partial DLL set is the case require_build_inputs() cannot see.
# ffmpeg.exe needs all of them at process start, so a partial set is as fatal as
# none.
if len(_FFMPEG_DLLS) < _FFMPEG_DLL_MIN:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the FFmpeg runtime is incomplete.",
                "",
                "  found %d DLL(s) in %s" % (len(_FFMPEG_DLLS), _FFMPEG_DIR),
                "  expected at least %d" % _FFMPEG_DLL_MIN,
                "",
                "ffmpeg.exe is the ACLOS recorder's build and links against these",
                "DLLs at process start (libpag.dll, o264rtenc.dll, libEGL/libGLESv2,",
                "msvcp140, vcruntime140[_1]). A partial set builds an EXE whose video",
                "pipeline dies with STATUS_DLL_NOT_FOUND -- which looks like a stream",
                "problem, not a packaging problem.",
                "",
                "Provision them from an ACLOS install, e.g.",
                '  copy "C:\\Program Files (x86)\\ACLOS\\Cross\\recorder-release\\*.dll" %s'
                % _FFMPEG_DIR,
                "=" * 72,
                "",
            ]
        )
    )

# Same reasoning for ffprobe.exe, which is a different build with its own closure.
# A partial set is as fatal as none: the exe dies with STATUS_DLL_NOT_FOUND
# (0xC0000135), get_video_info() reports no resolution, and the app falls back to
# the hardcoded 1920x1080 frame size this fix exists to remove.
if len(_FFPROBE_DLLS) < len(_FFPROBE_DLL_NAMES):
    _present = {os.path.basename(path).lower() for path in _FFPROBE_DLLS}
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the ffprobe runtime is incomplete.",
                "",
                "  found %d of %d DLL(s) in %s" % (
                    len(_FFPROBE_DLLS), len(_FFPROBE_DLL_NAMES), _FFMPEG_DIR),
                "  missing: %s" % ", ".join(
                    name for name in _FFPROBE_DLL_NAMES if name not in _present),
                "",
                "ffprobe.exe is the shared ACLOS FFmpeg build (n5.1.3) and imports",
                "these libraries at process start. Without the full set it dies",
                "before it parses a single argument on any machine that has no",
                "ACLOS install on PATH -- and the resolution probe silently",
                "degrades to a hardcoded 1920x1080 frame size, which does not",
                "match the selected Douyin tier. The rawvideo stream then",
                "desynchronizes into a tiled/repeated picture.",
                "",
                "Provision them from an ACLOS install, e.g.",
                '  copy "C:\\Program Files (x86)\\ACLOS\\Cross\\recorder-release\\ffprobe.exe" %s'
                % _FFPROBE_SRC,
                '  copy "C:\\Program Files (x86)\\ACLOS\\Cross\\recorder-release\\av*.dll" %s'
                % _FFMPEG_DIR,
                '  copy "C:\\Program Files (x86)\\ACLOS\\Cross\\recorder-release\\sw*.dll" %s'
                % _FFMPEG_DIR,
                "=" * 72,
                "",
            ]
        )
    )

# A present-but-empty PP-OCRv6 model is the case require_build_inputs() cannot
# see: it only stats for existence. PPOCRModelLocator.is_usable_model_dir() is
# what the client actually applies, and it rejects a 0-byte inference.pdiparams,
# so a placeholder must fail the build here rather than ship an EXE that cannot
# load its production OCR backend.
if os.path.isfile(_PPOCR_MODEL_PARAMS) and os.path.getsize(_PPOCR_MODEL_PARAMS) == 0:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the PP-OCRv6 model is empty.",
                "",
                "  %s" % _PPOCR_MODEL_PARAMS,
                "  is a 0-byte placeholder; expected the model's real weights.",
                "",
                "PPOCRModelLocator.is_usable_model_dir() treats a 0-byte",
                "inference.pdiparams as 'not found', so this build would produce an",
                "EXE whose OCR never starts -- which looks like an OCR bug, not a",
                "packaging bug.",
                "",
                "How to provision:",
                "  copy the %s model directory to %s" % (
                    _PPOCR_MODEL_DIR_NAME, _PPOCR_MODEL_DIR),
                "=" * 72,
                "",
            ]
        )
    )

# ---------------------------------------------------------------------------
# Paddle / PaddleX runtime + CUDA closure (Phase 1.1)
#
# 1.1.1 的内部包是这个缺陷的实例：构建机上 GPU 栈一应俱全，发行包里却
# 既没有 PP-OCRv6 模型，也没有任何 CUDA DLL —— 客户机上 OCR 一次都跑不起来。
# 原因不是哪个路径写错了，而是 **PyInstaller 的依赖分析本来就看不见它们**：
#
#   * PP-OCRv6 模型是纯数据，没有任何 import 指向它，datas 漏写只警告不报错；
#   * paddle/libs 下的 mklml / libblas / liblapack / libgfortran / libquadmath /
#     warpctc / warprnnt 是 phi.dll 与 libpaddle.pyd 用 LoadLibrary 在运行期
#     打开的，不在 PE 导入表里。实测导入表：libpaddle.pyd 只静态导入
#     phi.dll 与 python311.dll，phi.dll 只静态导入
#     common.dll / mkldnn.dll / libiomp5md.dll —— 所以 PyInstaller 只收到这四个；
#   * nvidia-* wheel 的 CUDA 库既不是 Python 模块也不是静态依赖，paddle 靠
#     os.add_dll_directory 拼路径去找，分析器完全看不见。
#
# 目标目录不是随便选的，是运行期查找路径的硬要求：
#   * 模型 -> <MEIPASS>/runtime/ppocr/PP-OCRv6_medium_rec_infer
#       （ocr.ppocr_engine.PPOCRModelLocator._bundled_candidates()）
#   * CUDA -> <MEIPASS>/nvidia/...（保持 wheel 里的原相对路径）
#       ocr.ppocr_engine._ensure_cuda_dlls() 在 site.getsitepackages() 下
#       glob "nvidia/cu13/bin/*"。冻结环境里 site.getsitepackages() 的根
#       **就是** sys._MEIPASS —— 已用一个最小冻结探针实测确认（PyInstaller
#       6.22.2 onedir 下 sys.prefix == sys.exec_prefix == sys._MEIPASS），
#       所以只要文件按原相对路径落进 _internal/nvidia/...，这个函数在冻结包
#       里照样命中，无需改动 OCR 代码。paddle 自己找 cudnn 用的是
#       <paddle>/../nvidia/<pkg>/bin —— 同一个根。
#   * paddle/libs -> <MEIPASS>/paddle/libs（paddle 自己 glob 这个目录）
#
# 为什么 CUDA 闭包整目录收、不挑子集：Phase 1 审计区分了「实测被生产 OCR
# 链路加载」（cudnn 8 个 + cublas 2 个，906.5 MiB）与「实测被 paddle 其它
# 算子按需加载」（cufft / cusolver / cusparse / nvJitLink，632.2 MiB）。
# 裁剪需要在剥离后重测，属于高风险实验，本阶段明确不做，因此整目录收。
# ---------------------------------------------------------------------------
_PADDLE_LIBS_DEST = os.path.join("paddle", "libs")

#: PyInstaller 的 PE 依赖分析**已经能收到**的 paddle/libs DLL（见上面的实测
#: 导入表）。它们由 BINARY TOC 落到 paddle/libs/，不能再写进 datas ——
#: 同一个目标名出现两次会让 COLLECT 收到重复条目。
_PADDLE_LIBS_PE_COLLECTED = ("phi.dll", "common.dll", "mkldnn.dll", "libiomp5md.dll")

#: mkldnn.dll 静态导入的 MSVC OpenMP 运行时。它随 VC++ 2015-2022 可再发行
#: 组件分发，干净 Windows 机器上不保证存在；缺失时
#: phi.dll -> libpaddle.pyd -> import paddle 整条链失败，而原来的闸门看不见。
_MSVC_OPENMP_DLL = "vcomp140.dll"

#: 模型完整性期望值。由 scripts/verify_ppocr_model.py --record 生成，
#: 也是该脚本校验时读的同一份文件 —— 这里复用同一份 SHA-256 期望值。
_PPOCR_MANIFEST = os.path.join(PROJECT_ROOT, "scripts", "ppocr_model_manifest.json")

# ---------------------------------------------------------------------------
# PaddleX 运行时依赖的发行元数据（dist-info）
#
# paddlex 判断「某个运行时依赖装了没有」用的是**发行元数据**，不是模块能不能
# import：paddlex/utils/deps.py:get_dep_version() 就是
# importlib.metadata.version(dep)，拿不到版本号就判定该依赖不可用，然后
# require_deps() 直接拒绝构造识别器。
#
# 冻结包里默认**没有**任何 dist-info，于是 version() 抛 PackageNotFoundError，
# paddlex 判定缺依赖 —— 而那个依赖的模块其实就在包里。这是实测到的：
# 冻结 OcrSelfCheck 报
#   `TextRecRunnerPredictor` is not ready for use, because the following
#   dependencies are not available: python-bidi
# 而 _internal/bidi/ 与 bidi.cp311-win_amd64.pyd 都在。
#
# 下面这几个名字来自**实测**，不是抄 paddlex 的依赖表：在生产路径上把
# paddlex.utils.deps.is_dep_available 打桩，跑一遍完整的
# PPOCREngine() 构造 + 识别，记录它查过的全部依赖，只有这些。
#   paddlepaddle 也在其中，但它走 importlib.util.find_spec 分支，不需要元数据。
#   opencv-contrib-python / pypdfium2 的检查排在 python-bidi 之后，所以冻结包
#   先报的是 python-bidi；三个都要带，否则只是把失败推到下一步。
# 换模型或升级 paddlex 时这个集合要重新实测（见 scripts/verify_frozen_bundle.py
# 的同类检查，它会在打包后核对这里没漏）。
# ---------------------------------------------------------------------------
_PADDLE_METADATA_DISTS = ("python-bidi", "pypdfium2", "opencv-contrib-python")


def _build_env_package_dirs():
    """构建解释器里可能承载第三方包的目录（去重保序）。"""
    dirs = []

    def _add(path):
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)

    try:
        import sysconfig
        _paths = sysconfig.get_paths()
        for _key in ("purelib", "platlib"):
            _add(_paths.get(_key))
    except Exception:  # noqa: BLE001 - 定位失败交给下面的 importlib 兜底
        pass

    for _module in ("paddle", "nvidia"):
        try:
            _found = importlib.util.find_spec(_module)
        except Exception:  # noqa: BLE001
            _found = None
        for _location in list(getattr(_found, "submodule_search_locations", None) or []):
            _add(os.path.dirname(_location))
    return dirs


def _find_under_build_env(leaf):
    """在构建环境的包目录下找**目录** leaf，返回第一个存在的绝对路径。"""
    for _base in _build_env_package_dirs():
        _candidate = os.path.join(_base, leaf)
        if os.path.isdir(_candidate):
            return _candidate
    return None


def _find_file_under_build_env(leaf):
    """在构建环境的包目录下找**文件** leaf，返回第一个存在的绝对路径。"""
    for _base in _build_env_package_dirs():
        _candidate = os.path.join(_base, leaf)
        if os.path.isfile(_candidate):
            return _candidate
    return None


def _collect_paddle_libs():
    """paddle/libs 下运行期 LoadLibrary 打开、PE 分析看不见的 DLL。

    common.lib 是静态导入库，不是运行期文件，按 .dll 后缀自然排除。
    """
    _libs = _find_under_build_env(os.path.join("paddle", "libs"))
    if not _libs:
        return [], None
    _collected = [
        (os.path.join(_libs, _name), _PADDLE_LIBS_DEST)
        for _name in sorted(os.listdir(_libs))
        if _name.lower().endswith(".dll")
        and _name.lower() not in _PADDLE_LIBS_PE_COLLECTED
    ]
    return _collected, _libs


def _collect_nvidia_dlls():
    """nvidia-* wheel 的全部 DLL，保持 wheel 内的原相对路径。"""
    _nvidia = _find_under_build_env("nvidia")
    if not _nvidia:
        return [], None
    _parent = os.path.dirname(_nvidia)
    _collected = []
    for _root, _dirs, _files in os.walk(_nvidia):
        _dest = os.path.relpath(_root, _parent)
        for _name in sorted(_files):
            if _name.lower().endswith(".dll"):
                _collected.append((os.path.join(_root, _name), _dest))
    return sorted(_collected), _nvidia


def _find_msvc_openmp():
    """定位 vcomp140.dll：先用构建环境自带的副本（与 app 同源），再退到系统。"""
    for _leaf in (os.path.join("PySide6", _MSVC_OPENMP_DLL),
                  os.path.join("shiboken6", _MSVC_OPENMP_DLL)):
        _found = _find_file_under_build_env(_leaf)
        if _found:
            return _found
    _fallback = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", _MSVC_OPENMP_DLL)
    return _fallback if os.path.isfile(_fallback) else None


def _collect_dist_metadata(dist_names):
    """按发行名取 dist-info 目录，返回 [(目录, ".")] 形式的 datas 片段。

    发行名与目录名不是一回事（python-bidi -> python_bidi-0.6.11.dist-info），
    所以读每个 METADATA 的 Name 字段来配对，而不是拼字符串。
    """
    wanted = {name.lower().replace("_", "-") for name in dist_names}
    found = {}
    for _base in _build_env_package_dirs():
        if not os.path.isdir(_base):
            continue
        for _entry in sorted(os.listdir(_base)):
            if not _entry.endswith(".dist-info"):
                continue
            _meta = os.path.join(_base, _entry, "METADATA")
            if not os.path.isfile(_meta):
                continue
            try:
                with open(_meta, "r", encoding="utf-8", errors="replace") as _f:
                    _name = ""
                    for _line in _f:
                        if _line.startswith("Name:"):
                            _name = _line.split(":", 1)[1].strip()
                            break
            except OSError:
                continue
            _key = _name.lower().replace("_", "-")
            if _key in wanted and _key not in found:
                found[_key] = os.path.join(_base, _entry)
    # dest 必须是 dist-info **目录名**，不能是 "."：PyInstaller 对目录型 datas
    # 源只保留文件相对该目录的路径，会把 METADATA / RECORD / WHEEL 平铺到包根，
    # 三个包的这些文件互相覆盖。与 _PPOCR_MODEL_DEST 是同一个坑。
    return ([(found[name], os.path.basename(found[name])) for name in sorted(found)],
            sorted(found))


_PADDLE_LIBS, _PADDLE_LIBS_DIR = _collect_paddle_libs()
_NVIDIA_DLLS, _NVIDIA_DIR = _collect_nvidia_dlls()
_MSVC_OPENMP_SRC = _find_msvc_openmp()
_PADDLE_METADATA, _PADDLE_METADATA_FOUND = _collect_dist_metadata(_PADDLE_METADATA_DISTS)

# Paddle / PaddleX 是*可导入*依赖：PyInstaller 对解析不了的 hidden import
# 只警告不报错，缺了会得到"包能构建、OCR 起不来"。检查在 spec 自己的解释器里
# 跑 —— 那正是 PyInstaller 走 import 图用的同一个解释器，"这里能解析" == "会被打进包"。
_paddle_missing = [
    _mod for _mod in ("paddle", "paddlex")
    if importlib.util.find_spec(_mod) is None
]
if _paddle_missing:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the Paddle OCR runtime is not installed.",
                "",
                "  not importable in the build interpreter (%s):" % sys.executable,
                "    %s" % ", ".join(_paddle_missing),
                "",
                "PP-OCRv6_medium_rec is the production OCR backend and runs on",
                "Paddle. Without it the frozen package cannot load its OCR model on",
                "any machine, and PyInstaller will not tell you: an unresolvable",
                "hidden import is only a warning.",
                "",
                "Fix: install the packaging requirements into the build environment",
                "  python -m pip install -r requirements-packaging.txt",
                "=" * 72,
                "",
            ]
        )
    )

if not _PADDLE_LIBS:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the Paddle runtime libraries are missing.",
                "",
                "  no paddle/libs found under any of:",
                "    %s" % "\n    ".join(_build_env_package_dirs() or ["(none)"]),
                "",
                "paddle/libs holds libpaddle.pyd's runtime companions. They are",
                "opened with LoadLibrary, so PyInstaller's PE dependency walk cannot",
                "see them; the spec has to list them by hand, and it can only do that",
                "from an installed paddlepaddle-gpu.",
                "",
                "Fix: python -m pip install -r requirements-packaging.txt",
                "=" * 72,
                "",
            ]
        )
    )

if not _NVIDIA_DLLS:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the CUDA runtime is missing.",
                "",
                "  no CUDA DLLs found under any of:",
                "    %s" % "\n    ".join(_build_env_package_dirs() or ["(none)"]),
                "",
                "ocr.ppocr_engine._ensure_cuda_dlls() resolves them out of",
                "site-packages/nvidia/cu13/bin/x86_64 (and nvidia/cudnn/bin) at",
                "runtime. Without them the frozen app fails on the first GPU op",
                'with "cublas64_13.dll ... not configured correctly" (error 126),',
                "and there is no CPU fallback by design.",
                "",
                "Fix: python -m pip install -r requirements-packaging.txt",
                "=" * 72,
                "",
            ]
        )
    )

if _MSVC_OPENMP_SRC is None:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: %s is missing." % _MSVC_OPENMP_DLL,
                "",
                "mkldnn.dll statically imports it, and mkldnn.dll is a static import",
                "of phi.dll, which libpaddle.pyd imports in turn. On a clean Windows",
                "machine that has no VC++ 2015-2022 redistributable the whole chain",
                "fails and 'import paddle' never succeeds -- which surfaces as an OCR",
                "that never starts, not as a packaging error.",
                "",
                "Looked for it beside PySide6/shiboken6 in the build environment and",
                "in %%SystemRoot%%\\System32.",
                "=" * 72,
                "",
            ]
        )
    )

_metadata_missing = [
    _name for _name in _PADDLE_METADATA_DISTS
    if _name.lower().replace("_", "-") not in _PADDLE_METADATA_FOUND
]
if _metadata_missing:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: PaddleX runtime dependency metadata is missing.",
                "",
                "  no dist-info found for: %s" % ", ".join(_metadata_missing),
                "",
                "PaddleX decides whether an optional runtime dependency is present",
                "with importlib.metadata.version() (paddlex/utils/deps.py), NOT by",
                "importing it. A frozen package without dist-info therefore makes",
                "PaddleX refuse to build the recognizer even though the module is",
                "right there -- measured on the frozen OcrSelfCheck:",
                '  TextRecRunnerPredictor ... dependencies are not available:',
                "  python-bidi",
                "",
                "Fix: python -m pip install -r requirements-packaging.txt",
                "=" * 72,
                "",
            ]
        )
    )

# 模型完整性：与 scripts/verify_ppocr_model.py 读同一份 manifest，因此
# "构建机上的模型 == manifest 记录的模型" 由同一处期望值判定。
# manifest 不在（例如被某个测试的临时工程树)时只告警 —— 真实仓库里它进 Git。
_MODEL_MANIFEST = {}
if os.path.isfile(_PPOCR_MANIFEST):
    import json as _json
    import hashlib as _hashlib
    with open(_PPOCR_MANIFEST, "r", encoding="utf-8") as _f:
        _MODEL_MANIFEST = _json.load(_f).get("files", {})

    _model_failures = []
    for _name, _expected in sorted(_MODEL_MANIFEST.items()):
        _path = os.path.join(_PPOCR_MODEL_DIR, _name)
        if not os.path.isfile(_path):
            _model_failures.append("缺少文件 %s" % _name)
            continue
        _size = os.path.getsize(_path)
        if _size != _expected.get("bytes"):
            _model_failures.append(
                "%s 字节数不符（期望 %s，实际 %d）" % (_name, _expected.get("bytes"), _size))
            continue
        _digest = _hashlib.sha256()
        with open(_path, "rb") as _f:
            for _chunk in iter(lambda: _f.read(1 << 20), b""):
                _digest.update(_chunk)
        if _digest.hexdigest() != _expected.get("sha256"):
            _model_failures.append(
                "%s SHA-256 不符（期望 %s，实际 %s）"
                % (_name, _expected.get("sha256", "")[:16], _digest.hexdigest()[:16]))

    if _model_failures:
        raise FileNotFoundError(
            "\n".join(
                [
                    "",
                    "=" * 72,
                    "BUILD ABORTED: the PP-OCRv6 model does not match its manifest.",
                    "",
                    "  %s" % _PPOCR_MODEL_DIR,
                    "  expected by %s" % _PPOCR_MANIFEST,
                    "",
                ]
                + ["  - %s" % item for item in _model_failures]
                + [
                    "",
                    "打包一份与 manifest 不同的权重，会让「准确率变了」被当成代码",
                    "回归去查。要么把构建机上的模型换成 manifest 记录的那一份，",
                    "要么在确认新权重后重新记录：",
                    "    python scripts/verify_ppocr_model.py --record",
                    "=" * 72,
                    "",
                ]
            )
        )
else:
    print("WARNING: %s 不存在，跳过模型 SHA-256 校验（只按字节数判非空）"
          % _PPOCR_MANIFEST)

# Data files to include
datas = [
    # Config file
    (os.path.join(PROJECT_ROOT, "config.yaml"), "."),
    # License verification public key. The client needs it to verify SignedLicenses
    # offline, and utils.susi_verifier.resolve_public_key() looks for it next to the
    # package root -- which inside a frozen bundle is sys._MEIPASS. The key is public
    # by design; the private key never leaves the license server.
    (_PUBLIC_KEY_SRC, "."),
    # susi_helper is a separate executable (Rust), not an importable module, so it is
    # shipped as data. The destination directory matters: it must match where
    # utils.susi_verifier.default_helper_path() looks, otherwise the packaged app
    # cannot compute a machine code and activation fails before any HTTP call.
    (_SUSI_HELPER_SRC, _SUSI_HELPER_DEST),
    # FFmpeg is an external executable, not an importable module, so it ships as
    # data. A clean Windows machine has it not installed, and the release build
    # must not depend on PATH, so it is bundled and resolved relative to the
    # bundle: utils.runtime_paths probes <MEIPASS>/runtime/ffmpeg/ffmpeg.exe.
    (_FFMPEG_SRC, _FFMPEG_DEST),
    # ffprobe.exe backs the resolution pre-detection. resolve_ffprobe_path() probes
    # <MEIPASS>/runtime/ffmpeg/ffprobe.exe, so it must land in the same directory as
    # ffmpeg.exe. Without it the frozen app cannot size its rawvideo frames.
    (_FFPROBE_SRC, _FFMPEG_DEST),
]

# ffmpeg.exe's companion DLLs, resolved beside it at process start. Bundled flat
# into <dest>/ next to ffmpeg.exe -- the same place the Windows loader searches,
# and where the vendored set is validated to run from a clean PATH.
datas += [(dll, _FFMPEG_DEST) for dll in _FFMPEG_DLLS]

# ffprobe.exe's companion DLLs, resolved beside it at process start. Same
# directory as ffmpeg.exe's set, and the same reason.
datas += [(dll, _FFMPEG_DEST) for dll in _FFPROBE_DLLS]

# PP-OCRv6 model. Dest carries the model directory name (see _PPOCR_MODEL_DEST),
# so the frozen probe path <MEIPASS>/runtime/ppocr/PP-OCRv6_medium_rec_infer is
# exactly what PPOCRModelLocator._bundled_candidates() looks for.
datas += [(_PPOCR_MODEL_DIR, _PPOCR_MODEL_DEST)]

# paddle/libs: only the members PyInstaller's dependency walk cannot see. The
# four it does resolve (phi/common/mkldnn/libiomp5md) already land in this same
# directory via the BINARY TOC -- listing them here as well would put the same
# destination name in the TOC twice.
datas += _PADDLE_LIBS

# CUDA closure: the whole nvidia/ tree from the wheels, original relative paths
# preserved, because both _ensure_cuda_dlls() (nvidia/cu13/bin/x86_64) and
# paddle itself (nvidia/cudnn/bin) probe by that layout.
datas += _NVIDIA_DLLS

# MSVC OpenMP runtime, flat at the bundle root beside msvcp140/vcruntime140 --
# that is where the Windows loader searches for mkldnn.dll's static import.
datas += [(_MSVC_OPENMP_SRC, ".")]

# PaddleX's dependency-check metadata (see _PADDLE_METADATA_DISTS). importlib
# .metadata scans sys.path for *.dist-info; in a frozen onedir that is
# _internal/, which is exactly where dest="." puts them.
datas += _PADDLE_METADATA

# Binaries to include (will be added to runtime directory)
binaries = []

# Additional binary data
additional_binaries = []

# Hidden imports
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtWidgets",
    "PySide6.QtGui",
    "PySide6.QtSvg",
    "cv2",
    "numpy",
    "pytesseract",
    "yaml",
    "requests",
    "psutil",
    "threading",
    "logging",
    "subprocess",
    "collections",
    "ctypes",
    "msvcrt",
    "select",
    "time",
    "argparse",
]

# Collect Qt plugins
def collect_qt_plugins():
    """Collect required Qt5/Qt6 plugins"""
    try:
        from PySide6.QtCore import QLibraryInfo
        plugin_path = QLibraryInfo.path(QLibraryInfo.PluginsPath)

        # Required plugins
        required_plugins = [
            "platforms",  # Windows platform plugin
            "imageformats",  # Image format plugins
            "styles",  # Style plugins
        ]

        plugins = []
        for plugin_dir in required_plugins:
            dir_path = os.path.join(plugin_path, plugin_dir)
            if os.path.exists(dir_path):
                for file in os.listdir(dir_path):
                    if file.endswith(".dll"):
                        plugins.append((os.path.join(dir_path, file), os.path.join("PySide6", "plugins", plugin_dir)))

        return plugins
    except Exception as e:
        print(f"Warning: Could not collect Qt plugins: {e}")
        return []

# Add Qt plugins to binaries
qt_plugins = collect_qt_plugins()
binaries.extend(qt_plugins)

# Include Python runtime files
def include_python_runtime():
    """Include required Python runtime files"""
    try:
        import PySide6
        pyside_path = os.path.dirname(PySide6.__file__)

        # Additional Qt modules
        qt_modules = [
            "PySide6/QtNetwork.dll",  # For network functionality
            "PySide6/QtMultimedia.dll",  # For multimedia (if needed)
            "PySide6/QtOpenGL.dll",  # For OpenGL support
        ]

        binaries_list = []
        for module in qt_modules:
            module_path = os.path.join(pyside_path, module)
            if os.path.exists(module_path):
                binaries_list.append((module_path, "."))

        return binaries_list
    except Exception as e:
        print(f"Warning: Could not include Python runtime: {e}")
        return []

binaries.extend(include_python_runtime())

a = Analysis(
    ['main.py'],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Create executable with proper configuration
pyz = PYZ(a.pure)

# onedir layout: EXE carries only the bootloader, the PYZ and the scripts. Passing
# a.binaries / a.datas here as well (the pre-2026-09 form) made PKG embed the whole
# payload compressed into the EXE *and* have COLLECT write it out again, so the
# package shipped every binary twice -- an EXE that never gets unpacked (onedir
# does not self-extract) plus the real _internal copy.
#
# exclude_binaries=True is what makes this the onedir codepath: PKG diverts every
# BINARY/EXTENSION/DATA entry into PKG.dependencies instead of the CArchive, and
# COLLECT collects them. PyInstaller's own comment on that branch: "This prevents
# a onedir application from becoming a broken onefile one if user accidentally
# passes datas and binaries TOCs to EXE instead of COLLECT."
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    # EXE name is fixed at DouyinLowLatencyViewer (Phase 0 decision).
    # "LiveLens" is the product/window name only -- it must NOT be used here
    # without a matching change to the update system, which derives the process
    # to wait for, the backup name and the relaunch target from this value.
    name='DouyinLowLatencyViewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    uac_admin=False,
    version_file=None,
)

# Sole owner of the binaries and datas TOCs in this onedir build. It also merges
# exe.dependencies, which is how any EXTENSION/BINARY entries arriving via
# PYZ.dependencies still reach _internal/.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DouyinLowLatencyViewer',
)