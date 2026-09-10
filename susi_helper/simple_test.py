#!/usr/bin/env python3
"""
Simple test to verify Susi source structure
"""
import sys
import os
from pathlib import Path

def check_susi_source():
    """Check Susi source structure"""
    # Convert path separators
    base_path = os.getcwd()
    susi_source_path = Path(base_path) / "susi_source"

    print("Base path:", base_path)
    print("Checking Susi source at:", susi_source_path)

    if not susi_source_path.exists():
        print("ERROR: Susi source directory not found!")
        return False

    # Check for susi_core
    core_path = susi_source_path / "crates" / "susi_core"
    core_lib = core_path / "src" / "lib.rs"

    if core_path.exists():
        print("OK: susi_core crate found")
        if core_lib.exists():
            print("OK: susi_core/src/lib.rs exists")
            # Read the file to show exports
            with open(core_lib, 'r', encoding='utf-8') as f:
                content = f.read()
                print("\n=== susi_core/src/lib.rs exports ===")
                print(content[:500] + "...")
                print("\n=== End of exports ===")
        else:
            print("ERROR: susi_core/src/lib.rs not found")
            return False
    else:
        print("ERROR: susi_core crate not found")
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