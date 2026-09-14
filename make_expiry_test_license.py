#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开发用工具：为「许可证到期」手工测试签发一份短时效的 SignedLicense。

这不是产品代码，也不被客户端引用。它只做一件事：用真实的开发签名私钥
（license_server/test_rsa_key.pem）签发一份真实、绑定本机、在 N 分钟后到期的
凭据，并（可选）写入本地 license.json，让真实客户端在启动时走完整的本地
校验路径加载它。

为什么需要它：
    服务器管理接口 LicenseCreate.duration_days 是 int（gt=0, le=365），
    最短只能签发 1 天，无法在手工会话中观察「运行中到期」。
    本工具用仓库既有的真实签名器绕过这个下限，但签名、机器绑定、到期
    判定全部是真的。

用法：
    python make_expiry_test_license.py --minutes 3 --write    # 备份并写入
    python make_expiry_test_license.py --status               # 查看当前状态
    python make_expiry_test_license.py --restore              # 还原备份
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

PRIVATE_KEY_PATH = os.path.join(PROJECT_ROOT, "license_server", "test_rsa_key.pem")
HELPER_PATH = os.path.join(
    PROJECT_ROOT, "susi_helper", "target", "release", "susi_helper.exe"
)

BACKUP_SUFFIX = ".bak-expiry-test"


def _rfc3339(value: datetime) -> str:
    """RFC3339 + 显式 UTC 偏移；susi_helper 的 chrono 反序列化要求它。"""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _call_helper(command: dict):
    """调用 susi_helper，返回 Success.data；失败抛 RuntimeError。"""
    if not os.path.isfile(HELPER_PATH):
        raise RuntimeError(f"susi_helper 不存在: {HELPER_PATH}")
    result = subprocess.run(
        [HELPER_PATH],
        input=json.dumps(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    response = json.loads(result.stdout)
    if "Success" in response:
        return response["Success"]["data"]
    message = response.get("Error", {}).get("message", "未知错误")
    raise RuntimeError(f"susi_helper 调用失败: {message}")


def get_machine_code() -> str:
    return _call_helper({"command": "GetMachineCode"}).strip()


def sign_short_license(minutes: int) -> str:
    """签发一份 N 分钟后到期、绑定本机的 SignedLicense（JSON 字符串）。"""
    private_key_pem = open(PRIVATE_KEY_PATH, "r", encoding="utf-8").read().strip()
    machine_code = get_machine_code()
    now = datetime.now(timezone.utc)

    payload = {
        "id": f"expiry-test-{int(now.timestamp())}",
        "product": "DouyinLowLatencyViewer",
        "customer": "DouyinLowLatencyViewer",
        "license_key": "expiry-test-artifact",
        "created": _rfc3339(now),
        "expires": _rfc3339(now + timedelta(minutes=minutes)),
        "features": ["ocr"],
        "machine_codes": [machine_code],
        "lease_expires": None,
        "lease_grace_period": None,
        "require_signed_binary": False,
    }

    data = _call_helper(
        {
            "command": "SignLicense",
            "private_key_pem": private_key_pem,
            "payload": payload,
        }
    )
    signed = json.loads(data)
    return json.dumps(signed)


def _store_paths():
    from utils.license_store import LicenseStore

    store = LicenseStore()
    return store.path, store.path + BACKUP_SUFFIX


def _read_store_record(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _describe(signed_license_json: str) -> str:
    """用真实客户端路径解析并描述一份凭据。"""
    from utils.license_manager import LicenseManager

    manager = LicenseManager()
    result = manager.verify_stored(signed_license_json) if hasattr(
        manager, "verify_stored"
    ) else manager._verify_artifact(signed_license_json)
    expires = result.expires_at.isoformat() if result.expires_at else "永久"
    return (
        f"status={result.status.value} ({result.display_text}) "
        f"message={result.message} expires={expires}"
    )


def cmd_write(minutes: int) -> int:
    path, backup = _store_paths()

    # 备份：只备份一次，避免把短时效凭据当成原件覆盖掉真正的备份
    if os.path.isfile(path) and not os.path.isfile(backup):
        shutil.copy2(path, backup)
        print(f"[backup] 原 license.json 已备份到 {backup}")
    elif os.path.isfile(backup):
        print(f"[backup] 备份已存在，保留不动: {backup}")
    else:
        print("[backup] 当前没有 license.json，无需备份")

    signed = sign_short_license(minutes)

    # 先用真实客户端校验器确认它确实有效，再写入
    print(f"[verify] {_describe(signed)}")

    record = {
        "version": 1,
        "signed_license": signed,
        "device_id": get_machine_code(),
        "server_url": "http://127.0.0.1:8000/api/v1",
        "activated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    print(f"[write]  已写入 {path}")
    print(f"[write]  到期时间(UTC) = {_rfc3339(expires_at)}")
    print(f"[write]  距现在 {minutes} 分钟")
    return 0


def cmd_status() -> int:
    path, backup = _store_paths()
    print(f"license.json : {path}")
    print(f"backup       : {backup}  ({'存在' if os.path.isfile(backup) else '不存在'})")
    record = _read_store_record(path)
    if not record:
        print("当前 license.json : 不存在或不可读")
        return 0
    print(f"activated_at : {record.get('activated_at')}")
    try:
        print(f"verification : {_describe(record['signed_license'])}")
    except Exception as e:  # noqa: BLE001 - 诊断工具，任何异常都应展示
        print(f"verification : 失败 -> {e}")
    return 0


def cmd_restore() -> int:
    path, backup = _store_paths()
    if not os.path.isfile(backup):
        print(f"[restore] 没有备份文件: {backup}")
        return 1
    shutil.copy2(backup, path)
    print(f"[restore] 已从备份还原 {path}")
    print(f"[restore] {_describe(_read_store_record(path)['signed_license'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="签发并写入 license.json")
    group.add_argument("--status", action="store_true", help="查看当前 license.json")
    group.add_argument("--restore", action="store_true", help="从备份还原 license.json")
    parser.add_argument("--minutes", type=int, default=3, help="有效分钟数（默认 3）")
    args = parser.parse_args()

    if args.write:
        return cmd_write(args.minutes)
    if args.status:
        return cmd_status()
    return cmd_restore()


if __name__ == "__main__":
    sys.exit(main())
