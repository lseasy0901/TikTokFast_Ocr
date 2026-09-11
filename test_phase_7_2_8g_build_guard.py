#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-8G: the release build must fail loudly, not silently.

douyin_viewer.spec bundles two artifacts that are NOT in Git:

    license_public_key.pem                       (.gitignore:22)
    susi_helper/target/release/susi_helper.exe   (gitignored Rust target tree)

PyInstaller only *warns* when a `datas` source is missing and then builds anyway.
The resulting EXE cannot compute a machine code or verify a SignedLicense, so a
missing input turned into "activation doesn't work" on a customer's machine
rather than into a build error.

The spec now verifies both up front and raises FileNotFoundError, before
PyInstaller does any work.

The spec resolves its inputs with os.path.abspath("."), so each case below runs
the real spec file with the working directory pointed at a throwaway tree that
mimics the repository layout. Nothing here builds a real EXE -- except the last
check, which runs PyInstaller for real and expects it to abort immediately.

Covers:
  [1] both inputs missing  -> abort, naming both exact paths, no analysis
  [2] helper missing       -> abort, naming only the helper
  [3] public key missing   -> abort, naming only the key
  [4] both present         -> accepted, datas destinations unchanged
  [5] the real repository  -> accepted
  [6] real PyInstaller     -> exits non-zero on a tree missing the inputs
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


def make_tree(root, with_key, with_helper):
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
    return root


def key_path(root):
    return str(pathlib.Path(root) / PUBLIC_KEY_NAME)


def helper_path(root):
    return str(pathlib.Path(root).joinpath(*HELPER_RELPATH))


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
            check("cannot activate" in message,
                  "it explains the consequence (an EXE that cannot activate a license)")
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
        print("\n[4] both inputs present")
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

        # --------------------------------------------------------------
        print("\n[5] the real repository is accepted")
        _, namespace, calls = exec_spec(ROOT)
        # exec_spec against the real repo root would build nothing; this only
        # proves the guard passes where both artifacts really exist.
        check(calls["analysis"] == 1,
              "the real repository passes the guard and reaches analysis")
        check(SPEC.is_file(), "the spec file exists")

        # --------------------------------------------------------------
        print("\n[6] real PyInstaller aborts on a tree missing the inputs")
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
