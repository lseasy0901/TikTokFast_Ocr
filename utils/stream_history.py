# -*- coding: utf-8 -*-
"""
最近连接的流历史记录管理器

功能：
- 保存最近10个成功连接的Douyin URL
- 去重：相同URL只保留一个，更新last_used
- LRU：超过10个时删除最旧的
- 持久化：本地JSON文件存储
- 线程安全：读写锁保护

存储位置（Phase 0）：
    Windows: %LOCALAPPDATA%/LiveLens/stream_history.json
    其它:    ~/.local/share/LiveLens/stream_history.json

    此前这里写的是 ``<项目根>/stream_history.json``（由 ``__file__`` 推导）。
    源码环境下那确实是项目根；但**冻结构建下 ``__file__`` 指向 _MEIPASS**，
    于是实际写到了安装目录内的 ``_internal/stream_history.json``。这有两个后果：
        1. 更新系统整体替换安装目录时会连带删掉用户的历史记录；
        2. 安装目录若不可写（例如解压到 Program Files），保存会静默失败。
    因此改为写入用户数据目录 —— 与 utils/license_store.py 的做法一致，
    该目录在安装目录之外，更新时不受影响。

    注意本模块**不**复用 license_store.default_data_dir()：两者的基础目录
    （LOCALAPPDATA vs APPDATA）与目录名（LiveLens vs DouyinLowLatencyViewer）
    都不同，是有意的独立配置，不是重复代码。

数据结构：
{
    "streams": [
        {
            "url": "https://live.douyin.com/123456789",
            "nickname": "",
            "last_used": "2026-09-09T10:30:00"
        }
    ]
}
"""

import json
import os
import shutil
import threading
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

#: 用户数据目录名。产品名是 LiveLens（窗口标题），与 EXE 名
#: DouyinLowLatencyViewer 刻意不同 —— 见 douyin_viewer.spec 的 EXE name 说明。
APP_DIR_NAME = "LiveLens"
HISTORY_FILENAME = "stream_history.json"


def default_data_dir() -> str:
    """返回历史记录的存放目录（不存在时不创建；由保存时创建）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
    return os.path.join(base, APP_DIR_NAME)


def _legacy_file_path() -> Path:
    """Phase 0 之前的历史记录位置（安装目录内 / 源码树根）。"""
    return Path(__file__).parent.parent / HISTORY_FILENAME


def _migrate_legacy_file(default_path: Path) -> None:
    """把旧位置的历史记录一次性搬到用户数据目录。

    只在新位置**尚无文件**时执行，且全程 best-effort：任何失败都忽略。
    迁移失败最坏只是丢掉一份最近连接列表，不影响任何功能；而为此中断启动
    是不可接受的。

    为什么需要它：本项目目前是手工解压 ZIP 分发，旧安装目录里的
    ``_internal/stream_history.json`` 只有在"覆盖式更新且新版首次启动"时
    才可能被搬到新位置。更新系统整体替换目录的那种流程下旧文件会先被删除，
    因此 Phase 3 的 Updater 也必须单独保留它 —— 本函数不是那条路径的替代品。
    """
    try:
        if default_path.exists():
            return
        legacy = _legacy_file_path()
        if not legacy.exists() or legacy.resolve() == default_path.resolve():
            return
        default_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy, default_path)
    except OSError:
        pass


class StreamHistory:
    """最近连接的流历史记录管理器"""

    def __init__(self, max_items: int = 10, file_path: str | Path | None = None):
        self.max_items = max_items
        self._data: Dict = {"streams": []}
        self._lock = threading.RLock()

        # Set file path (use provided path or default)
        if file_path is None:
            self._file_path = Path(default_data_dir()) / HISTORY_FILENAME
            _migrate_legacy_file(self._file_path)
        else:
            self._file_path = Path(file_path)

        # 确保文件存在
        self._load()

    def _load(self):
        """从文件加载历史记录"""
        with self._lock:
            try:
                if self._file_path.exists():
                    with open(self._file_path, 'r', encoding='utf-8') as f:
                        self._data = json.load(f)
                    self._trim_to_max()
                else:
                    self._data = {"streams": []}
            except Exception as e:
                print(f"[StreamHistory] 加载历史记录失败: {e}")
                self._data = {"streams": []}

    def _save(self):
        """保存历史记录到文件"""
        with self._lock:
            try:
                self._trim_to_max()
                # 用户数据目录可能尚不存在（首次运行）。不创建的话 open() 会抛
                # FileNotFoundError，被下面的 except 吞掉，表现为"保存静默失效"。
                self._file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._file_path, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[StreamHistory] 保存历史记录失败: {e}")

    def _trim_to_max(self):
        """裁剪到最大数量，保留最近使用的"""
        if len(self._data["streams"]) > self.max_items:
            # 按last_used排序，保留最新的max_items个
            self._data["streams"].sort(key=lambda x: x.get("last_used", ""), reverse=True)
            self._data["streams"] = self._data["streams"][:self.max_items]

    def add_or_update(self, url: str, nickname: str = ""):
        """
        添加或更新URL记录

        :param url: 直播间URL
        :param nickname: 用户设置的昵称
        """
        with self._lock:
            now = datetime.now().isoformat()

            # 检查是否已存在
            for stream in self._data["streams"]:
                if stream["url"] == url:
                    # 更新现有记录
                    stream["nickname"] = nickname
                    stream["last_used"] = now
                    self._save()
                    return

            # 添加新记录
            self._data["streams"].append({
                "url": url,
                "nickname": nickname,
                "last_used": now
            })

            self._trim_to_max()
            self._save()

    def get_all(self) -> List[Dict]:
        """获取所有记录，按最后使用时间降序排序"""
        with self._lock:
            streams = self._data["streams"].copy()
            # 按last_used排序，最新的在前
            streams.sort(key=lambda x: x.get("last_used", ""), reverse=True)
            return streams

    def remove(self, url: str) -> bool:
        """删除指定URL的记录"""
        with self._lock:
            original_len = len(self._data["streams"])
            self._data["streams"] = [s for s in self._data["streams"] if s["url"] != url]

            if len(self._data["streams"]) < original_len:
                self._save()
                return True
            return False

    def clear(self):
        """清空所有记录"""
        with self._lock:
            self._data = {"streams": []}
            self._save()

    def get_nickname(self, url: str) -> str:
        """获取URL的昵称"""
        with self._lock:
            for stream in self._data["streams"]:
                if stream["url"] == url:
                    return stream.get("nickname", "")
            return ""

    def set_nickname(self, url: str, nickname: str) -> bool:
        """设置URL的昵称"""
        with self._lock:
            for stream in self._data["streams"]:
                if stream["url"] == url:
                    stream["nickname"] = nickname
                    self._save()
                    return True
            return False