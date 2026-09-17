# -*- coding: utf-8 -*-
"""
心跳调度（客户端） - Phase 7.4

分工：
    调度（本模块）—— 下一次心跳什么时候发、这一拍要不要发
    上报（utils.license_manager.heartbeat）—— 怎么发、发给谁

为什么不是固定周期
------------------
DAU 在服务端按业务日（``BUSINESS_TZ_NAME``）去重，所以只要一条会话**跨过
业务日边界时还活着**，那一天就该被计入一次。固定周期的定时器对齐的是
「启动时刻的钟点」，不是边界：

    启动于 23:00，周期 24h  -> 次日要等到 23:00 才被计入（最坏缺失近一整天）
    启动于 23:00，周期 1h   -> 次日最坏缺失 1 小时，代价是 24 倍的写入量（实测
                               平均 15.7 次/天/设备）

本模块改为「对齐下一个业务日边界」，并且**每次触发后重新计算**下一次延时：

    - 边界触发   会话跨过午夜即立刻计入新的一天，缺失窗口为 0，写入量 1 次/天
    - 重新计算   延时由"当前时刻到下一个边界"现算得出，不累积 QTimer 漂移
    - watchdog   业务日边界可能在 24h 之后，但定时器单次挂起时间被封顶
                 （``WATCHDOG_MAX_MS``）。系统休眠期间定时器不触发，醒来后
                 最多这么久就会醒一次，若期间已跨过边界就补报那天 —— 这就是
                 "休眠唤醒后的补发"。

时区一致性
----------
本模块解析出的业务时区**必须与服务端一致**，否则客户端会在服务端边界之前
触发，把心跳记到错误的一天。两者默认值相同，且都可通过环境变量覆盖
（客户端 ``DLV_BUSINESS_TZ`` / 服务端 ``BUSINESS_TZ_NAME``）。
"""

import logging
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("DouyinLowLatencyViewer.heartbeat.schedule")

#: 业务时区名。与服务端 config.settings.BUSINESS_TZ_NAME 同源同名。
_ENV_TZ = "DLV_BUSINESS_TZ"
DEFAULT_BUSINESS_TZ = "Asia/Shanghai"

#: 定时器单次最长挂起时间。
#: 业务日边界最远在 24h 之后，但定时器不能被挂 24h —— 系统休眠会冻结定时器，
#: 唤醒后需要一个不依赖"边界还差多久"的兜底节拍来重新评估业务日是否已推进。
#: 代价只是每 15 分钟一次纯本地的日期比较（不产生网络请求）。
WATCHDOG_MAX_MS = 15 * 60 * 1000


def business_tz_name() -> str:
    """客户端使用的业务时区名（环境变量可覆盖，空值按未设置处理）。"""
    value = os.environ.get(_ENV_TZ)
    if value and value.strip():
        return value.strip()
    return DEFAULT_BUSINESS_TZ


def resolve_tz(name: Optional[str] = None):
    """解析时区对象；不可用时返回 None，由调用方决定降级行为。

    Windows 没有系统 IANA 时区库，需要 tzdata 包。缺失时这里返回 None，
    调用方应**跳过周期调度**（启动那一次上报仍然进行），而不是猜一个偏移 ——
    猜错会让心跳落到服务端的另一天，比不调度更糟。
    """
    target = name or business_tz_name()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(target)
    except Exception as exc:  # noqa: BLE001 - 缺时区库不是致命错误
        logger.warning(
            "无法加载业务时区 %s（原因：%s）。心跳将只上报启动那一次，"
            "不再做周期补发；请在运行环境安装 tzdata。",
            target,
            exc,
        )
        return None


def _as_utc(now_utc: datetime) -> datetime:
    """naive 一律按 UTC 解释 —— 与全仓库约定一致。"""
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc


def business_day(now_utc: datetime, tz) -> Optional[date]:
    """UTC 瞬时所归属的业务日。tz 为 None（时区不可用）时返回 None。"""
    if tz is None:
        return None
    return _as_utc(now_utc).astimezone(tz).date()


