#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8G: the release build must fail loudly, not silently.

douyin_viewer.spec bundles artifacts that are NOT in Git:

    license_public_key.pem                                  (.gitignore:22)
    susi_helper/target/release/susi_helper.exe              (gitignored Rust target)
    runtime/ffmpeg/ffmpeg.exe                               (third-party binary)
    runtime/ffmpeg/*.dll                                    (its companion DLLs)
    runtime/ppocr/PP-OCRv6_medium_rec_infer/                (production OCR model)

runtime/ffmpeg/ffprobe.exe itself is tracked, but its av*/sw* companion closure is
not -- and a lone ffprobe.exe cannot start. Both the exe and the closure are guarded.

The Tesseract set (runtime/tesseract/) used to be on this list. It is retired:
PP-OCRv6_medium_rec is the production OCR backend, and those assets were deleted
from the repository. Its gate and its cases are gone with it.

PyInstaller only *warns* when a `datas` source is missing and then builds anyway.
The resulting EXE cannot compute a machine code or verify a SignedLicense, cannot
spawn FFmpeg or ffprobe, and cannot load its production OCR model, so a missing
input surfaced as "activation doesn't work", "no video", a silently wrong frame
size, or an OCR that never starts on a customer's machine rather than as a build
error.

The spec now verifies all of them up front and raises FileNotFoundError, before
PyInstaller does any work.

The spec resolves its inputs with os.path.abspath("."), so each case below runs
the real spec file with the working directory pointed at a throwaway tree that
mimics the repository layout. Nothing here builds a real EXE -- except the last
check, which runs PyInstaller for real and expects it to abort immediately.

The throwaway trees must mirror every module-level read the spec performs before
its guards run -- including utils/version.py, which the spec exec's to learn the
client version. A tree missing it aborted on that file instead of on the guard,
which is what made this script silently useless before.

Covers:
  [1] all inputs missing   -> abort, naming the exact key and helper paths
  [2] helper missing       -> abort, naming only the helper
  [3] public key missing   -> abort, naming only the key
  [4] all inputs present   -> accepted, datas destinations unchanged, ffprobe.exe
                              bundled beside ffmpeg.exe with its own av*/sw*
                              closure, and the full FFmpeg companion DLL set
                              bundled
  [5] FFmpeg core lib missing    -> abort, naming it
  [6] FFmpeg set incomplete      -> abort, saying the runtime is incomplete
  [6a] ffprobe core lib missing  -> abort, naming it
  [6b] ffprobe set incomplete    -> abort, saying the runtime is incomplete
  [7] PP-OCRv6 model missing     -> abort, naming the model path
  [8] PP-OCRv6 model empty       -> abort, saying the model is empty
  [9] the real repository  -> accepted once the model is provisioned; aborts
                              naming the model until then
  [10] real PyInstaller    -> exits non-zero on a tree missing the inputs
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent      # repository root
SPEC = ROOT / "douyin_viewer.spec"

PUBLIC_KEY_NAME = "license_public_key.pem"
HELPER_RELPATH = ("susi_helper", "target", "release", "susi_helper.exe")
HELPER_DEST = os.path.join("susi_helper", "target", "release")

# The spec exec's this to read APP_VERSION, before any guard runs. Every fixture
# tree must carry it or the spec aborts on it rather than on the guard under test.
VERSION_RELPATH = ("utils", "version.py")
VERSION_SOURCE = '__version__ = "0.0.0"\n'

# runtime/ assets bundled so the release build never needs PATH.
FFMPEG_RELPATH = ("runtime", "ffmpeg", "ffmpeg.exe")
FFMPEG_DEST = os.path.join("runtime", "ffmpeg")
FFMPEG_DIR_RELPATH = ("runtime", "ffmpeg")

# ffmpeg.exe is the ACLOS recorder's build, not a stock FFmpeg: it hard-imports
# libpag.dll and o264rtenc.dll (plus ANGLE and the MSVC runtime) at process start,
# so the spec requires the core library by name and the whole set to be present.
FFMPEG_DLL_REQUIRED = "libpag.dll"
FFMPEG_DLL_MIN = 7

