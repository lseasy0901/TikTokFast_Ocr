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
#   runtime/onnx/                                 (production OCR artifacts, large)
#
# PyInstaller only *warns* when a `datas` source is missing and then produces an
# EXE anyway. Each of those omissions is invisible here and fatal on a customer's
# machine: no helper -> activation never works; no FFmpeg -> the video pipeline
# dies at spawn; no ffprobe -> the resolution probe silently degrades to a wrong
# fixed frame size; no ONNX model -> the production OCR backend cannot load.
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
# ONNX Runtime OCR artifacts (the production OCR backend)
#
# PP-OCRv6_medium_rec on ONNX Runtime + DirectML replaced the retired
# PaddlePaddle-GPU / PaddleX runtime, so the build ships exactly two OCR
# artifacts. They must land where the client probes for them:
#
#   ocr.onnx_engine.ONNXModelLocator._bundled_candidates()
#       -> <base>/runtime/onnx/PP-OCRv6_medium_rec_fp32.onnx
#       -> <base>/runtime/onnx/charset.txt
#
# where <base> is sys._MEIPASS in a frozen onedir build -- the same contract the
# retired Tesseract and Paddle locators had for their own runtime/ subtrees.
#
# Both datas sources below are *files*, not directories, so PyInstaller copies
# them straight into dest. The "a directory source drops its own name" trap
# (which is why the old PP-OCRv6 model dest had to repeat the directory name)
# does not apply to them.
#
# ONNX Runtime itself is deliberately NOT listed in datas/binaries: the pinned
# pyinstaller-hooks-contrib ships hook-onnxruntime.py, whose
# `collect_dynamic_libs("onnxruntime")` picks up onnxruntime/capi/*.dll
# (onnxruntime.dll, DirectML.dll, onnxruntime_providers_shared.dll) by itself.
# Only the two model artifacts are ours to place.
#
# There is no CUDA closure and no Paddle runtime any more: DirectML is the GPU
# path and CPUExecutionProvider is the fallback, both inside onnxruntime.
# ---------------------------------------------------------------------------
_ONNX_SUBDIR = "onnx"                        # mirrors ocr.onnx_engine._ONNX_SUBDIR
_ONNX_DEST = os.path.join("runtime", _ONNX_SUBDIR)
_ONNX_DIR = os.path.join(PROJECT_ROOT, "runtime", _ONNX_SUBDIR)
_ONNX_MODEL_NAME = "PP-OCRv6_medium_rec_fp32.onnx"
_ONNX_CHARSET_NAME = "charset.txt"
_ONNX_MODEL_SRC = os.path.join(_ONNX_DIR, _ONNX_MODEL_NAME)
_ONNX_CHARSET_SRC = os.path.join(_ONNX_DIR, _ONNX_CHARSET_NAME)


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
        "  runtime/onnx/",
        "      copy the two ONNX artifacts to %s:" % _ONNX_DIR,
        "        %s" % _ONNX_MODEL_NAME,
        "        %s" % _ONNX_CHARSET_NAME,
        "      (both must be present and non-empty; charset.txt is exported by",
        "      scripts/export_onnx_charset.py from the PP-OCRv6 inference.yml).",
        "      PP-OCRv6_medium_rec on ONNX Runtime + DirectML is the production",
        "      OCR backend; the old Paddle/PaddleX + CUDA runtime is retired.",
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
        ("ONNX OCR model", _ONNX_MODEL_SRC),
        ("ONNX charset", _ONNX_CHARSET_SRC),
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
for _onnx_label, _onnx_path in (("ONNX OCR model", _ONNX_MODEL_SRC),
                                ("ONNX charset", _ONNX_CHARSET_SRC)):
    if os.path.isfile(_onnx_path) and os.path.getsize(_onnx_path) == 0:
        raise FileNotFoundError(
            "\n".join(
                [
                    "",
                    "=" * 72,
                    "BUILD ABORTED: %s is empty." % _onnx_label,
                    "",
                    "  %s" % _onnx_path,
                    "  is a 0-byte placeholder; expected the real artifact.",
                    "",
                    "ONNXModelLocator.is_usable_model_file() treats a 0-byte file as",
                    "'not found', so this build would produce an EXE whose OCR never",
                    "starts -- which looks like an OCR bug, not a packaging bug.",
                    "",
                    "How to provision:",
                    "  put %s and %s under %s" % (
                        _ONNX_MODEL_NAME, _ONNX_CHARSET_NAME, _ONNX_DIR),
                    "  (charset.txt is generated by scripts/export_onnx_charset.py)",
                    "=" * 72,
                    "",
                ]
            )
        )

# ---------------------------------------------------------------------------
# ONNX Runtime: why nothing else has to be listed by hand
#
# The retired Paddle/CUDA stack needed a hand-written Phase-1.1 block here
# because PyInstaller's dependency analysis could not see any of it:
#   * the model was pure data -- a missing datas entry only warns;
#   * paddle/libs DLLs are opened with LoadLibrary at runtime, so they are
#     absent from the PE import table (measured: libpaddle.pyd statically
#     imports only phi.dll and python311.dll);
#   * the nvidia-* CUDA wheels were located via os.add_dll_directory, which is
#     neither a Python module nor a static dependency.
#
# ONNX Runtime has none of those problems. It is an ordinary importable package
# and the pinned pyinstaller-hooks-contrib ships hook-onnxruntime.py:
#
#     binaries = collect_dynamic_libs("onnxruntime")
#
# which collects onnxruntime/capi/*.dll -- onnxruntime.dll, DirectML.dll and
# onnxruntime_providers_shared.dll -- while the pybind11 extension
# (onnxruntime_pybind11_state.pyd) arrives through the normal import graph.
# CPUExecutionProvider is compiled into onnxruntime.dll; DirectML ships as its
# own DLL next to it. So the only OCR entries this spec adds are the two model
# artifacts, and there is no CUDA closure and no Paddle runtime to keep alive.
#
# What can still go silently wrong is the *import*: PyInstaller only warns about
# an unresolvable hidden import, so a build interpreter without onnxruntime
# would still produce an installer whose OCR never starts. That is what the ONNX
# import gate below is for -- same reasoning as the pytesseract gate above.
# ---------------------------------------------------------------------------

