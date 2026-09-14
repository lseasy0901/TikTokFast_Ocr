# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Douyin Low Latency Viewer
Windows standalone EXE packaging
"""

import glob
import os
import sys

# Get the absolute path of the project directory
PROJECT_ROOT = os.path.abspath(".")

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
#   runtime/tesseract/tesseract.exe               (third-party binary)
#   runtime/tesseract/*.dll                       (its required companion DLLs)
#   runtime/tesseract/tessdata/eng.traineddata    (third-party data)
#
# PyInstaller only *warns* when a `datas` source is missing and then produces an
# EXE anyway. Each of those omissions is invisible here and fatal on a customer's
# machine: no helper -> activation never works; no FFmpeg -> the video pipeline
# dies at spawn; no ffprobe -> the resolution probe silently degrades to a wrong
# fixed frame size; no Tesseract -> OCR silently degrades. Verify all of them up
# front and stop the build.
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
#   ocr.engine.TesseractLocator.find_tesseract() -> <MEIPASS>/runtime/tesseract/tesseract.exe
# which is why ffmpeg.exe and tesseract.exe must keep these exact relative paths.
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
# ffmpeg.exe and gitignored (.gitignore), exactly like the Tesseract set.
_FFMPEG_DLL_REQUIRED = os.path.join(_FFMPEG_DIR, "libpag.dll")
_FFMPEG_DLLS = sorted(
    path
    for path in glob.glob(os.path.join(_FFMPEG_DIR, "*.dll"))
    if os.path.basename(path).lower() not in _FFPROBE_DLL_NAMES
)
_FFMPEG_DLL_MIN = 7

_TESSERACT_DIR = os.path.join(PROJECT_ROOT, "runtime", "tesseract")
_TESSERACT_SRC = os.path.join(_TESSERACT_DIR, "tesseract.exe")
_TESSDATA_SRC = os.path.join(
    PROJECT_ROOT, "runtime", "tesseract", "tessdata", "eng.traineddata"
)
_TESSERACT_DEST = os.path.join("runtime", "tesseract")
_TESSDATA_DEST = os.path.join(_TESSERACT_DEST, "tessdata")

# tesseract.exe is a stock UB-Mannheim build and is NOT self-contained: it needs
# its ~26 sibling DLLs (libtesseract-5.dll, liblept-5.dll, libcurl-4.dll, ...)
# beside it or it dies at process start with "error while loading shared
# libraries". Those DLLs are provisioned into runtime/tesseract/ and gitignored
# (.gitignore). Shipping the exe alone would produce an EXE whose OCR never
# starts, so the core library is required by name and the set is required to be
# complete.
_TESSERACT_DLL_REQUIRED = os.path.join(_TESSERACT_DIR, "libtesseract-5.dll")
_TESSERACT_DLLS = sorted(glob.glob(os.path.join(_TESSERACT_DIR, "*.dll")))
_TESSERACT_DLL_MIN = 26


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
        "at spawn; no Tesseract (or a partial one) -> OCR silently never starts.",
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
        "  runtime/tesseract/",
        "      copy tesseract.exe to %s" % _TESSERACT_SRC,
        "      and eng.traineddata to %s" % _TESSDATA_SRC,
        "      plus that build's %d companion DLLs (tesseract.exe is not" % _TESSERACT_DLL_MIN,
        "      self-contained; without them it fails at process start)",
        "      e.g. from C:\\Program Files\\Tesseract-OCR\\*.dll",
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
        ("Tesseract executable", _TESSERACT_SRC),
        ("Tesseract English traineddata", _TESSDATA_SRC),
        ("Tesseract core library", _TESSERACT_DLL_REQUIRED),
    )
)

# Same reasoning as Tesseract: a present-but-partial DLL set is the case
# require_build_inputs() cannot see. ffmpeg.exe needs all of them at process
# start, so a partial set is as fatal as none.
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

# A present-but-partial Tesseract (only the exe, or a few DLLs) is the one case
# require_build_inputs() cannot see. Fail on an incomplete companion set too.
if len(_TESSERACT_DLLS) < _TESSERACT_DLL_MIN:
    raise FileNotFoundError(
        "\n".join(
            [
                "",
                "=" * 72,
                "BUILD ABORTED: the Tesseract runtime is incomplete.",
                "",
                "  found %d DLL(s) in %s" % (len(_TESSERACT_DLLS), _TESSERACT_DIR),
                "  expected at least %d" % _TESSERACT_DLL_MIN,
                "",
                "tesseract.exe is a stock UB-Mannheim build and links against its",
                "sibling DLLs at process start. A partial set builds an EXE whose OCR",
                "never starts -- which looks like an OCR bug, not a packaging bug.",
                "",
                "Provision them from a full Tesseract install, e.g.",
                "  copy \"C:\\Program Files\\Tesseract-OCR\\*.dll\" %s" % _TESSERACT_DIR,
                "=" * 72,
                "",
            ]
        )
    )

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
    # FFmpeg and Tesseract are external executables, not importable modules, so they
    # ship as data. A clean Windows machine has neither installed, and the release
    # build must not depend on PATH, so both are bundled and resolved relative to
    # the bundle: utils.runtime_paths and ocr.engine both probe <MEIPASS>/runtime/...
    # The engine's traineddata sits beside tesseract.exe under <dest>/tessdata/.
    (_FFMPEG_SRC, _FFMPEG_DEST),
    # ffprobe.exe backs the resolution pre-detection. resolve_ffprobe_path() probes
    # <MEIPASS>/runtime/ffmpeg/ffprobe.exe, so it must land in the same directory as
    # ffmpeg.exe. Without it the frozen app cannot size its rawvideo frames.
    (_FFPROBE_SRC, _FFMPEG_DEST),
    (_TESSERACT_SRC, _TESSERACT_DEST),
    (_TESSDATA_SRC, _TESSDATA_DEST),
]

# ffmpeg.exe's companion DLLs, resolved beside it at process start. Bundled flat
# into <dest>/ next to ffmpeg.exe -- the same place the Windows loader searches,
# and where the vendored set is validated to run from a clean PATH.
datas += [(dll, _FFMPEG_DEST) for dll in _FFMPEG_DLLS]

# ffprobe.exe's companion DLLs, resolved beside it at process start. Same
# directory as ffmpeg.exe's set, and the same reason.
datas += [(dll, _FFMPEG_DEST) for dll in _FFPROBE_DLLS]

# tesseract.exe's companion DLLs, resolved beside it at process start. Bundled
# flat into <dest>/ next to tesseract.exe -- that is where the Windows loader
# looks, and where the vendored set is validated to run.
datas += [(dll, _TESSERACT_DEST) for dll in _TESSERACT_DLLS]

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