# ffprobe.exe is a different build from ffmpeg.exe -- the ACLOS *shared* n5.1.3
# build -- and hard-imports its own av*/sw* closure. It lands beside ffmpeg.exe,
# so the spec tells the two closures apart by name and the fixture must provision
# the av*/sw* names, not stub_ffmpeg_*.dll, or the ffmpeg count check would see
# them as ffmpeg's.
FFPROBE_RELPATH = ("runtime", "ffmpeg", "ffprobe.exe")
FFPROBE_DEST = os.path.join("runtime", "ffmpeg")
FFPROBE_DLL_NAMES = (
    "avformat-59.dll",      # core library, required by name
    "avcodec-59.dll",
    "avdevice-59.dll",
    "avfilter-8.dll",
    "avutil-57.dll",
    "swresample-4.dll",
    "swscale-6.dll",
)
FFPROBE_DLL_REQUIRED = FFPROBE_DLL_NAMES[0]
FFPROBE_DLL_COUNT = len(FFPROBE_DLL_NAMES)

# PP-OCRv6 is the production OCR backend, so the build has to ship the model. The
# spec requires inference.pdiparams by name (it mirrors
# PPOCRModelLocator.is_usable_model_dir(), which rejects a 0-byte placeholder) and
# the destination must match the locator's probe path:
#   <base>/runtime/ppocr/PP-OCRv6_medium_rec_infer
PPOCR_SUBDIR = "ppocr"
PPOCR_MODEL_DIR_NAME = "PP-OCRv6_medium_rec_infer"
PPOCR_MODEL_PARAMS = "inference.pdiparams"
PPOCR_RELPATH = ("runtime", PPOCR_SUBDIR, PPOCR_MODEL_DIR_NAME)
PPOCR_PARAMS_RELPATH = PPOCR_RELPATH + (PPOCR_MODEL_PARAMS,)
PPOCR_DEST = os.path.join("runtime", PPOCR_SUBDIR)

RUNTIME_RELPATHS = (FFMPEG_RELPATH, FFPROBE_RELPATH)

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(cond, msg):
    global PASSED, FAILED, SKIPPED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


def skip(msg):
    global SKIPPED
    SKIPPED += 1
    print("  [skip] " + msg, flush=True)


def make_tree(root, with_key, with_helper, with_runtime=True,
              ffmpeg_dll_count=FFMPEG_DLL_MIN,
              ffprobe_dll_count=FFPROBE_DLL_COUNT,
              with_ppocr_model=True, ppocr_params=b"stub model weights"):
    """Build a throwaway repository root containing the requested inputs."""
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("ocr: {}\n", encoding="utf-8")
    # APP_VERSION is exec'd at spec import time, before any guard.
    target = root.joinpath(*VERSION_RELPATH).parent
    target.mkdir(parents=True, exist_ok=True)
    (target / VERSION_RELPATH[-1]).write_text(VERSION_SOURCE, encoding="utf-8")
    if with_key:
        (root / PUBLIC_KEY_NAME).write_text(
            "-----BEGIN PUBLIC KEY-----\nstub\n-----END PUBLIC KEY-----\n",
            encoding="utf-8",
        )
    if with_helper:
        target = root.joinpath(*HELPER_RELPATH).parent
        target.mkdir(parents=True, exist_ok=True)
        (target / HELPER_RELPATH[-1]).write_bytes(b"stub helper")
    if with_runtime:
        for relpath in RUNTIME_RELPATHS:
            target = root.joinpath(*relpath).parent
            target.mkdir(parents=True, exist_ok=True)
            (target / relpath[-1]).write_bytes(b"stub binary")
    if ffmpeg_dll_count:
        dll_dir = root.joinpath(*FFMPEG_DIR_RELPATH)
        dll_dir.mkdir(parents=True, exist_ok=True)
        (dll_dir / FFMPEG_DLL_REQUIRED).write_bytes(b"stub dll")
        for i in range(ffmpeg_dll_count - 1):
            (dll_dir / ("stub_ffmpeg_%02d.dll" % i)).write_bytes(b"stub dll")
    if ffprobe_dll_count:
        dll_dir = root.joinpath(*FFMPEG_DIR_RELPATH)
        dll_dir.mkdir(parents=True, exist_ok=True)
        for name in FFPROBE_DLL_NAMES[:ffprobe_dll_count]:
            (dll_dir / name).write_bytes(b"stub dll")
    if with_ppocr_model:
        model_dir = root.joinpath(*PPOCR_RELPATH)
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / PPOCR_MODEL_PARAMS).write_bytes(ppocr_params)
    return root


