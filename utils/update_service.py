# -*- coding: utf-8 -*-
"""
Velopack 更新服务 —— 极薄封装 (Phase 3)。

定位：
    LiveLens GUI → UpdateService → velopack.UpdateManager

本模块**只**做三件事：
    1. 把 LiveLens 需要的四个动作（取当前版本 / 检查 / 下载 / 应用并重启）
       转成 Velopack 的调用；
    2. 把 Velopack 的构造失败（未安装运行时）与调用异常收口成「安全返回值」
       或一个明确的 UpdateError，绝不让更新链的异常冒到启动流程 / GUI 上；
    3. 让 GUI 只依赖一个稳定对象，而不是直接依赖 velopack 这个第三方包。

刻意**不**做的事（这些都是 Velopack 的职责，重复实现就是重新造轮子）：
    版本比较、release metadata 解析、HTTP 下载、校验和、ZIP 解压、更新器、
    EXE 文件锁处理、安装目录替换。

线程模型：
    本模块是**同步**的，不自建线程。Velopack 的 check / download 是阻塞调用
    （Rust 侧释放 GIL，但不返回事件循环），调用方负责放到后台线程，见
    gui/update_dialog.py 与 gui/main_window.py。Velopack 自己已经不阻塞 GUI
    线程地跑下载 I/O 并回调，所以我们也不在外面再包一层无意义的线程。

版本口径：
    运行时显示的版本**优先取 Velopack 的当前安装版本**（get_current_version），
    因为它才知道「更新之后我是什么版本」。只有未安装运行（源码 / 便携 ZIP）时
    才回落到 utils.version.__version__。
"""

import gzip
import json
import logging
import urllib.error
import urllib.request

from utils import update_state
from utils.update_source import CHANNEL, get_update_source

logger = logging.getLogger("DouyinLowLatencyViewer.utils.update_service")

#: 取更新源清单的超时（秒）。回滚要在启动时后台跑，不能让一个卡住的连接拖着。
_FEED_TIMEOUT = 30.0


def _fetch_bytes(url: str) -> bytes:
    """取更新源上的一个文件。独立成函数是为了单测能替换掉它。"""
    request = urllib.request.Request(url, headers={"User-Agent": "LiveLens-Update"})
    with urllib.request.urlopen(request, timeout=_FEED_TIMEOUT) as response:
        return response.read()


class UpdateError(Exception):
    """一次更新动作（下载 / 应用）失败。消息面向用户，不含敏感信息。"""