def ms_until_next_boundary(now_utc: datetime, tz) -> Optional[int]:
    """距**下一个**业务日边界还有多少毫秒（严格大于 0）。

    用 ``datetime.combine(..., tzinfo=tz)`` 而不是对当前本地时间做加一天/替换，
    这样目标时刻的 UTC 偏移由时区规则重新求值；本业务时区没有夏令时，两种写法
    等价，但前者对未来换用时区的场景仍然正确。

    Args:
        now_utc: 当前 UTC 瞬时（naive 按 UTC 解释）。
        tz: 已解析的时区对象。

    Returns:
        毫秒数，至少为 1；时区不可用时返回 None。
    """
    if tz is None:
        return None

    local = _as_utc(now_utc).astimezone(tz)
    next_date = local.date() + timedelta(days=1)
    next_boundary = datetime.combine(next_date, dtime.min, tzinfo=tz)

    delta_ms = int((next_boundary - local).total_seconds() * 1000)
    return max(delta_ms, 1)


class HeartbeatScheduler:
    """心跳调度状态机。纯本地计算，不依赖 Qt，可无 GUI 测试。

    状态只有一项：``_reported_day`` —— 服务端最近一次**成功记账**的业务日
    （来自心跳响应的 ``active_date``，见 :meth:`mark_reported_day`）。
    由此推出全部行为：

        启动上报成功 -> 当天不再重复；定时器对齐到下一个边界
        启动上报失败 -> 当天视为尚未上报；按 watchdog 周期持续重试
        边界触发     -> 业务日已推进 -> 上报成功 -> 记入服务端返回的新业务日
        休眠跨天唤醒 -> watchdog 触发时发现业务日已推进 -> 补报
    """

    def __init__(self, tz_name: Optional[str] = None, watchdog_ms: int = WATCHDOG_MAX_MS):
        self.tz_name = tz_name or business_tz_name()
        self._tz = resolve_tz(self.tz_name)
        self._watchdog_ms = watchdog_ms
        self._reported_day: Optional[date] = None

    @property
    def available(self) -> bool:
        """业务时区是否可用。不可用时调用方应跳过周期调度。"""
        return self._tz is not None

    @property
    def reported_day(self) -> Optional[date]:
        """最近一次成功上报所属的业务日（未上报过为 None）。"""
        return self._reported_day

    def current_day(self, now_utc: datetime) -> Optional[date]:
        """``now_utc`` 所属的业务日。"""
        return business_day(now_utc, self._tz)

    def mark_reported_day(self, day: Optional[date]) -> None:
        """记录"服务端已把 ``day`` 这一天记为已上报"。

        ``day`` **必须来自服务端的记账结果**（心跳响应里的 ``active_date``），
        而不是客户端收到响应时的本地日期。

        为什么不能由本地时钟推断：请求贴着业务日边界发出、响应跨过边界时，
        两端的日期会差一天。用本地日期会让客户端把**下一天**记成"已上报"，
        而服务端实际只记了前一天 —— 下一天于是永远不会被上报，该设备少记
        一天 DAU。记账权威在服务端，客户端只负责原样转交。

        ``None``（未上报成功 / 响应不可解析）会把状态置回"无已上报日"，
        从而保持 watchdog 重试 —— 重复上报会被服务端按
        ``(device_id, active_date)`` 去重，是安全方向。
        """
        self._reported_day = day

    def should_report(self, now_utc: datetime) -> bool:
        """定时器触发时：业务日是否已推进到尚未上报的一天。"""
        day = self.current_day(now_utc)
        return day is not None and day != self._reported_day

    def next_delay_ms(self, now_utc: datetime) -> Optional[int]:
        """下一次定时器应设置的延时（毫秒）；时区不可用时返回 None。

        分两种情况：
            当前业务日尚未上报 -> 一个 watchdog 周期后重试
                （启动上报就可能失败，不能把一整天都算作已上报）
            当前业务日已上报   -> 对齐下一个边界，但不超过 watchdog 上限，
                以便休眠唤醒后还能重新评估
        """
        if self._tz is None:
            return None

        if self.should_report(now_utc):
            return self._watchdog_ms

        until_boundary = ms_until_next_boundary(now_utc, self._tz)
        if until_boundary is None:
            return None
        return max(1, min(until_boundary, self._watchdog_ms))
