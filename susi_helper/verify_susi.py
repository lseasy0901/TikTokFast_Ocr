#!/usr/bin/env python3
"""
Simple test to verify Susi source structure
"""
import sys
import os
from pathlib import Path

def check_susi_source():
    """Check Susi source structure"""
    # Use absolute path
    parent_dir = Path(r"D:\TIikTok_Ocr\DouyinLowLatencyViewer")
    susi_path = parent_dir / "susi_source"

    print("Parent directory:", parent_dir)
    print("Checking Susi source at:", susi_path)
    print("Susi path exists:", susi_path.exists())

    if not susi_path.exists():
        print("ERROR: Susi source directory not found!")
        return False

    # Check for susi_core
    core_path = susi_path / "crates" / "susi_core"
    core_lib = core_path / "src" / "lib.rs"

    print("Core path:", core_path)
    print("Core path exists:", core_path.exists())

    if core_path.exists():
        print("OK: susi_core crate found")
        if core_lib.exists():
            print("OK: susi_core/src/lib.rs exists")
            # Read the file to show exports
            with open(core_lib, 'r', encoding='utf-8') as f:
                content = f.read()
                print("\n=== susi_core/src/lib.rs exports ===")
                print(content)
                print("\n=== End of exports ===")
        else:
            print("ERROR: susi_core/src/lib.rs not found")
            return False
    else:
        print("ERROR: susi_core crate not found")
        return False

    # Check other key files
    print("\n=== Checking Susi core modules ===")

    # Check crypto module
    crypto_path = core_path / "src" / "crypto.rs"
    if crypto_path.exists():
        print("OK: crypto.rs module found")
    else:
        print("ERROR: crypto.rs module not found")
        return False

    # Check fingerprint module
    fingerprint_path = core_path / "src" / "fingerprint.rs"
    if fingerprint_path.exists():
        print("OK: fingerprint.rs module found")
    else:
        print("ERROR: fingerprint.rs module not found")
        return False

    # Check license module
    license_path = core_path / "src" / "license.rs"
    if license_path.exists():
        print("OK: license.rs module found")
    else:
        print("ERROR: license.rs module not found")
        return False

    return True

def show_integration_plan():
    """Show the integration plan"""
    print("\n=== Integration Plan ===")
    print("1. Rust helper will use real susi_core library")
    print("2. Helper implements JSON stdin/stdout protocol")
    print("3. Python app calls helper.exe")
    print("4. Helper uses susi_core functions:")
    print("   - get_machine_code() for Windows fingerprint")
    print("   - sign_license() for backend (only)")
    print("   - verify_license() for client verification")
    print("5. Business logic remains in Python layer")

if __name__ == "__main__":
    print("Phase 7.2-3: Susi Integration Test")
    print("=" * 40)

    os.chdir(Path(__file__).parent)
    print("Changed directory to:", os.getcwd())

    if check_susi_source():
        print("\nSUCCESS: Susi source structure verified!")
        show_integration_plan()

        print("\n=== Next Steps ===")
        print("1. Install Rust on Windows")
        print("2. Build susi_helper.exe")
        print("3. Test with real Susi functions")
        print("4. Integrate with Python subprocess")
    else:
        print("\nFAILURE: Susi source verification failed!")
        sys.exit(1)