def key_path(root):
    return str(pathlib.Path(root) / PUBLIC_KEY_NAME)


def helper_path(root):
    return str(pathlib.Path(root).joinpath(*HELPER_RELPATH))


def runtime_path(root, relpath):
    return str(pathlib.Path(root).joinpath(*relpath))


def exec_spec(root):
    """Execute the real spec with PyInstaller's building blocks stubbed out.

    Returns (error, namespace, call_counts). `error` is the raised exception, or
    None when the spec ran to completion.
    """
    calls = {"analysis": 0}
    namespace = {}

    class _Analysis:
        def __init__(self, *args, **kwargs):
            calls["analysis"] += 1
            namespace["datas"] = kwargs.get("datas")
            self.pure, self.scripts = [], []
            self.binaries = kwargs.get("binaries", [])
            self.datas = kwargs.get("datas", [])

    def _PYZ(pure, *args, **kwargs):
        return {"pure": pure}

    def _EXE(*args, **kwargs):
        return {"kind": "exe"}

    def _COLLECT(*args, **kwargs):
        return {"kind": "collect"}

    scope = {
        "SPECPATH": str(SPEC.parent),
        "DISTPATH": str(pathlib.Path(root) / "dist"),
        "WORKPATH": str(pathlib.Path(root) / "build"),
        "Analysis": _Analysis, "PYZ": _PYZ, "EXE": _EXE, "COLLECT": _COLLECT,
    }

    previous = os.getcwd()
    os.chdir(str(root))
    try:
        source = SPEC.read_text(encoding="utf-8")
        exec(compile(source, str(SPEC), "exec"), scope)
        error = None
    except Exception as exc:                      # noqa: BLE001 - the point is to catch it
        error = exc
    finally:
        os.chdir(previous)

    return error, namespace, calls


def pyinstaller_available():
    try:
        import PyInstaller  # noqa: F401
        return True
    except Exception:
        return False


def missing_section(message):
    """The part of the abort report that lists what was missing.

    The provisioning hints after it mention both source paths, so they are cut
    off before the "names only the missing one" assertions.
    """
    return message.split("Looked under:")[0]