class UpdateService:
    """Velopack UpdateManager 的薄封装。

    全部方法都是「不抛异常」或「抛 UpdateError」的：更新是后台功能，
    它的任何失败都不该让视频 / OCR / 授权受影响。
    """

    def __init__(self, source: str | None = None):
        self._source = source or get_update_source()
        self._manager = None          # 惰性构造并缓存（正常检查用）
        self._manager_error: str | None = None
        self._downgrade_mgr = None    # 惰性构造并缓存（回滚用）

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @property
    def source(self) -> str:
        return self._source

    def _downgrade_manager(self):
        """用于定向回滚的 UpdateManager。

        与正常检查用的那个只有两点不同，且都是回滚必需：
          - AllowVersionDowngrade=True：明确允许「装一个更低的版本」；
          - MaximumDeltasBeforeFallback=-1：强制走全量包（回滚是救火，不能
            依赖增量包在坏版本上的可用性）。
        """
        if self._downgrade_mgr is not None:
            return self._downgrade_mgr
        try:
            import velopack
        except Exception as e:  # noqa: BLE001
            logger.warning("velopack 导入失败，无法回滚: %s", e)
            return None
        try:
            options = velopack.UpdateOptions(
                AllowVersionDowngrade=True, MaximumDeltasBeforeFallback=-1
            )
            self._downgrade_mgr = velopack.UpdateManager(self._source, options)
        except Exception as e:  # noqa: BLE001
            logger.warning("构造回滚用 UpdateManager 失败: %s", e)
            return None
        return self._downgrade_mgr

    def _get_manager(self):
        """返回 UpdateManager；不可用时返回 None。

        未安装运行时（源码启动、旧 ZIP 直接解压运行）Velopack 会抛
        RuntimeError("This application is not properly installed: ...")。
        那不是错误，而是「本机压根没有 Velopack 安装上下文」——
        静默降级为「无更新能力」，而不是弹错。
        """
        if self._manager is not None:
            return self._manager
        if self._manager_error is not None:
            return None
        try:
            import velopack
        except Exception as e:  # noqa: BLE001 - 缺包也不能影响启动
            self._manager_error = f"velopack 不可用: {e}"
            logger.warning("velopack 导入失败，更新功能关闭: %s", e)
            return None
        try:
            self._manager = velopack.UpdateManager(self._source)
        except Exception as e:  # noqa: BLE001 - 未安装运行是正常路径
            self._manager_error = str(e)
            logger.info("Velopack 安装上下文不可用，更新功能关闭: %s", e)
            return None
        return self._manager

    def is_available(self) -> bool:
        """本机是否具备 Velopack 更新能力（已通过 Setup.exe 安装）。"""
        return self._get_manager() is not None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def current_version(self) -> str:
        """当前版本：优先 Velopack 的安装版本，未安装时回落 utils.version。"""
        mgr = self._get_manager()
        if mgr is not None:
            try:
                return mgr.get_current_version()
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 Velopack 当前版本失败: %s", e)
        from utils.version import __version__

        return __version__

    def check(self):
        """检查更新。返回 velopack.UpdateInfo 或 None（无更新 / 不可用 / 出错）。

        任何失败都返回 None：后台版本检查失败不该在用户面前产生动静。

        额外负责 blocked_version（Phase 4B，启动失败恢复）：
          - 更新源上的最新版正好是被封锁的那个版本 → 视为无更新，避免
            「回滚 → 又被更新回去 → 再崩 → 再回滚」的死循环；
          - 最新版不是它 → 说明源上已有更高版本，解除封锁。这里**不需要**
            自己写版本比较器：正常情况下 AllowVersionDowngrade=False，
            check_for_updates 只可能返回比当前更高的版本；而 blocked 曾经就是
            当时的最高版本，所以「不等于 blocked」就等于「比 blocked 更高」
            （vpk 也拒绝用更低版本覆盖同一 channel，单调性有保证）。
        """
        mgr = self._get_manager()
        if mgr is None:
            return None
        try:
            info = mgr.check_for_updates()
        except Exception as e:  # noqa: BLE001
            logger.warning("检查更新失败: %s", e)
            return None
        if info is None:
            return None

        blocked = update_state.read_state().blocked_version
        if not blocked:
            return info
        target = info.TargetFullRelease.Version
        if target == blocked:
            logger.info("版本 %s 上次启动失败已被封锁，跳过本次更新", blocked)
            return None
        logger.info("更新源上出现更高版本 %s（已封锁 %s），解除封锁", target, blocked)
        update_state.clear_blocked_version()
        return info

    def pending_restart(self):
        """已下载待重启应用的更新资产；没有则 None。"""
        mgr = self._get_manager()
        if mgr is None:
            return None
        try:
            return mgr.get_update_pending_restart()
        except Exception as e:  # noqa: BLE001
            logger.warning("读取待应用更新失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def download(self, info, progress=None) -> None:
        """下载更新包。由调用方放到后台线程执行。

        :param info: check() 返回的 UpdateInfo
        :param progress: 可选回调 callback(percent: int)，percent 为 0..100。
                         这是 Velopack 真实上报的下载百分比，不是估算值。
                         它会在**调用 download 的那个线程**上被回调。
        :raises UpdateError: 下载失败（网络中断、磁盘、校验失败等）
        """
        mgr = self._get_manager()
        if mgr is None:
            raise UpdateError("当前不是 Velopack 安装版本，无法自动更新。")
        try:
            mgr.download_updates(info, progress)
        except Exception as e:  # noqa: BLE001
            raise UpdateError(f"更新下载失败: {e}") from e

    def fetch_full_asset(self, version: str):
        """从更新源清单里取指定版本的**全量**资产。取不到返回 None。

        为什么客户端要自己取一次清单：Velopack 的 Python binding 没有暴露
        release feed（`UpdateManager` 只有检查/下载/应用那十个方法），而定向
        回滚需要一个**精确版本**的资产元数据。取清单只是「一条 HTTP GET +
        挑一条记录」——下载、校验、解压、替换、重启仍然全部由 Velopack 完成，
        这里没有实现任何 package manager 的职责。

        必须是 Full：增量包只能从「它声明的基础版本」打出来，而那个基础版本
        正是我们要离开的坏版本，不能依赖。
        """
        url = f"{self._source}/releases.{CHANNEL}.json"
        try:
            raw = _fetch_bytes(url)
        except (urllib.error.URLError, OSError) as e:
            raise UpdateError(f"无法访问更新源清单（{url}）：{e}") from e

        try:
            text = gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8")
            feed = json.loads(text)
        except (ValueError, OSError) as e:
            raise UpdateError(f"更新源清单无法解析：{e}") from e

        assets = feed.get("Assets") if isinstance(feed, dict) else None
        if not isinstance(assets, list):
            raise UpdateError("更新源清单结构不符合预期（缺少 Assets）。")

        for entry in assets:
            if not isinstance(entry, dict):
                continue
            if entry.get("Version") != version or entry.get("Type") != "Full":
                continue
            try:
                import velopack

                return velopack.VelopackAsset(
                    PackageId=entry["PackageId"],
                    Version=entry["Version"],
                    Type=entry["Type"],
                    FileName=entry["FileName"],
                    SHA1=entry["SHA1"],
                    SHA256=entry["SHA256"],
                    Size=int(entry["Size"]),
                    NotesMarkdown=entry.get("NotesMarkdown", ""),
                    NotesHtml=entry.get("NotesHtml", ""),
                )
            except (KeyError, TypeError, ValueError) as e:
                raise UpdateError(f"更新源清单里的资产字段不完整：{e}") from e
        return None

    def downgrade_to(self, version: str, progress=None) -> None:
        """定向回滚到指定版本（启动失败恢复用，Phase 4B）。

        全程只有「拿元数据」这一步是我们做的，其余都交给 Velopack：
        download_updates 会按 FileName 从源下载并校验 SHA/大小，
        apply_updates_and_restart 会拉起 Update.exe、等本进程退出、替换安装
        目录并重新启动 —— 与本进程无关。

        :raises UpdateError: 源上没有该版本的 Full 包、下载失败、或 Velopack
            未接管（进程没退出）
        """
        mgr = self._downgrade_manager()
        if mgr is None:
            raise UpdateError("当前不是 Velopack 安装版本，无法执行自动恢复。")

        asset = self.fetch_full_asset(version)
        if asset is None:
            raise UpdateError(
                f"更新源上没有版本 {version} 的完整安装包，无法自动恢复。"
            )

        try:
            import velopack

            info = velopack.UpdateInfo(
                TargetFullRelease=asset, DeltasToTarget=[], IsDowngrade=True
            )
        except Exception as e:  # noqa: BLE001
            raise UpdateError(f"构造回滚更新信息失败：{e}") from e

        logger.info("开始回滚到 %s（全量包 %s）", version, asset.FileName)
        try:
            mgr.download_updates(info, progress)
        except Exception as e:  # noqa: BLE001
            raise UpdateError(f"回滚包下载失败：{e}") from e
        # 这一句之后进程应当被 Velopack 结束，旧版本会被拉起
        mgr.apply_updates_and_restart(info)
        logger.warning("apply_updates_and_restart 意外返回，回滚未生效")
        raise UpdateError("回滚未能启动，请手动重新安装。")

    def apply_and_restart(self, info) -> None:
        """应用已下载的更新并重启到新版本。

        Velopack 的实现是「启动 Update.exe 等本进程退出 → 应用 → 重新拉起」，
        并在这一步直接结束当前进程（Rust 侧 exit(0)），因此本函数正常情况下
        **不会返回**。真要返回了，说明 Velopack 没有接管，属于异常路径。

        调用前调用方必须自行收尾（停流、释放 ffmpeg）—— 进程会被直接结束，
        不会再走 Qt 的窗口关闭流程。
        """
        mgr = self._get_manager()
        if mgr is None:
            raise UpdateError("当前不是 Velopack 安装版本，无法应用更新。")
        logger.info("应用更新并重启（进程将退出，由 Velopack 接管）")
        mgr.apply_updates_and_restart(info)
        # 走到这里说明 Velopack 没有结束进程
        logger.warning("apply_updates_and_restart 意外返回，进程仍在运行")
        raise UpdateError("更新未能启动，请手动重启程序后重试。")
