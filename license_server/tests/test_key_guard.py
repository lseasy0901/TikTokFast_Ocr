# -*- coding: utf-8 -*-
"""密钥防呆回归(2026-11-05 P0):公开分发的测试密钥绝不允许签发凭据。"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

import config  # noqa: E402
from services.susi_security_service import (  # noqa: E402
    SusiSecurityService, KNOWN_TEST_KEY_PUB_SHA256, _private_key_pub_sha256)


def _make_service(tmpdir, key_pem: str):
    key_file = os.path.join(tmpdir, "sign.pem")
    with open(key_file, "wb") as fh:
        fh.write(key_pem.encode())
    monkeypatched = type("S", (), {
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE": key_file,
        "SUSI_DEVELOPMENT_PRIVATE_KEY": "",
        "SUSI_DEVELOPMENT_PUBLIC_KEY": "",
    })()
    return SusiSecurityService(monkeypatched)


def test_test_key_forbidden(tmp_path):
    """仓库公开的 test_rsa_key.pem → create_signed_license 直接拒绝。"""
    with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "test_rsa_key.pem"), encoding="utf-8") as fh:
        test_pem = fh.read()
    svc = _make_service(str(tmp_path), test_pem)
    assert svc._signature_forbidden is True
    try:
        svc.create_signed_license(
            {"id": 1, "license_key": "h" * 64, "created_at": "2026-01-01",
             "features": [], "authorization": {"expires_at": None,
                                               "activated_at": None}},
            "device-x", 30)
        raise AssertionError("测试密钥签发必须被拒绝")
    except RuntimeError as e:
        assert "测试密钥" in str(e)


def test_random_key_allowed(tmp_path):
    """随机生成的(非公开)密钥 → 不触发防呆,可正常进入签名。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    assert _private_key_pub_sha256(pem) != KNOWN_TEST_KEY_PUB_SHA256
    svc = _make_service(str(tmp_path), pem)
    assert svc._signature_forbidden is False
