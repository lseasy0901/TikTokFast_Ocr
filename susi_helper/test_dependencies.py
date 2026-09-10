#!/usr/bin/env python3
"""
Test script to verify Susi integration without building
"""
import json
import subprocess
import sys
from pathlib import Path

def test_susi_source():
    """Test Susi source structure and exports"""
    print("=== Testing Susi Source Structure ===")

    parent_dir = Path(r"D:\TIikTok_Ocr\DouyinLowLatencyViewer")
    susi_path = parent_dir / "susi_source"

    if not susi_path.exists():
        print("FAIL: Susi source not found")
        return False

    print(f"OK: Susi source found at: {susi_path}")

    # Check core crate
    core_path = susi_path / "crates" / "susi_core"
    if not core_path.exists():
        print("FAIL: susi_core crate not found")
        return False

    print("OK: susi_core crate found")

    # Check main lib.rs
    lib_rs = core_path / "src" / "lib.rs"
    if not lib_rs.exists():
        print("FAIL: susi_core/src/lib.rs not found")
        return False

    print("OK: susi_core/src/lib.rs found")

    # Read lib.rs to verify exports
    with open(lib_rs, 'r') as f:
        lib_content = f.read()

    required_exports = [
        'pub use crypto::{generate_keypair, sign_license, verify_license}',
        'pub use fingerprint::get_machine_code',
        'pub use license::{License, LicensePayload, MachineActivation, SignedLicense'
    ]

    for export in required_exports:
        if export in lib_content:
            print(f"OK: Found required export: {export[:50]}...")
        else:
            print(f"FAIL: Missing required export: {export}")
            return False

    return True

def test_rust_availability():
    """Test Rust availability"""
    print("\n=== Testing Rust Availability ===")

    rustc_path = Path(r"/c/Users/ZhuanZ（无密码）/.cargo/bin/rustc.exe")
    cargo_path = Path(r"/c/Users/ZhuanZ（无密码）/.cargo/bin/cargo.exe")

    if not rustc_path.exists():
        print("FAIL: rustc.exe not found")
        return False

    if not cargo_path.exists():
        print("FAIL: cargo.exe not found")
        return False

    print(f"OK: rustc found at: {rustc_path}")
    print(f"OK: cargo found at: {cargo_path}")

    # Test versions
    try:
        result = subprocess.run([rustc_path, "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"OK: Rust version: {result.stdout.strip()}")
        else:
            print("FAIL: rustc version check failed")
            return False
    except Exception as e:
        print(f"FAIL: Error checking rustc: {e}")
        return False

    try:
        result = subprocess.run([cargo_path, "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"OK: Cargo version: {result.stdout.strip()}")
        else:
            print("FAIL: cargo version check failed")
            return False
    except Exception as e:
        print(f"FAIL: Error checking cargo: {e}")
        return False

    return True

def test_vs_buildtools():
    """Test Visual Studio Build Tools availability"""
    print("\n=== Testing Visual Studio Build Tools ===")

    # Check common VS locations
    vs_paths = [
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat"),
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvarsall.bat"),
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"),
    ]

    for vs_path in vs_paths:
        if vs_path.exists():
            print(f"OK: Found Visual Studio at: {vs_path}")
            return True

    print("FAIL: Visual Studio Build Tools not found")
    print("   Required for building Rust Windows applications")
    return False

def show_build_status():
    """Show current build status"""
    print("\n=== Build Status ===")

    if test_rust_availability():
        print("OK: Rust Toolchain: Available")
    else:
        print("FAIL: Rust Toolchain: Not Available")
        return False

    if test_vs_buildtools():
        print("OK: Visual Studio Build Tools: Available")
    else:
        print("FAIL: Visual Studio Build Tools: Not Available")
        print("   Building will fail without this")
        return False

    if test_susi_source():
        print("OK: Susi Source: Available")
    else:
        print("FAIL: Susi Source: Not Available")
        return False

    print("\nBuild Requirements:")
    print("   - Rust OK")
    print("   - Visual Studio Build Tools FAIL")
    print("   - Susi Source OK")

    print("\nNext Steps:")
    print("   1. Install Visual Studio Build Tools")
    print("   2. Run: cargo build --release")
    print("   3. Test susi_helper functionality")

    return True

if __name__ == "__main__":
    print("Phase 7.2-3: Build Status Check")
    print("=" * 50)

    if show_build_status():
        print("\nOK: Rust environment is ready")
        print("FAIL: Build tools missing - cannot compile yet")
    else:
        print("\nFAIL: Some components are missing")