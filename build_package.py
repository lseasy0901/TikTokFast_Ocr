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

    # Download and prepare Tesseract
    print("3. Preparing Tesseract...")
    if not prepare_tesseract(project_root):
        print("Error: Failed to prepare Tesseract")
        return 1

    # Create icon (if needed)
    # create_icon(project_root)

    # Build with PyInstaller
    print("4. Building with PyInstaller...")
    if not build_with_pyinstaller(spec_file, dist_dir):
        print("Error: PyInstaller build failed")
        return 1

    # Post-process build
    print("5. Post-processing build...")
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
    """Download and prepare FFmpeg binaries"""
    runtime_ffmpeg_dir = project_root / "runtime" / "ffmpeg"

    # Check if FFmpeg already exists
    ffmpeg_exe = runtime_ffmpeg_dir / "ffmpeg.exe"
    ffprobe_exe = runtime_ffmpeg_dir / "ffprobe.exe"

    if ffmpeg_exe.exists() and ffprobe_exe.exists():
        print("  FFmpeg already prepared")
        return True

    # For now, copy from system if available
    # In production, you would download official FFmpeg builds
    ffmpeg_paths = [
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]

    ffmpeg_found = False
    for ffmpeg_path in ffmpeg_paths:
        if os.path.exists(ffmpeg_path):
            print(f"  Found FFmpeg at: {ffmpeg_path}")

            # Create runtime directory
            runtime_ffmpeg_dir.mkdir(parents=True, exist_ok=True)

            # Copy FFmpeg and related DLLs
            ffmpeg_dir = os.path.dirname(ffmpeg_path)
            for file in os.listdir(ffmpeg_dir):
                if file.lower().startswith('ffmpeg') or file.lower().endswith('.dll'):
                    shutil.copy2(os.path.join(ffmpeg_dir, file), runtime_ffmpeg_dir)
                    print(f"  Copied: {file}")

            ffmpeg_found = True
            break

    if not ffmpeg_found:
        print("  Warning: FFmpeg not found in standard locations")
        print("  You will need to manually place ffmpeg.exe in runtime/ffmpeg/")
        # Create placeholder files for now
        runtime_ffmpeg_dir.mkdir(parents=True, exist_ok=True)
        (runtime_ffmpeg_dir / "ffmpeg.exe").touch()
        (runtime_ffmpeg_dir / "ffprobe.exe").touch()
        return True

    return True

def prepare_tesseract(project_root):
    """Download and prepare Tesseract OCR"""
    runtime_tesseract_dir = project_root / "runtime" / "tesseract"

    # Check if Tesseract already exists
    tesseract_exe = runtime_tesseract_dir / "tesseract.exe"
    tessdata_dir = runtime_tesseract_dir / "tessdata"
    eng_data = tessdata_dir / "eng.traineddata"

    if tesseract_exe.exists() and eng_data.exists():
        print("  Tesseract already prepared")
        return True

    # Create directories
    runtime_tesseract_dir.mkdir(parents=True, exist_ok=True)
    tessdata_dir.mkdir(parents=True, exist_ok=True)

    # Check for existing Tesseract installation
    tesseract_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]

    tesseract_found = False
    for tesseract_path in tesseract_paths:
        if os.path.exists(tesseract_path):
            print(f"  Found Tesseract at: {tesseract_path}")

            # Copy tesseract.exe
            shutil.copy2(tesseract_path, runtime_tesseract_dir)
            tesseract_found = True

            # Copy tessdata
            system_tessdata = os.path.dirname(tesseract_path)
            if os.path.exists(os.path.join(system_tessdata, "tessdata", "eng.traineddata")):
                system_eng_data = os.path.join(system_tessdata, "tessdata", "eng.traineddata")
                shutil.copy2(system_eng_data, tessdata_dir)
                print("  Copied: eng.traineddata")

            break

    if not tesseract_found:
        print("  Warning: Tesseract not found in standard locations")
        print("  You will need to manually place tesseract.exe and eng.traineddata")
        # Create placeholder files
        tesseract_exe.touch()
        eng_data.touch()
        return True

    return True

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
- runtime/ - Runtime dependencies (FFmpeg, Tesseract)
"""

    readme_path = dist_dir / "README.txt"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)

if __name__ == "__main__":
    sys.exit(main())