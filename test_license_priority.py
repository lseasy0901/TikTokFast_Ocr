#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线授权 / 离线灾备授权 优先级的验收测试（最小回归）。

本次修复的唯一目标——固定优先级：

    有效 Online  >  有效 Offline  >  未激活

覆盖需求里列出的边界场景：
    1  Online 有效 + Offline 有效   -> Online
    2  Online 有效 + Offline 不存在 -> Online
    3  Online 过期 + Offline 有效   -> Offline
    4  Online 不存在 + Offline 有效 -> Offline
    5  都不成立                     -> 未激活（在线凭据存在但无效时保留其具体结论）
    6  在线激活成功                 -> 不删除 offline_license.json、不改 offline_keys.json

用测试 RSA 私钥 + 真实 susi_helper 构造**真实签名**的凭据，所以本测试会调用
susi_helper.exe（与 license_server/test_phase_7_2_6_5.py 的做法一致）。
不联网、不读写真实 %APPDATA%：全部落在 tempfile 数据目录里。
"""

import json
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "license_server"))

from test_key_loader import load_test_rsa_key, load_test_rsa_public_key  # noqa: E402

from utils import offline_activation as oa  # noqa: E402
from utils.license_manager import LicenseManager, LicenseStatus  # noqa: E402
from utils.license_store import LicenseStore  # noqa: E402
from utils.susi_verifier import SusiVerifier, default_helper_path  # noqa: E402

PRIVATE_KEY_PEM = load_test_rsa_key()
PUBLIC_KEY_PEM = load_test_rsa_public_key()
OFFLINE_FILE = oa.OFFLINE_LICENSE_FILENAME
USED_KEYS_FILE = oa.USED_KEYS_FILENAME

_PASS = []
_FAIL = []


def check(name, condition, detail=""):
    (_PASS if condition else _FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'}  {name}"
          f"{('  -- ' + str(detail)) if detail and not condition else ''}")


# ----------------------------------------------------------------------
# 构造真实签名的凭据
# ----------------------------------------------------------------------
def _rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat()


def sign_payload(payload):
    """用测试私钥真实签名一份凭据（走 susi_helper，不做任何伪造）。"""
    proc = subprocess.run(
        [default_helper_path()],
        input=json.dumps({
            "command": "SignLicense",
            "private_key_pem": PRIVATE_KEY_PEM,
            "payload": payload,
        }),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    outer = json.loads(proc.stdout)
    if "Success" not in outer:
        raise RuntimeError(f"签名失败: {proc.stdout[:200]}")
    return outer["Success"]["data"]


def online_artifact(machine, days):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return sign_payload({
        "id": "priority-test",
        "product": "DouyinLowLatencyViewer",
        "customer": "DouyinLowLatencyViewer",
        "license_key": "0" * 64,
        "created": _rfc3339(now - timedelta(days=1)),
        "expires": _rfc3339(now + timedelta(days=days)),
        "features": [],
        "machine_codes": [machine],
        "require_signed_binary": False,
    })


# ----------------------------------------------------------------------
# 数据目录准备
# ----------------------------------------------------------------------
def make_manager(data_dir, verifier):
    return LicenseManager(
        server_url="http://127.0.0.1:1",
        store=LicenseStore(data_dir=data_dir),
        verifier=verifier,
    )


def seed_offline_valid(data_dir, hours=24):
    """写一份有效的离线灾备授权（走真实 API）。"""
    return oa.OfflineActivation(data_dir).save_license(hours)


def seed_offline_expired(data_dir):
    """写一份已过期的离线灾备授权（无 API 可造，直接落盘同样结构）。"""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    record = {
        "version": 1,
        "expires_at": past.replace(microsecond=0).isoformat(),
        "activated_at": (past - timedelta(days=30)).replace(microsecond=0).isoformat(),
        "duration_hours": 720,
    }
    path = pathlib.Path(data_dir) / OFFLINE_FILE
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot(*paths):
    out = {}
    for p in paths:
        p = pathlib.Path(p)
        out[str(p)] = p.read_bytes() if p.is_file() else None
    return out


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------
def run(verifier, machine):
    print("\n[1] Online 有效 + Offline 有效 -> Online")
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        m.store.save(online_artifact(machine, 30), machine, "http://127.0.0.1:1")
        seed_offline_valid(d, hours=24)          # 离线先到期，用来区分两者
        r = m.load_stored()
        check("1 状态为已激活", r.status is LicenseStatus.ACTIVATED, r.status.value)
        check("1 采用 Online（非离线）", "已激活" == r.message, r.message)
        check("1 到期时间来自 Online（约 30 天）",
              r.expires_at is not None and 29.5 < (r.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400 < 30.5,
              r.expires_at)

    print("\n[2] Online 有效 + Offline 不存在 -> Online")
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        m.store.save(online_artifact(machine, 30), machine, "http://127.0.0.1:1")
        r = m.load_stored()
        check("2 状态为已激活", r.status is LicenseStatus.ACTIVATED, r.status.value)
        check("2 采用 Online", "已激活" == r.message, r.message)

    print("\n[3] Online 过期 + Offline 有效 -> Offline")
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        m.store.save(online_artifact(machine, -1), machine, "http://127.0.0.1:1")
        offline_expires = seed_offline_valid(d, hours=24)
        r = m.load_stored()
        check("3 状态为已激活", r.status is LicenseStatus.ACTIVATED, r.status.value)
        check("3 回退到 Offline", "离线授权有效" == r.message, r.message)
        check("3 到期时间来自 Offline",
              r.expires_at is not None and abs((r.expires_at - offline_expires).total_seconds()) < 1,
              r.expires_at)

    print("\n[4] Online 不存在 + Offline 有效 -> Offline")
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        offline_expires = seed_offline_valid(d, hours=24)
        r = m.load_stored()
        check("4 状态为已激活", r.status is LicenseStatus.ACTIVATED, r.status.value)
        check("4 采用 Offline", "离线授权有效" == r.message, r.message)
        check("4 到期时间来自 Offline",
              r.expires_at is not None and abs((r.expires_at - offline_expires).total_seconds()) < 1,
              r.expires_at)

    print("\n[5] 都不成立 -> 未激活 / 保留在线凭据的具体结论")
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        r = m.load_stored()
        check("5a 两份都不存在 -> NOT_ACTIVATED",
              r.status is LicenseStatus.NOT_ACTIVATED, r.status.value)

    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        m.store.save(online_artifact(machine, -1), machine, "http://127.0.0.1:1")
        r = m.load_stored()
        check("5b 在线过期 + 无离线 -> EXPIRED（保留可诊断结论）",
              r.status is LicenseStatus.EXPIRED, r.status.value)

    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        m.store.save(online_artifact(machine, -1), machine, "http://127.0.0.1:1")
        seed_offline_expired(d)
        r = m.load_stored()
        check("5c 在线过期 + 离线过期 -> 仍为 EXPIRED",
              r.status is LicenseStatus.EXPIRED, r.status.value)

    print("\n[6] 在线激活成功：接管状态，但不得删除/修改离线文件")
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d, verifier)
        offline_expires = seed_offline_valid(d, hours=24)
        used_keys_path = pathlib.Path(d) / USED_KEYS_FILE
        used_keys_path.write_text(json.dumps(
            {"version": 1, "used_keys": ["K7QM3XPD9RTB"],
             "last_used_at": "2026-09-24T12:40:59+00:00", "last_duration_hours": 24},
            ensure_ascii=False, indent=2), encoding="utf-8")
        offline_path = pathlib.Path(d) / OFFLINE_FILE
        before = snapshot(offline_path, used_keys_path)

        artifact = online_artifact(machine, 30)
        m.client.activate = lambda key, machine_code: {"signed_license": artifact}
        result = m.activate("ANY-ONLINE-KEY")

        check("6 在线激活成功", result.is_activated, result.status.value)
        check("6 license.json 已写入", (pathlib.Path(d) / "license.json").is_file())
        after = snapshot(offline_path, used_keys_path)
        check("6 offline_license.json 仍然存在", offline_path.is_file())
        check("6 offline_license.json 逐字节未变", before[str(offline_path)] == after[str(offline_path)])
        check("6 offline_keys.json 逐字节未变", before[str(used_keys_path)] == after[str(used_keys_path)])

        reloaded = make_manager(d, verifier).load_stored()
        check("6 重启后采用 Online（原 bug：会回退到离线）",
              reloaded.status is LicenseStatus.ACTIVATED and reloaded.message == "已激活",
              reloaded.message)
        check("6 重启后到期时间不是离线的那份",
              reloaded.expires_at is not None
              and abs((reloaded.expires_at - offline_expires).total_seconds()) > 60,
              reloaded.expires_at)


def main():
    verifier = SusiVerifier(public_key_pem=PUBLIC_KEY_PEM)
    machine = verifier.get_machine_code()
    run(verifier, machine)

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for name in _FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