#: 模型完整性期望值，与 scripts/verify_onnx_model.py 读同一份文件。
_ONNX_MANIFEST = os.path.join(PROJECT_ROOT, "scripts", "onnx_model_manifest.json")


# onnxruntime is an *importable* dependency, and PyInstaller only warns about a
# hidden import it cannot resolve -- the build would succeed and ship an
# installer whose OCR never starts on any machine. Gate it like pytesseract.
_onnx_missing = [
    _mod for _mod in ("onnxruntime",)
    if importlib.util.find_spec(_mod) is None
]

if _onnx_missing:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the ONNX OCR runtime is not installed.",
                "",
                "  not importable in the build interpreter (%s):" % sys.executable,
                "    %s" % ", ".join(_onnx_missing),
                "",
                "PP-OCRv6_medium_rec runs on ONNX Runtime (DirectML, with a",
                "CPUExecutionProvider fallback) and is the production OCR backend.",
                "Without it the frozen package cannot load its OCR model on any",
                "machine, and PyInstaller will not tell you: an unresolvable hidden",
                "import is only a warning.",
                "",
                "Fix: install the packaging requirements into the build environment",
                "  python -m pip install -r requirements-packaging.txt",
                "=" * 72,
                "",
            ]
        )
    )

# 产物完整性：与 scripts/verify_onnx_model.py 读同一份 manifest，因此
# 「构建机上的产物 == manifest 记录的产物」由同一处期望值判定。
# charset.txt 也在其中：项数或顺序错了**不会报错**，只会把每个字符索引
# 静默解错，所以它必须被同一把 SHA-256 锁住。
# manifest 不在时只告警 —— 真实仓库里它进 Git。
_MODEL_MANIFEST = {}
if os.path.isfile(_ONNX_MANIFEST):
    import json as _json
    import hashlib as _hashlib
    with open(_ONNX_MANIFEST, "r", encoding="utf-8") as _f:
        _MODEL_MANIFEST = _json.load(_f).get("files", {})

    _model_failures = []
    for _name, _expected in sorted(_MODEL_MANIFEST.items()):
        _path = os.path.join(_ONNX_DIR, _name)
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
                    "BUILD ABORTED: the ONNX OCR artifacts do not match their manifest.",
                    "",
                    "  %s" % _ONNX_DIR,
                    "  expected by %s" % _ONNX_MANIFEST,
                    "",
                ]
                + ["  - %s" % item for item in _model_failures]
                + [
                    "",
                    "打包一份与 manifest 不同的产物，会让「识别结果变了」被当成代码",
                    "回归去查。要么把构建机上的产物换成 manifest 记录的那一份，",
                    "要么在确认新产物后重新记录：",
                    "    python scripts/verify_onnx_model.py --record",
                    "=" * 72,
                    "",
                ]
            )
        )
else:
    print("WARNING: %s 不存在，跳过 ONNX 产物 SHA-256 校验（只按字节数判非空）"
          % _ONNX_MANIFEST)


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

# ONNX OCR artifacts. Both sources are files, so dest is the directory they must
# land in and the frozen probe path <MEIPASS>/runtime/onnx/<name> is exactly what
# ONNXModelLocator._bundled_candidates() looks for.
datas += [
    (_ONNX_MODEL_SRC, _ONNX_DEST),
    (_ONNX_CHARSET_SRC, _ONNX_DEST),
]

# Nothing else is needed for OCR: ONNX Runtime and its provider DLLs come from
# PyInstaller's hook-onnxruntime (see the ONNX Runtime block above). There is no
# Paddle runtime, no CUDA closure and no PaddleX dependency metadata any more.

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
    # 音频播放（AudioService）：QtMultimedia 的 pyd/DLL 与 multimedia 插件必须
    # 进包，否则冻结包里 import 即失败、音频一次都放不出来。模块在
    # audio/output.py 内延迟导入，modulegraph 发现不了，必须显式声明
    # （与本文件里 onnxruntime 的注释同理）。
    "PySide6.QtMultimedia",
    "cv2",
    "numpy",
    # ONNX OCR runtime. hook-onnxruntime (pyinstaller-hooks-contrib) collects its
    # DLLs; this entry guarantees the module itself resolves even though the
    # production import lives inside ONNXEngine.__init__. The modulegraph does
    # find function-level imports, but the failure mode here -- a silent warning
    # and an installer whose OCR never starts -- is one this project has been
    # bitten by before.
    "onnxruntime",
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
            "multimedia",  # 音频后端插件（windowsmediaplugin 等，AudioService 用）
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
    # The OCR backend moved to ONNX Runtime; Paddle/PaddleX must not come back in
    # through the import graph. Deleting the datas entries alone does NOT drop
    # them -- PyInstaller walks imports, and any stray `import paddle` left in the
    # tree would pull roughly 735 MB of Paddle plus its CUDA closure back in.
    excludes=["paddle", "paddlex"],
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
    # UPX-compressing the ONNX Runtime DLLs is a known breakage source, and it
    # buys little on files this large. UPX is not installed on the current build
    # machine, so upx=True is a no-op today; this list is here so that stays
    # harmless if UPX is ever put on PATH.
    upx_exclude=["onnxruntime.dll", "DirectML.dll"],
    name='DouyinLowLatencyViewer',
)