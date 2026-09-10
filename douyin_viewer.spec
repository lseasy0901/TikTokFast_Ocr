# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Douyin Low Latency Viewer
Windows standalone EXE packaging
"""

import os
import sys

# Get the absolute path of the project directory
PROJECT_ROOT = os.path.abspath(".")

# Data files to include
datas = [
    # Config file
    (os.path.join(PROJECT_ROOT, "config.yaml"), "."),
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