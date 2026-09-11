# -*- coding: utf-8 -*-
"""
许可证本地存储（客户端） - Phase 7.2-6.5

把服务器返回的 SignedLicense 持久化到【应用数据目录】，而不是源码目录。

存放内容（全部为非机密数据）：
    - signed_license: 服务器签名的凭据本体（JSON 字符串）
    - device_id:      激活时使用的设备标识（用于诊断）
    - server_url:     签发该凭据的服务器地址（用于诊断）
    - activated_at:   本地记录的成功激活时间（仅用于展示，不作为有效期依据）

绝不存放：
    - 私钥 / 任何签名密钥材料
    - 本地计算的到期时间（有效期只认 SignedLicense 里服务器签名的 expires 字段）

存储位置：
    Windows: %APPDATA%/DouyinLowLatencyViewer/license.json
    其它:    ~/.local/share/DouyinLowLatencyViewer/license.json
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("DouyinLowLatencyViewer.license.store")

APP_DIR_NAME = "DouyinLowLatencyViewer"
LICENSE_FILENAME = "license.json"
SCHEMA_VERSION = 1


def default_data_dir() -> str:
    """返回应用数据目录（不存在时不创建；由 LicenseStore 在写入时创建）。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
    return os.path.join(base, APP_DIR_NAME)


class LicenseStore:
    """SignedLicense 的本地持久化（原子写入，线程安全）。"""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or default_data_dir()
        self.path = os.path.join(self.data_dir, LICENSE_FILENAME)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def load(self) -> Optional[dict]:
        """读取已保存的许可证记录；不存在或损坏时返回 None。

        损坏的文件不会抛异常：返回 None 让上层走「未激活」路径，
        避免一个坏文件把客户端彻底卡死。
        """
        with self._lock:
            if not os.path.isfile(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("本地许可证文件不可读，按未激活处理: %s", e)
                return None

            if not isinstance(record, dict) or not record.get("signed_license"):
                logger.warning("本地许可证文件结构非法，按未激活处理")
                return None
            return record

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def save(self, signed_license: str, device_id: str, server_url: str) -> None:
        """原子写入许可证记录。

        先写临时文件再 os.replace，保证进程中断时不会留下半个文件。
        """
        record = {
            "version": SCHEMA_VERSION,
            "signed_license": signed_license,
            "device_id": device_id,
            "server_url": server_url,
            "activated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

        with self._lock:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp_path = self.path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
            except OSError:
                # 清理残留临时文件，但不掩盖原始异常
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

        # 只记录路径，不记录内容
        logger.info("许可证已保存到 %s", self.path)

    def clear(self) -> None:
        """删除本地许可证（用于诊断/退订）。"""
        with self._lock:
            if os.path.isfile(self.path):
                try:
                    os.remove(self.path)
                    logger.info("本地许可证已清除")
                except OSError as e:
                    logger.warning("清除本地许可证失败: %s", e)
