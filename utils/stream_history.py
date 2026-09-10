# -*- coding: utf-8 -*-
"""
最近连接的流历史记录管理器

功能：
- 保存最近10个成功连接的Douyin URL
- 去重：相同URL只保留一个，更新last_used
- LRU：超过10个时删除最旧的
- 持久化：本地JSON文件存储
- 线程安全：读写锁保护

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
import threading
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

class StreamHistory:
    """最近连接的流历史记录管理器"""

    def __init__(self, max_items: int = 10, file_path: str | Path | None = None):
        self.max_items = max_items
        self._data: Dict = {"streams": []}
        self._lock = threading.RLock()

        # Set file path (use provided path or default)
        if file_path is None:
            self._file_path = Path(__file__).parent.parent / "stream_history.json"
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