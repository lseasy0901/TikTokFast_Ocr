# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Douyin Low Latency Viewer
Windows standalone EXE packaging
"""

import os
import sys

# Get the absolute path of the project directory
PROJECT_ROOT = os.path.abspath(".")

# ---------------------------------------------------------------------------
# Required build inputs (Phase 7.2-8G)
#
# Two of the bundled artifacts are deliberately NOT in Git -- both are gitignored
# and provisioned per deployment or built locally:
#
#   license_public_key.pem                        (.gitignore:22)
#   susi_helper/target/release/susi_helper.exe    (Rust target tree, gitignored)
#
# PyInstaller only *warns* when a `datas` source is missing and then produces an
# EXE anyway. That EXE cannot compute a machine code or verify a SignedLicense,
# so the failure surfaces as "activation does not work" on a customer's machine
# instead of as a build error here. Verify both up front and stop the build.
#
# config.yaml is intentionally not in this list: it is tracked in Git, so it
# cannot go missing from a clone.
# ---------------------------------------------------------------------------
_PUBLIC_KEY_SRC = os.path.join(PROJECT_ROOT, "license_public_key.pem")
_SUSI_HELPER_SRC = os.path.join(
    PROJECT_ROOT, "susi_helper", "target", "release", "susi_helper.exe"
)
_SUSI_HELPER_DEST = os.path.join("susi_helper", "target", "release")


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
        "PyInstaller would otherwise emit an EXE that cannot activate a license.",
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
        "=" * 72,
        "",
    ]
    raise FileNotFoundError("\n".join(report))


require_build_inputs(
    (
        ("license_public_key.pem", _PUBLIC_KEY_SRC),
        ("susi_helper executable", _SUSI_HELPER_SRC),
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
]

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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
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

# Create additional executables if needed (like FFmpeg)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DouyinLowLatencyViewer',
)