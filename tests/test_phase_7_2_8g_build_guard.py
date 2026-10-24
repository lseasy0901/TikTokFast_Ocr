#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8G: the release build must fail loudly, not silently.

douyin_viewer.spec bundles artifacts that are NOT in Git:

    license_public_key.pem                                  (.gitignore:22)
    susi_helper/target/release/susi_helper.exe              (gitignored Rust target)
    runtime/ffmpeg/ffmpeg.exe                               (third-party binary)
    runtime/ffmpeg/*.dll                                    (its companion DLLs)
    runtime/onnx/                                           (production OCR artifacts)

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
  [7] ONNX model missing         -> abort, naming the model file
  [8] ONNX model empty           -> abort, saying the model is empty
  [8b] ONNX charset empty        -> abort, saying the charset is empty
  [9] the real repository  -> accepted (both ONNX artifacts are provisioned in
                              runtime/onnx/); aborts naming them until then
  [10] real PyInstaller    -> exits non-zero on a tree missing the inputs
"""

import os
import pathlib
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
#
# RELEASE-02-B5-CLEANUP: runtime/ffmpeg/ffmpeg.exe is GONE. It was the ACLOS
# recorder's build (hard-importing libpag.dll / o264rtenc.dll / ANGLE / MSVC at
# process start) and existed only for the old `_uhd` pull path. Playback now uses
# the external FFmpeg 7 runtime located by utils/ffmpeg7_runtime.py, which is
# deliberately NOT bundled. The assertions below therefore check the *absence*
# of that binary rather than its presence.
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

# ACLOS-specific DLLs that only ffmpeg.exe needed. None of them may reappear in
# the bundle now that the binary is gone.
FFMPEG_ONLY_DLLS = (
    "libpag.dll",
    "o264rtenc.dll",
    "libEGL.dll",
    "libGLESv2.dll",
)

# ONNX Runtime is the production OCR backend, so the build has to ship its two
# artifacts. The spec gates on both (it mirrors
# ONNXModelLocator.is_usable_model_file(), which rejects a 0-byte placeholder) and
# the destination must match the locator's probe path:
#   <base>/runtime/onnx/PP-OCRv6_medium_rec_fp32.onnx
#   <base>/runtime/onnx/charset.txt
ONNX_SUBDIR = "onnx"
ONNX_MODEL_NAME = "PP-OCRv6_medium_rec_fp32.onnx"
ONNX_CHARSET_NAME = "charset.txt"
ONNX_RELPATH = ("runtime", ONNX_SUBDIR)
ONNX_MODEL_RELPATH = ONNX_RELPATH + (ONNX_MODEL_NAME,)
ONNX_CHARSET_RELPATH = ONNX_RELPATH + (ONNX_CHARSET_NAME,)
ONNX_DEST = os.path.join("runtime", ONNX_SUBDIR)

RUNTIME_RELPATHS = (FFPROBE_RELPATH,)

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
              ffprobe_dll_count=FFPROBE_DLL_COUNT,
              with_onnx_artifacts=True, onnx_model=b"stub onnx model",
              onnx_charset=b"a\nb\n"):
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
    if ffprobe_dll_count:
        dll_dir = root.joinpath(*FFPROBE_RELPATH[:-1])
        dll_dir.mkdir(parents=True, exist_ok=True)
        for name in FFPROBE_DLL_NAMES[:ffprobe_dll_count]:
            (dll_dir / name).write_bytes(b"stub dll")
    if with_onnx_artifacts:
        onnx_dir = root.joinpath(*ONNX_RELPATH)
        onnx_dir.mkdir(parents=True, exist_ok=True)
        (onnx_dir / ONNX_MODEL_NAME).write_bytes(onnx_model)
        (onnx_dir / ONNX_CHARSET_NAME).write_bytes(onnx_charset)
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

        ff_dir = os.path.normpath(runtime_path(root, FFPROBE_RELPATH[:-1]))

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

        # RELEASE-02-B5-CLEANUP: ffmpeg.exe and its ACLOS-only DLL closure must
        # NOT be bundled any more -- playback uses the external FFmpeg 7 runtime.
        ffmpeg_src = os.path.normpath(runtime_path(
            root, ("runtime", "ffmpeg", "ffmpeg.exe")))
        check(ffmpeg_src not in entries,
              "ffmpeg.exe is NOT bundled (old `_uhd` pull path removed)")
        stray = [p for p in entries
                 if os.path.basename(os.path.normpath(p)).lower()
                 in {n.lower() for n in FFMPEG_ONLY_DLLS}]
        check(not stray,
              "no ACLOS-only DLL rides along (%s) %s"
              % (", ".join(FFMPEG_ONLY_DLLS),
                 [os.path.basename(p) for p in stray]))

        # Both ONNX artifacts must be gated AND land in the directory
        # ONNXModelLocator probes. The charset is checked alongside the model on
        # purpose: a missing charset.txt is exactly as fatal and just as invisible
        # (recognition would decode every index wrong without raising).
        for name, relpath in ((ONNX_MODEL_NAME, ONNX_MODEL_RELPATH),
                              (ONNX_CHARSET_NAME, ONNX_CHARSET_RELPATH)):
            src = os.path.normpath(runtime_path(root, relpath))
            check(src in entries, "the spec bundles %s as a datas entry" % name)
            if src in entries:
                check(entries[src] == os.path.normpath(ONNX_DEST),
                      "%s is bundled to %s" % (name, ONNX_DEST))

        # --------------------------------------------------------------
        # RELEASE-02-B5-CLEANUP: sections [5] and [6] used to prove the spec
        # aborts when runtime/ffmpeg/ffmpeg.exe's own DLL closure was missing
        # (libpag.dll / partial set). That binary and its guard are gone.
        print("\n[5] ffprobe runtime library missing")
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
        print("\n[7] ONNX model missing")
        root = make_tree(temp / "noonnx", with_key=True, with_helper=True,
                         with_onnx_artifacts=False)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check("ONNX OCR model" in report,
                  "it names the missing ONNX OCR model")
            check(ONNX_MODEL_NAME in report,
                  "it names the model file (%s)" % ONNX_MODEL_NAME)
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[8] ONNX model present but empty")
        root = make_tree(temp / "emptyonnx", with_key=True, with_helper=True,
                         onnx_model=b"")
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            message = str(error)
            check("ONNX OCR model is empty" in message,
                  "it says the ONNX OCR model is empty")
            check("0-byte" in message,
                  "it explains that a 0-byte placeholder is not provisioned")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[8b] ONNX charset present but empty")
        root = make_tree(temp / "emptycharset", with_key=True, with_helper=True,
                         onnx_charset=b"")
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            check("ONNX charset is empty" in str(error),
                  "it says the ONNX charset is empty")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[9] the real repository")
        error, namespace, calls = exec_spec(ROOT)
        # Both artifacts are provisioned in runtime/onnx/, so the real repository
        # must now be ACCEPTED -- this is the flip the migration is about.
        if (os.path.isfile(runtime_path(ROOT, ONNX_MODEL_RELPATH))
                and os.path.isfile(runtime_path(ROOT, ONNX_CHARSET_RELPATH))):
            check(error is None,
                  "the real repository is accepted (ONNX artifacts provisioned)")
            check(calls["analysis"] == 1, "analysis ran exactly once")
        else:
            check(isinstance(error, FileNotFoundError),
                  "the real repository aborts until the ONNX artifacts are "
                  "provisioned (%s)" % type(error).__name__)
            check(ONNX_MODEL_NAME in str(error),
                  "the abort names the ONNX artifact that is still missing")
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
