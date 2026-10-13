# -*- coding: utf-8 -*-
"""
Package build script for Douyin Low Latency Viewer

This script prepares and packages the application using PyInstaller.
"""

import os
import sys
import shutil
import subprocess
import time
from pathlib import Path

def main():
    """Main build process"""
    print("=== Douyin Low Latency Viewer Build Script ===\n")

    # Configuration
    project_root = Path(__file__).parent
    spec_file = project_root / "douyin_viewer.spec"
    dist_dir = project_root / "dist" / "release"
    build_dir = project_root / "build"

    # Clean previous builds
    print("1. Cleaning previous builds...")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    if build_dir.exists():
        shutil.rmtree(build_dir)

    # Create directories
    dist_dir.parent.mkdir(parents=True, exist_ok=True)

    # Download and prepare FFmpeg
    print("2. Preparing FFmpeg...")
    if not prepare_ffmpeg(project_root):
        print("Error: Failed to prepare FFmpeg")
        return 1

    # NOTE: this script used to run prepare_tesseract() here. Tesseract is no
    # longer the production OCR runtime (PP-OCRv6_medium_rec is), and its assets
    # were deleted from the repository, so that step is retired. It is NOT
    # replaced by another provisioning mechanism: the PP-OCRv6 model is supplied
    # by the packaging phase and gated in douyin_viewer.spec.

    # Create icon (if needed)
    # create_icon(project_root)

    # Build with PyInstaller
    print("3. Building with PyInstaller...")
    if not build_with_pyinstaller(spec_file, dist_dir):
        print("Error: PyInstaller build failed")
        return 1

    # Post-process build
    print("4. Post-processing build...")
    post_process_build(dist_dir, project_root)

    # Display results
    print("\n=== Build Complete! ===")
    exe_path = dist_dir / "DouyinLowLatencyViewer.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"Output EXE: {exe_path}")
        print(f"Package size: {size_mb:.1f} MB")

        # List included files
        print("\nIncluded files:")
        for root, dirs, files in os.walk(dist_dir):
            for file in files:
                if file.endswith('.exe') or file.endswith('.dll') or file.endswith('.pyd'):
                    rel_path = os.path.relpath(os.path.join(root, file), dist_dir)
                    file_size = os.path.getsize(os.path.join(root, file)) / (1024 * 1024)
                    print(f"  - {rel_path}: {file_size:.1f} MB")
    else:
        print("Error: EXE file not found after build")
        return 1

    return 0

def prepare_ffmpeg(project_root):
    """Prepare the bundled ffprobe runtime.

    RELEASE-02-B5-CLEANUP: this used to also stage ``ffmpeg.exe`` (the ACLOS
    recorder's build) for the old ``_uhd`` pull path. That binary is gone --
    playback now uses the external FFmpeg 7 runtime located by
    ``utils/ffmpeg7_runtime.py``, which is intentionally NOT bundled.

    What remains in ``runtime/ffmpeg/`` is ``ffprobe.exe`` plus its ``av*``/``sw*``
    companion DLLs (used for the resolution probe fallback), and both are required
    by ``douyin_viewer.spec``.
    """
    runtime_ffmpeg_dir = project_root / "runtime" / "ffmpeg"

    ffprobe_exe = runtime_ffmpeg_dir / "ffprobe.exe"

    if ffprobe_exe.exists() and ffprobe_exe.stat().st_size > 0:
        print("  ffprobe already prepared")
        return True

    # For now, copy from system if available.
    # In production, you would download an official FFmpeg build.
    ffprobe_paths = [
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe",
        r"C:\ffmpeg\bin\ffprobe.exe",
    ]

    for ffprobe_path in ffprobe_paths:
        if os.path.exists(ffprobe_path):
            print(f"  Found ffprobe at: {ffprobe_path}")
            runtime_ffmpeg_dir.mkdir(parents=True, exist_ok=True)

            src_dir = os.path.dirname(ffprobe_path)
            for file in os.listdir(src_dir):
                low = file.lower()
                # 只要 ffprobe 本体与 av*/sw* 依赖闭包；ffmpeg.exe 不再随包分发
                if low == "ffprobe.exe" or low.startswith(("av", "sw")):
                    shutil.copy2(os.path.join(src_dir, file), runtime_ffmpeg_dir)
                    print(f"  Copied: {file}")
            return True

    print("  Warning: ffprobe not found in standard locations")
    print("  You will need to manually place ffprobe.exe (plus its av*/sw* DLLs)")
    print("  in runtime/ffmpeg/ before building.")
    return False

def build_with_pyinstaller(spec_file, dist_dir):
    """Build the application using PyInstaller"""
    try:
        # Install PyInstaller if not available
        try:
            import PyInstaller
            print("  PyInstaller already installed")
        except ImportError:
            print("  Installing PyInstaller...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

        # Run PyInstaller
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            str(spec_file)
        ]

        print(f"  Running: {' '.join(cmd)}")
        subprocess.check_call(cmd)

        return True
    except subprocess.CalledProcessError as e:
        print(f"  Build failed: {e}")
        return False

def post_process_build(dist_dir, project_root):
    """Post-process the built application"""
    # Move EXE to release directory
    exe_files = list(dist_dir.glob("*.exe"))
    for exe in exe_files:
        new_path = dist_dir / "DouyinLowLatencyViewer.exe"
        shutil.move(str(exe), str(new_path))

    # Clean up unnecessary files
    unnecessary_files = [
        "warn-DouyinLowLatencyViewer.txt",
        "*.log",
        "*.py",
        "__pycache__",
        ".DS_Store",
    ]

    for pattern in unnecessary_files:
        for file_path in dist_dir.rglob(pattern):
            if file_path.is_file():
                file_path.unlink()
            elif file_path.is_dir():
                shutil.rmtree(file_path)

    # Create a simple readme
    readme_content = """# Douyin Low Latency Viewer

This is a standalone Windows application for viewing Douyin streams with OCR capabilities.

## Requirements
- Windows 10 or later
- No Python installation required

## Usage
1. Run DouyinLowLatencyViewer.exe
2. Enter a Douyin stream URL
3. Select ROI for OCR
4. The app will automatically recognize and copy text to clipboard

## Files
- DouyinLowLatencyViewer.exe - Main application
- runtime/ - Runtime dependencies (FFmpeg; PP-OCRv6 model under runtime/ppocr/)
"""

    readme_path = dist_dir / "README.txt"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)

if __name__ == "__main__":
    sys.exit(main())