if __name__ == "__main__":
    print("=" * 66)
    print("Phase 7.2-8G build guard")
    print("=" * 66)

    with tempfile.TemporaryDirectory(prefix="dlv-8g-") as temp_dir:
        temp = pathlib.Path(temp_dir)

        # --------------------------------------------------------------
        print("\n[1] both inputs missing")
        root = make_tree(temp / "none", with_key=False, with_helper=False)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError (got %s)" % type(error).__name__)
        if isinstance(error, FileNotFoundError):
            message = str(error)
            report = missing_section(message)
            check("BUILD ABORTED" in message, "the message says the build aborted")
            check(key_path(root) in report,
                  "it names the exact missing public key path")
            check(helper_path(root) in report,
                  "it names the exact missing helper path")
            check("broken on a clean machine" in message,
                  "it explains the consequence (an EXE broken on a clean machine)")
        check(calls["analysis"] == 0,
              "PyInstaller's analysis never ran (the build stopped first)")

        # --------------------------------------------------------------
        print("\n[2] helper missing, public key present")
        root = make_tree(temp / "nohelper", with_key=True, with_helper=False)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check(helper_path(root) in report, "it names the helper path")
            check(key_path(root) not in report,
                  "it does not report the public key, which is present")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[3] public key missing, helper present")
        root = make_tree(temp / "nokey", with_key=False, with_helper=True)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check(key_path(root) in report, "it names the public key path")
            check(helper_path(root) not in report,
                  "it does not report the helper, which is present")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[4] all inputs present")
        root = make_tree(temp / "complete", with_key=True, with_helper=True)
        error, namespace, calls = exec_spec(root)
        check(error is None, "the spec is accepted (no exception)")
        check(calls["analysis"] == 1, "analysis ran exactly once")
        datas = namespace.get("datas") or []
        entries = {os.path.normpath(src): dest for src, dest in datas}
        check(os.path.normpath(key_path(root)) in entries,
              "the public key is bundled from the project root")
        check(entries.get(os.path.normpath(key_path(root))) == ".",
              "the public key destination is unchanged ('.')")
        check(os.path.normpath(helper_path(root)) in entries,
              "the helper is bundled from its release directory")
        check(entries.get(os.path.normpath(helper_path(root)))
              == os.path.normpath(HELPER_DEST),
              "the helper destination is unchanged (%s)" % HELPER_DEST)
        check(any(os.path.normpath(p) == os.path.normpath(
                      str(pathlib.Path(root) / "config.yaml")) for p in entries),
              "config.yaml is still bundled from the project root")

        for relpath, dest, label in (
            (FFMPEG_RELPATH, FFMPEG_DEST, "ffmpeg.exe"),
        ):
            src = os.path.normpath(runtime_path(root, relpath))
            check(src in entries, "%s is bundled from %s"
                  % (label, os.path.join(*relpath[:-1])))
            check(entries.get(src) == os.path.normpath(dest),
                  "%s destination is %s" % (label, dest))

        ff_dir = os.path.normpath(runtime_path(root, FFMPEG_DIR_RELPATH))

        # ffprobe.exe backs resolution pre-detection. Without it a frozen build
        # cannot size its rawvideo frames and falls back to a wrong fixed size.
        ffprobe_src = os.path.normpath(runtime_path(root, FFPROBE_RELPATH))
        check(ffprobe_src in entries, "ffprobe.exe is bundled from runtime/ffmpeg")
        check(entries.get(ffprobe_src) == os.path.normpath(FFPROBE_DEST),
              "ffprobe.exe destination is %s" % FFPROBE_DEST)

        # ...and its own av*/sw* closure must ride along beside it, or the bundled
        # ffprobe dies at process start on a machine with no ACLOS on PATH.
        ffprobe_dll_srcs = [
            p for p in entries
            if os.path.normpath(p).startswith(ff_dir + os.sep)
            and os.path.basename(os.path.normpath(p)).lower() in FFPROBE_DLL_NAMES
        ]
        check(len(ffprobe_dll_srcs) == FFPROBE_DLL_COUNT,
              "the full ffprobe companion closure is bundled (%d)"
              % len(ffprobe_dll_srcs))
        check(all(entries[os.path.normpath(p)] == os.path.normpath(FFPROBE_DEST)
                  for p in ffprobe_dll_srcs),
              "every ffprobe companion DLL lands beside ffprobe.exe (%s)"
              % FFPROBE_DEST)

        # ffmpeg.exe is the ACLOS build; its companion DLLs must ride along.
        # ffprobe's av*/sw* closure shares this directory, so it is excluded here
        # to keep this count about ffmpeg.exe's own set.
        ff_dlls = [p for p in entries
                   if os.path.normpath(p).startswith(ff_dir + os.sep)
                   and p.lower().endswith(".dll")
                   and os.path.basename(os.path.normpath(p)).lower()
                   not in FFPROBE_DLL_NAMES]
        check(os.path.normpath(runtime_path(
                  root, FFMPEG_DIR_RELPATH + (FFMPEG_DLL_REQUIRED,))) in entries,
              "the FFmpeg runtime library (libpag.dll) is bundled")
        check(len(ff_dlls) >= FFMPEG_DLL_MIN,
              "the full FFmpeg companion DLL set is bundled (%d)" % len(ff_dlls))
        check(all(entries[os.path.normpath(p)] == os.path.normpath(FFMPEG_DEST)
                  for p in ff_dlls),
              "every FFmpeg companion DLL lands beside ffmpeg.exe (%s)" % FFMPEG_DEST)

        # The PP-OCRv6 model is *gated* as a required build input, but its datas
        # entry is deliberately not written yet: shipping the model directory and
        # the Paddle/PaddleX runtime is the PP-OCRv6 packaging phase's job. Record
        # which of the two states the spec is in, so the flip is visible.
        ppocr_src = os.path.normpath(runtime_path(root, PPOCR_PARAMS_RELPATH))
        if ppocr_src in entries:
            check(entries[ppocr_src] == os.path.normpath(PPOCR_DEST),
                  "the PP-OCRv6 model is bundled to %s" % PPOCR_DEST)
        else:
            skip("PP-OCRv6 model datas entry not written yet "
                 "(PP-OCRv6 packaging phase)")

        # --------------------------------------------------------------
        print("\n[5] FFmpeg runtime library missing")
        root = make_tree(temp / "noffcore", with_key=True, with_helper=True,
                         ffmpeg_dll_count=0)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check("FFmpeg runtime library" in report,
                  "it names the missing FFmpeg runtime library")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[6] FFmpeg runtime present but incomplete")
        root = make_tree(temp / "partialff", with_key=True, with_helper=True,
                         ffmpeg_dll_count=1)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            message = str(error)
            check("FFmpeg runtime is incomplete" in message,
                  "it says the FFmpeg runtime is incomplete")
            check(str(FFMPEG_DLL_MIN) in message,
                  "it states how many DLLs are expected")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[6a] ffprobe runtime library missing")
        root = make_tree(temp / "noffprobecore", with_key=True, with_helper=True,
                         ffprobe_dll_count=0)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check("FFprobe runtime library" in report,
                  "it names the missing ffprobe runtime library")
            check(FFPROBE_DLL_REQUIRED in report,
                  "it names the exact missing library (%s)" % FFPROBE_DLL_REQUIRED)
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[6b] ffprobe runtime present but incomplete")
        root = make_tree(temp / "partialffprobe", with_key=True,
                         with_helper=True, ffprobe_dll_count=1)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            message = str(error)
            check("ffprobe runtime is incomplete" in message,
                  "it says the ffprobe runtime is incomplete")
            check("found 1 of %d" % FFPROBE_DLL_COUNT in message,
                  "it states how many of the %d are present" % FFPROBE_DLL_COUNT)
            # the fixture provisioned only the core library (index 0)
            missing_names = FFPROBE_DLL_NAMES[1:]
            check(all(name in message for name in missing_names),
                  "it names every missing library (%d)" % len(missing_names))
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[7] PP-OCRv6 model missing")
        root = make_tree(temp / "noppocr", with_key=True, with_helper=True,
                         with_ppocr_model=False)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check("PP-OCRv6 model" in report,
                  "it names the missing PP-OCRv6 model")
            check(PPOCR_MODEL_DIR_NAME in report,
                  "it names the model directory (%s)" % PPOCR_MODEL_DIR_NAME)
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[8] PP-OCRv6 model present but empty")
        root = make_tree(temp / "emptyppocr", with_key=True, with_helper=True,
                         ppocr_params=b"")
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            message = str(error)
            check("PP-OCRv6 model is empty" in message,
                  "it says the PP-OCRv6 model is empty")
            check("0-byte" in message,
                  "it explains that a 0-byte placeholder is not provisioned")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[9] the real repository")
        error, namespace, calls = exec_spec(ROOT)
        # The model is gated but not shipped yet, so the real repository is
        # expected to abort *until* the PP-OCRv6 packaging phase provisions it.
        # This case flips to "accepted" on its own once it does.
        if os.path.isfile(runtime_path(ROOT, PPOCR_PARAMS_RELPATH)):
            check(error is None,
                  "the real repository is accepted (model provisioned)")
            check(calls["analysis"] == 1, "analysis ran exactly once")
        else:
            check(isinstance(error, FileNotFoundError),
                  "the real repository aborts until the PP-OCRv6 model is "
                  "provisioned (%s)" % type(error).__name__)
            check(PPOCR_MODEL_DIR_NAME in str(error),
                  "the abort names the PP-OCRv6 model that is still missing")
            check(calls["analysis"] == 0, "analysis never ran")
        check(SPEC.is_file(), "the spec file exists")

        # --------------------------------------------------------------
        print("\n[10] real PyInstaller aborts on a tree missing the inputs")
        if not pyinstaller_available():
            skip("PyInstaller is not installed")
        else:
            bare = make_tree(temp / "bare", with_key=False, with_helper=False)
            result = subprocess.run(
                [sys.executable, "-m", "PyInstaller", "--noconfirm",
                 "--distpath", str(bare / "dist"),
                 "--workpath", str(bare / "build"),
                 str(SPEC)],
                cwd=str(bare), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
            output = (result.stdout or "") + (result.stderr or "")
            check(result.returncode != 0,
                  "PyInstaller exits non-zero (got %d)" % result.returncode)
            check("BUILD ABORTED" in output,
                  "the abort message reaches the build output")
            check(helper_path(bare) in output,
                  "the output names the exact missing helper path")
            check(not (bare / "dist").exists()
                  or not any((bare / "dist").iterdir()),
                  "no distribution was produced")

    print("\n" + "=" * 66)
    print("RESULT: %d/%d checks passed, %d failed, %d skipped"
          % (PASSED, PASSED + FAILED, FAILED, SKIPPED))
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
