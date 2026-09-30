# -*- coding: utf-8 -*-
"""
直播间历史记录（Model / Repository / Service）

与旧 utils/stream_history.py 的区别：
    - 数据结构是可扩展的 LiveRoomHistoryItem（不再是裸 URL 字符串）
    - **只在成功进入直播间后记录**（旧版在连接尝试前就写入）
    - 展示信息面向用户（房间备注 / 房间号），URL 只作为存储键

分层：
    LiveRoomHistoryItem  数据模型（含展示辅助，可序列化）
    HistoryRepository    持久化（JSON、线程安全、旧格式一次性迁移）
    HistoryService       用例层（record_success / set_note / items / clear）

存储沿用旧版目录（LOCALAPPDATA/LiveLens/stream_history.json），首次加载时
把旧 {"streams":[{url,nickname,last_used}]} 无损迁移为新结构，用户无感。
"""

import enum
import json
import logging
import threading
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("DouyinLowLatencyViewer.stream.history")

__all__ = [
    "LiveRoomHistoryItem", "HistoryRepository", "HistoryService",
    "RemarkStatus", "extract_room_id",
]

#: 历史文件沿用了旧 StreamHistory 的路径约定（Phase 0 起在用户数据目录）
from utils.stream_history import default_data_dir, HISTORY_FILENAME  # noqa: E402

#: 新结构的格式版本（未来加字段时用于兼容判断）
_FORMAT_VERSION = 2


def extract_room_id(url: str) -> str:
    """从直播间 URL 提取房间标识（第一段路径），失败返回空串。

    平台无关：直播间 URL 的房间号就在第一段路径上
    （如 https://live.douyin.com/123456789 -> 123456789）。
    仅用于展示兜底——解析成功时应优先存 StreamInfo.room_id。
    """
    try:
        return urlparse(url).path.strip("/").split("/")[0]
    except Exception:  # noqa: BLE001 - 展示兜底，绝不让坏 URL 抛异常
        return ""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RemarkStatus(enum.Enum):
    """备注处理状态（与"是否首次访问"正交：跳过≠从未处理）

    UNSET   从未处理备注 —— 每次成功进入都应询问，直到用户做出选择
    SKIPPED 用户主动跳过 —— 以后进入不再自动弹窗（仍可手动改备注）
    SET     已填写备注   —— 以后进入不再自动弹窗
    """
    UNSET = "unset"
    SKIPPED = "skipped"
    SET = "set"


def _coerce_remark_status(value) -> str:
    """容错转换：非法/缺失值一律回落 UNSET（宁可多问一次，不静默丢提示）"""
    try:
        return RemarkStatus(value).value
    except ValueError:
        return RemarkStatus.UNSET.value


@dataclass
class LiveRoomHistoryItem:
    """一条直播间历史记录（可扩展：未来加收藏/置顶等字段不动存储结构）"""

    url: str                        # 存储键：成功进入过的直播间地址
    id: str = ""                    # 稳定标识（生成一次不再变）
    room_name: str = ""             # 用户备注；空 = 未命名
    room_id: str = ""               # 平台房间号（解析成功时记录）
    created_at: str = ""            # 首次成功进入时间（ISO）
    last_used_at: str = ""          # 最近成功进入时间（ISO）
    success_count: int = 0          # 成功进入次数
    seq: int = 0                    # 单调递增序号：同秒内保证 LRU 排序稳定
    #: 备注处理状态（是否弹备注询问只看这里，与访问次数无关）
    remark_status: str = RemarkStatus.UNSET.value

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]

    def display_name(self) -> str:
        """列表主标题：有备注显示备注，否则"未命名直播间" """
        return self.room_name.strip() or "未命名直播间"

    def display_room_id(self) -> str:
        """房间号：优先解析记录，兜底从 URL 提取"""
        return self.room_id or extract_room_id(self.url)

    def display_last_used(self) -> str:
        """最近使用日期（YYYY-MM-DD），无记录返回空"""
        if not self.last_used_at:
            return ""
        try:
            return datetime.fromisoformat(self.last_used_at).strftime("%Y-%m-%d")
        except ValueError:
            return self.last_used_at[:10] if self.last_used_at else ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LiveRoomHistoryItem":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416 - 可读性优先
        data = {k: v for k, v in d.items() if k in known}
        if "remark_status" in data:
            data["remark_status"] = _coerce_remark_status(data["remark_status"])
        return cls(**data)


