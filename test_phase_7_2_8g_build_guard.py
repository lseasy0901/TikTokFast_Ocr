#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8G: the release build must fail loudly, not silently.

douyin_viewer.spec bundles artifacts that are NOT in Git:

    license_public_key.pem                       (.gitignore:22)
    susi_helper/target/release/susi_helper.exe   (gitignored Rust target tree)
    runtime/ffmpeg/ffmpeg.exe                    (third-party binary)
    runtime/ffmpeg/*.dll                         (their required companion DLLs)
    runtime/tesseract/tesseract.exe              (third-party binary)
    runtime/tesseract/tessdata/eng.traineddata   (third-party data)
    runtime/tesseract/*.dll                      (its required companion DLLs)

runtime/ffmpeg/ffprobe.exe itself is tracked, but its av*/sw* companion closure is
not -- and a lone ffprobe.exe cannot start. Both the exe and the closure are guarded.

PyInstaller only *warns* when a `datas` source is missing and then builds anyway.
The resulting EXE cannot compute a machine code or verify a SignedLicense, and
cannot spawn FFmpeg, ffprobe or Tesseract, so a missing input surfaced as
"activation doesn't work", "no video", or a silently wrong frame size on a
customer's machine rather than as a build error.

The spec now verifies all of them up front and raises FileNotFoundError, before
PyInstaller does any work.

The spec resolves its inputs with os.path.abspath("."), so each case below runs
the real spec file with the working directory pointed at a throwaway tree that
mimics the repository layout. Nothing here builds a real EXE -- except the last
check, which runs PyInstaller for real and expects it to abort immediately.

Covers:
  [1] all inputs missing   -> abort, naming the exact key and helper paths
  [2] helper missing       -> abort, naming only the helper
  [3] public key missing   -> abort, naming only the key
  [4] all inputs present   -> accepted, datas destinations unchanged, ffprobe.exe
                              bundled beside ffmpeg.exe with its own av*/sw*
                              closure, and the full FFmpeg and Tesseract
                              companion DLL sets bundled
  [5] FFmpeg core lib missing    -> abort, naming it
  [6] FFmpeg set incomplete      -> abort, saying the runtime is incomplete
  [6a] ffprobe core lib missing  -> abort, naming it
  [6b] ffprobe set incomplete    -> abort, saying the runtime is incomplete
  [7] Tesseract core lib missing -> abort, naming it
  [8] Tesseract set incomplete   -> abort, saying the runtime is incomplete
  [9] the real repository  -> accepted
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

TESSERACT_RELPATH = ("runtime", "tesseract", "tesseract.exe")
TESSDATA_RELPATH = ("runtime", "tesseract", "tessdata", "eng.traineddata")
TESSERACT_DEST = os.path.join("runtime", "tesseract")
TESSDATA_DEST = os.path.join("runtime", "tesseract", "tessdata")

RUNTIME_RELPATHS = (FFMPEG_RELPATH, FFPROBE_RELPATH, TESSERACT_RELPATH,
                   TESSDATA_RELPATH)

# tesseract.exe is not self-contained: the spec requires its core library by name
# and the whole companion set to be present, so the fixture must provision both.
TESSERACT_DIR_RELPATH = ("runtime", "tesseract")
TESSERACT_DLL_REQUIRED = "libtesseract-5.dll"
TESSERACT_DLL_MIN = 26

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
              tess_dll_count=TESSERACT_DLL_MIN, ffmpeg_dll_count=FFMPEG_DLL_MIN,
              ffprobe_dll_count=FFPROBE_DLL_COUNT):
    """Build a throwaway repository root containing the requested inputs."""
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("ocr: {}\n", encoding="utf-8")
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
    if tess_dll_count:
        dll_dir = root.joinpath(*TESSERACT_DIR_RELPATH)
        dll_dir.mkdir(parents=True, exist_ok=True)
        (dll_dir / TESSERACT_DLL_REQUIRED).write_bytes(b"stub dll")
        for i in range(tess_dll_count - 1):
            (dll_dir / ("stub_companion_%02d.dll" % i)).write_bytes(b"stub dll")
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
            (TESSERACT_RELPATH, TESSERACT_DEST, "tesseract.exe"),
            (TESSDATA_RELPATH, TESSDATA_DEST, "eng.traineddata"),
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

        # tesseract.exe is not self-contained; its companion DLLs must ride along.
        tess_dir = os.path.normpath(runtime_path(root, TESSERACT_DIR_RELPATH))
        dll_srcs = [p for p in entries
                    if os.path.normpath(p).startswith(tess_dir + os.sep)
                    and p.lower().endswith(".dll")]
        check(os.path.normpath(runtime_path(
                  root, TESSERACT_DIR_RELPATH + (TESSERACT_DLL_REQUIRED,))) in entries,
              "the Tesseract core library (libtesseract-5.dll) is bundled")
        check(len(dll_srcs) >= TESSERACT_DLL_MIN,
              "the full companion DLL set is bundled (%d)" % len(dll_srcs))
        check(all(entries[os.path.normpath(p)] == os.path.normpath(TESSERACT_DEST)
                  for p in dll_srcs),
              "every companion DLL lands beside tesseract.exe (%s)" % TESSERACT_DEST)

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
        print("\n[7] Tesseract core library missing")
        root = make_tree(temp / "nocore", with_key=True, with_helper=True,
                         tess_dll_count=0)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            report = missing_section(str(error))
            check("Tesseract core library" in report,
                  "it names the missing core library")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[8] Tesseract runtime present but incomplete")
        root = make_tree(temp / "partialtess", with_key=True, with_helper=True,
                         tess_dll_count=1)
        error, _, calls = exec_spec(root)
        check(isinstance(error, FileNotFoundError),
              "the spec raises FileNotFoundError")
        if isinstance(error, FileNotFoundError):
            message = str(error)
            check("Tesseract runtime is incomplete" in message,
                  "it says the Tesseract runtime is incomplete")
            check(str(TESSERACT_DLL_MIN) in message,
                  "it states how many DLLs are expected")
        check(calls["analysis"] == 0, "analysis never ran")

        # --------------------------------------------------------------
        print("\n[9] the real repository is accepted")
        _, namespace, calls = exec_spec(ROOT)
        # exec_spec against the real repo root would build nothing; this only
        # proves the guard passes where both artifacts really exist.
        check(calls["analysis"] == 1,
              "the real repository passes the guard and reaches analysis")
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
