# -*- coding: utf-8 -*-
"""播放链后台工作单元(自 gui/main_window.py 外迁,Phase 1 拆分)。

三类 worker 职责单一:连接解析(_ConnectWorker)、裸流解析
(_BareResolveWorker)、帧通知/桥接(_FrameNotifier/_FrameBridge)。
保持模块级私有名 re-export 兼容:tests 直接 `from gui.main_window
import _FrameBridge` 与 monkeypatch `mw._ConnectWorker`,main_window
顶部按原名导入本模块,既有测试零改动。
"""

import logging
import threading
from dataclasses import dataclass

from stream.bare_source import REASON_DISABLED, probe_video_bare
from stream.models import StreamResolveError
from stream.resolver import StreamResolver
from utils import ffmpeg7_runtime

logger = logging.getLogger("DouyinLowLatencyViewer.gui.playback_workers")

# ======================================================================
# 后台连接线程：执行流解析（平台无关），不阻塞 GUI
# ======================================================================
class _ConnectWorker:
    """后台线程：把直播间 URL 解析为统一的 StreamInfo"""

    def __init__(self, live_url: str, on_success, on_error, resolver=None):
        """
        :param live_url:   直播间 URL
        :param on_success: 成功回调，签名为 callback(info: StreamInfo)
        :param on_error:   失败回调，签名为 callback(error_msg: str)
        :param resolver:   StreamResolver；不传则新建一个默认的
        """
        self._live_url = live_url
        self._on_success = on_success
        self._on_error = on_error
        self._resolver = resolver if resolver is not None else StreamResolver()
        self._thread = threading.Thread(
            target=self._run,
            name="connect-worker",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def _run(self):
        try:
            info = self._resolver.resolve(self._live_url)
            logger.info("[GUI] stream resolved: platform=%s room=%s type=%s url=%s",
                        info.platform, info.room_id, info.protocol,
                        (info.url or "")[:80])
            self._on_success(info)
        except StreamResolveError as e:
            logger.error("[GUI] stream resolve failed: %s", e)
            self._on_error(str(e))
        except Exception as e:
            logger.exception("连接过程未知异常")
            self._on_error(f"连接异常: {e}")


# ======================================================================
# 后台裸流解析线程：FFmpeg7 运行时校验 + 裸流 URL 页面直取（不阻塞 GUI）
# ======================================================================
@dataclass(frozen=True)
class _BareResolveResult:
    """一次会话取流准备的结论（由后台线程产出，经 Signal 回到 GUI 线程）。

    RELEASE-02-B5-CLEANUP：这是**唯一**运行时/取流入口的结论。失败时不存在
    "降级到旧路径"的分支 —— 只有明确原因，由 GUI 给用户一句人话并结束会话。
    所有平台都必须先通过 FFmpeg 7 运行时校验（内置 FFmpeg 6 已随本任务移除，
    不再存在任何内置解码器）。

    :param url:         视频版裸流 URL；None = 非裸流平台，或本次取不到
    :param exe:         已通过校验的 FFmpeg7 运行时路径；None = 不可用
    :param runtime_ok:  运行时是否可用（False → 需要给用户可见提示）
    :param live:        页面是否显示该房间正在直播（用于区分"未开播"与"取不到"）
    :param reason:      稳定枚举（运行时或取流），供 GUI 映射人话
    :param detail:      给日志的技术细节（不进 UI）
    """

    url: str | None = None
    exe: str | None = None
    runtime_ok: bool = False
    live: bool = False
    reason: str | None = None
    detail: str = ""


class _BareResolveWorker:
    """后台线程：校验 FFmpeg7 运行时；（裸流平台）再取本次会话的裸流 URL。

    与 `_ConnectWorker` 同一模式（普通类 + daemon 线程 + 回调），不引入
    新的线程模型：线程只做阻塞 IO（运行时探测、页面 GET），结果通过
    MainWindow 的 Signal 回到 GUI 线程，绝不直接触碰任何 QWidget。

    **可取消**：`cancel()` 置位后，线程在发出结果前检查并放弃——
    因此"快速切换房间 / 重复点击 / 关闭窗口"场景下，旧请求的结果不会
    被应用（在途 HTTP 请求会在 `bare_source.FETCH_TIMEOUT_S` 内自行结束，
    其返回值被丢弃）。
    """

    def __init__(self, request_id: int, platform: str, room_id: str,
                 want_bare: bool, on_done):
        self._request_id = request_id
        self._platform = platform
        self._room_id = room_id
        self._want_bare = want_bare
        self._on_done = on_done
        self._cancelled = False
        self._thread = threading.Thread(
            target=self._run,
            name="bare-resolve-worker",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def cancel(self):
        """放弃本次结果（在途请求自行结束，结果被丢弃）。"""
        self._cancelled = True

    def _run(self):
        result = _BareResolveResult()
        try:
            # 1) 运行时校验（带缓存；首次会拉起子进程探测，故必须在后台线程）。
            #    所有平台共用同一个 FFmpeg 7 运行时 —— 内置 FFmpeg 6 已移除。
            st = ffmpeg7_runtime.status()
            if not st.ok:
                result = _BareResolveResult(
                    url=None, exe=None, runtime_ok=False,
                    reason=st.reason, detail=st.detail,
                )
            elif not self._want_bare:
                # 非裸流平台（虎牙 / B 站）：只需运行时就绪，候选由适配器给出。
                # 抖音但总开关被显式关闭 → 记录原因：抖音唯一取流路径就是裸流，
                # 关掉它意味着本会话无可播放来源（不许静默降级）。
                reason = None
                detail = "非裸流平台，使用适配器候选"
                if (self._platform or "").lower() == "douyin":
                    reason = REASON_DISABLED
                    detail = "抖音总开关被显式关闭，无可用取流来源"
                result = _BareResolveResult(
                    url=None, exe=st.path, runtime_ok=True,
                    reason=reason, detail=detail,
                )
            else:
                # 2) 页面直取裸流 URL（有界超时的阻塞请求）
                probe = probe_video_bare(self._platform, self._room_id)
                result = _BareResolveResult(
                    url=probe.url, exe=st.path, runtime_ok=True,
                    live=probe.live, reason=probe.reason, detail=probe.detail,
                )
        except Exception as e:  # noqa: BLE001 - 取流失败转结构化原因，不抛出
            logger.exception("[BARE] 取流线程异常")
            result = _BareResolveResult(
                url=None, exe=None, runtime_ok=False,
                reason=ffmpeg7_runtime.REASON_PROBE_FAILED,
                detail=f"取流线程异常: {e}",
            )

        if self._cancelled:
            logger.info("[BARE] 请求 %d 已取消，丢弃结果", self._request_id)
            return
        self._on_done(self._request_id, result)