class HistoryRepository:
    """JSON 持久化 + 旧格式迁移（线程安全）"""

    def __init__(self, file_path: str | Path | None = None):
        self._lock = threading.RLock()
        self._file_path = Path(file_path) if file_path \
            else Path(default_data_dir()) / HISTORY_FILENAME
        self._items: list[LiveRoomHistoryItem] = []
        self._load()

    # ---- 对外 ----
    def all(self) -> list[LiveRoomHistoryItem]:
        with self._lock:
            return list(self._items)

    def replace_all(self, items: list[LiveRoomHistoryItem]) -> None:
        with self._lock:
            self._items = list(items)
            self._save()

    def find(self, url: str) -> LiveRoomHistoryItem | None:
        with self._lock:
            return self._find(url)

    # ---- 内部 ----
    def _find(self, url: str) -> LiveRoomHistoryItem | None:
        for item in self._items:
            if item.url == url:
                return item
        return None

    def _load(self) -> None:
        try:
            if not self._file_path.exists():
                return
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") == _FORMAT_VERSION:
                self._items = [LiveRoomHistoryItem.from_dict(d)
                               for d in data.get("items", [])]
            else:
                self._items = self._migrate_legacy(data)
                self._save()   # 迁移即落盘，下次直接走新格式
                logger.info("[HISTORY] 旧格式已迁移: %d 条", len(self._items))
        except Exception as e:  # noqa: BLE001 - 坏文件不能挡启动
            logger.warning("[HISTORY] 历史记录加载失败（忽略）: %s", e)
            self._items = []

    @staticmethod
    def _migrate_legacy(data: dict) -> list[LiveRoomHistoryItem]:
        """旧 StreamHistory 格式 {"streams":[{url,nickname,last_used}]} → 新结构"""
        items = []
        for s in data.get("streams", []):
            url = s.get("url", "")
            if not url:
                continue
            last_used = s.get("last_used", "") or _now_iso()
            nickname = s.get("nickname", "") or ""
            items.append(LiveRoomHistoryItem(
                url=url,
                room_name=nickname,
                # 旧数据有备注 = 用户已表达过意愿（SET）；无备注 = 从未问过
                # （UNSET，迁移后的首次进入会补一次询问）
                remark_status=(RemarkStatus.SET.value if nickname
                               else RemarkStatus.UNSET.value),
                room_id=extract_room_id(url),
                created_at=last_used,
                last_used_at=last_used,
                success_count=1,
            ))
        return items

    def _save(self) -> None:
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": _FORMAT_VERSION,
                    "items": [it.to_dict() for it in self._items],
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001 - 保存失败不致命
            logger.warning("[HISTORY] 历史记录保存失败: %s", e)


class HistoryService:
    """历史记录用例层（GUI 唯一接触点）

    核心规则：**只有成功进入直播间才产生/更新记录**（record_success），
    连接尝试、失败一律不写。
    """

    def __init__(self, repository: HistoryRepository | None = None,
                 max_items: int = 10):
        self._repo = repository or HistoryRepository()
        self.max_items = max_items
        self._seq = max((it.seq for it in self._repo.all()), default=0)
        #: 首次进入直播间时的备注询问回调 cb(url, room_id)。
        #: 纯回调而非 Qt 信号：本模块不依赖 GUI；GUI 侧自行弹窗
        #: （见 gui/history_panel.RemarkDialog）。
        self._remark_cb = None

    def set_remark_callback(self, cb) -> None:
        """注册备注询问回调 cb(url, room_id)。

        只在"首次成功进入某直播间"时触发一次；回调异常被吞掉（不得影响
        记录本身——跳过询问时 remark 为空但历史仍在）。
        """
        self._remark_cb = cb

    def _notify_remark_request(self, url: str, room_id: str) -> None:
        cb = self._remark_cb
        if cb is None:
            return
        try:
            cb(url, room_id)
        except Exception:  # noqa: BLE001 - 询问失败不影响历史记录
            logger.warning("[HISTORY] 备注询问回调异常（已忽略）", exc_info=True)

    def record_success(self, url: str, room_id: str = "") -> None:
        """成功进入直播间：新增或更新记录（幂等，线程安全）。

        备注询问的触发只看 remark_status == UNSET（与访问次数无关）：
            - 首次进入的新条目为 UNSET → 询问；
            - 跳过过（SKIPPED）/ 已备注（SET）→ 永不再自动询问；
            - 旧数据迁移出的 UNSET 条目 → 在迁移后首次进入时补一次询问。
        回调是模态的（GUI 弹窗），返回时用户选择（SET/SKIPPED）已写回。
        """
        if not url:
            return
        items = self._repo.all()
        item = next((it for it in items if it.url == url), None)
        now = _now_iso()
        self._seq += 1
        if item is None:
            item = LiveRoomHistoryItem(url=url, room_id=room_id,
                                       created_at=now, last_used_at=now,
                                       success_count=1, seq=self._seq)
            items.append(item)
        else:
            item.last_used_at = now
            item.success_count += 1
            item.seq = self._seq
            if room_id and not item.room_id:
                item.room_id = room_id
        items.sort(key=lambda it: (it.last_used_at, it.seq), reverse=True)
        self._repo.replace_all(items[:self.max_items])
        # 记录已落盘后再询问：跳过时 remark 为空但历史仍在，状态转 SKIPPED
        if item.remark_status == RemarkStatus.UNSET.value:
            self._notify_remark_request(url, room_id)

    def mark_remark_skipped(self, url: str) -> bool:
        """用户跳过备注询问：UNSET/SKIPPED 状态固化，以后不再自动弹窗"""
        items = self._repo.all()
        item = next((it for it in items if it.url == url), None)
        if item is None:
            return False
        item.remark_status = RemarkStatus.SKIPPED.value
        self._repo.replace_all(items)
        return True

    def set_note(self, url: str, note: str) -> bool:
        """设置/清除用户备注。

        非空备注 → SET（不再自动询问）；清空备注 → SKIPPED（用户明确
        表示不要备注，也不自动询问；仍可再次手动设置）。
        """
        items = self._repo.all()
        item = next((it for it in items if it.url == url), None)
        if item is None:
            return False
        note = note.strip()
        item.room_name = note
        item.remark_status = (RemarkStatus.SET.value if note
                              else RemarkStatus.SKIPPED.value)
        self._repo.replace_all(items)
        return True

    def get_note(self, url: str) -> str:
        item = self._repo.find(url)
        return item.room_name if item else ""

    def items(self) -> list[LiveRoomHistoryItem]:
        """全部记录，按最近使用降序"""
        return self._repo.all()

    def clear(self) -> None:
        self._repo.replace_all([])
