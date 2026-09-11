#!/usr/bin/env python3
"""
Helper to load test RSA key consistently for all tests
"""

import os

def load_test_rsa_key():
    """Load the test RSA key from file with absolute path"""
    test_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(test_dir, 'test_rsa_key.pem')

    with open(key_path, 'r') as f:
        private_key_pem = f.read().strip()

    # Verify key is not empty
    if not private_key_pem:
        raise RuntimeError(f"Test RSA key file is empty: {key_path}")

    return private_key_pem

# For testing this helper
if __name__ == '__main__':
    key = load_test_rsa_key()
    print("Test key loader works!")