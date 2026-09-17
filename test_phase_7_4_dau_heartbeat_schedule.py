# -*- coding: utf-8 -*-
"""
Phase 7.4 — 心跳调度（对齐业务日边界）单元验证。

被钉住的契约
------------
事件在服务端按业务日（BUSINESS_TZ_NAME，默认 Asia/Shanghai）去重，所以真正的
目标不是"多久发一次"，而是"**会话跨过的每一个业务日各被报到一次**"。

    1. 启动立即上报（调用方行为，本文件只验证调度器与之配合的初始状态）
    2. 跨 UTC+8 午夜：边界时刻计算正确
    3. 连续多日：多日会话中每个业务日恰好被报到一次
    4. 定时器每次重新对齐：不累积漂移（跑 10 天，误差不得增长）
    5. 休眠/唤醒补发：跨天休眠后唤醒，发现业务日已推进 -> 补报
    6. 上报失败则当天持续重试（不能把失败当成"今天报过了"）
    7. 业务时区不可用时降级：不调度，而不是猜一个偏移

范围
----
纯本地计算，不联网、不依赖 PySide6、不触碰数据库。用虚拟时钟驱动调度器，
因此不受真实时间影响、可重复。

运行：python test_phase_7_4_dau_heartbeat_schedule.py
"""

import datetime
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from utils.heartbeat_schedule import (  # noqa: E402
    WATCHDOG_MAX_MS,
    HeartbeatScheduler,
    ms_until_next_boundary,
    resolve_tz,
)

UTC = datetime.timezone.utc
TZ_NAME = "Asia/Shanghai"

PASSED = 0
FAILED = 0


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


def tz():
    z = resolve_tz(TZ_NAME)
    assert z is not None, "需要 IANA 时区库（tzdata）"
    return z


def local(y, mo, d, h, mi=0):
    """构造一个业务时区本地时刻，返回等价的 UTC 瞬时。"""
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz()).astimezone(UTC)


def mark_day(scheduler, when):
    """测试便捷函数：模拟"服务端把 ``when`` 所属的业务日记为已上报"。

    生产代码里这个日期来自心跳响应的 ``active_date``（服务端是记账权威）。
    本测试不模拟网络，因此用调度器自己的业务日换算作为等价输入 —— 被验证的
    是"给定已记账某天后调度器的行为"，而不是这一天从哪来。
    """
    scheduler.mark_reported_day(scheduler.current_day(when))


def run_schedule(scheduler, start_utc, hours,
                 initial_report=True, fail_days=None):
    """用虚拟时钟驱动调度器，返回实际发生的上报时刻列表。

    复刻 MainWindow 的循环：定时器到点 -> should_report? -> 上报 -> 重新对齐。
    `fail_days` 里的业务日会让上报失败（不标记），用于验证重试。
    """
    reports = []
    now = start_utc
    deadline = start_utc + datetime.timedelta(hours=hours)
    fail_days = fail_days or set()

    def attempt(when):
        day = scheduler.current_day(when)
        if day in fail_days:
            return False          # 失败：刻意不标记业务日
        scheduler.mark_reported_day(day)
        reports.append(when)
        return True

    if initial_report:
        attempt(now)

    guard = 0
    while now < deadline and guard < 1_000_000:
        guard += 1
        delay = scheduler.next_delay_ms(now)
        if delay is None:
            break
        now = now + datetime.timedelta(milliseconds=delay)
        if now > deadline:
            break
        if scheduler.should_report(now):
            attempt(now)
    return reports


def days_of(reports, scheduler):
    return [scheduler.current_day(r) for r in reports]


# ----------------------------------------------------------------------
def test_boundary_math():
    print("\n[跨 UTC+8 午夜：边界计算]", flush=True)
    z = tz()

    cases = [
        # (UTC 瞬时,                期望距下一个边界的毫秒,  说明)
        ((2026, 9, 17, 15, 59), 60_000, "UTC+8 23:59 → 还差 1 分钟到午夜"),
        ((2026, 9, 17, 16, 0), 86_400_000, "UTC+8 00:00 整 → 正好一整天"),
        ((2026, 9, 17, 16, 30), 86_400_000 - 1_800_000, "UTC+8 00:30"),
        ((2026, 9, 17, 10, 0), 6 * 3600 * 1000, "UTC+8 18:00 → 还差 6 小时"),
        # 这一条最容易写错：UTC 23:59 在 UTC+8 是**次日 07:59**，
        # 所以距下一个午夜还有 16 小时 1 分，而不是 1 分钟。
        ((2026, 9, 17, 23, 59), 57_660_000, "UTC+8 次日 07:59 → 还差 16h01m"),
    ]
    for (y, mo, d, h, mi), expect_ms, label in cases:
        got = ms_until_next_boundary(
            datetime.datetime(y, mo, d, h, mi, tzinfo=UTC), z
        )
        check(
            got == expect_ms,
            f"{label}: {got} ms（期望 {expect_ms}）",
        )

    # 边界必须严格为未来：任意时刻的剩余毫秒都 > 0
    sample = [
        ms_until_next_boundary(
            datetime.datetime(2026, 9, 17, h, m, tzinfo=UTC), z
        )
        for h in range(0, 24, 3) for m in (0, 30)
    ]
    check(all(v is not None and v > 0 for v in sample), "任意时刻的剩余毫秒恒为正")


def test_consecutive_days():
    print("\n[连续多日：每个业务日恰好报到一次]", flush=True)
    s = HeartbeatScheduler(TZ_NAME)
    check(s.available, "业务时区可用")

    # 会话：本地 D 20:00 启动，持续 48 小时（跨 2 次午夜，覆盖 D / D+1 / D+2）
    start = local(2026, 9, 17, 20, 0)
    reports = run_schedule(s, start, hours=48)

    days = days_of(reports, s)
    expected = [datetime.date(2026, 9, 17), datetime.date(2026, 9, 18),
                datetime.date(2026, 9, 19)]
    check(days == expected, f"覆盖 3 个业务日且各一次：{days}")
    check(len(days) == len(set(days)), "没有任何业务日被重复上报")
    check(len(reports) == 3, f"48 小时会话共上报 3 次（实际 {len(reports)}）")

    # 每一次上报都必须紧贴对应的边界（启动那次除外）
    for r in reports[1:]:
        rl = r.astimezone(tz())
        offset_ms = (rl.hour * 3600 + rl.minute * 60 + rl.second) * 1000
        check(
            offset_ms <= WATCHDOG_MAX_MS,
            f"{r} 落在边界后 {rl.hour:02d}:{rl.minute:02d}（不超过 watchdog 上限）",
        )


def test_no_drift():
    print("\n[定时器重新对齐：10 天无漂移]", flush=True)
    s = HeartbeatScheduler(TZ_NAME)
    start = local(2026, 9, 17, 23, 30)          # 故意贴着午夜启动
    reports = run_schedule(s, start, hours=24 * 10)

    days = days_of(reports, s)
    check(len(days) == len(set(days)), "10 天内每个业务日只上报一次")
    check(len(reports) == 11, f"启动日 + 10 次边界 = 11 次上报（实际 {len(reports)}）")

    # 漂移检查：第 N 次边界上报距其边界的偏移，不应随天数增长
    offsets = []
    for r in reports[1:]:
        rl = r.astimezone(tz())
        offsets.append((rl.hour * 3600 + rl.minute * 60 + rl.second) * 1000)
    check(
        max(offsets) - min(offsets) <= WATCHDOG_MAX_MS,
        f"边界偏移不随天数增长（σ={max(offsets) - min(offsets)}ms ≤ watchdog）",
    )

    # 直接验证"重新计算"：一次边界上报之后，下一次延时必须是到再下一个边界的全长
    s2 = HeartbeatScheduler(TZ_NAME)
    at_boundary = local(2026, 9, 18, 0, 0)
    mark_day(s2, at_boundary)
    check(
        s2.next_delay_ms(at_boundary) == WATCHDOG_MAX_MS,
        "刚过边界：延时被 watchdog 封顶（而非沿用固定的 24h 或 1h）",
    )


def test_sleep_across_midnight():
    print("\n[休眠/唤醒补发]", flush=True)
    s = HeartbeatScheduler(TZ_NAME)

    # 启动于本地 D 20:00，上报成功
    start = local(2026, 9, 17, 20, 0)
    mark_day(s, start)
    check(not s.should_report(start), "启动后当天不再重复上报")

    # 模拟休眠：系统在 D 22:00 挂起，D+1 08:00 唤醒（期间定时器不会触发）
    check(
        not s.should_report(local(2026, 9, 17, 22, 0)),
        "休眠前（同一业务日）判定为无需上报",
    )
    woke = local(2026, 9, 18, 8, 0)
    check(s.should_report(woke), "唤醒后判定为需要补报（业务日已推进）")

    mark_day(s, woke)
    check(
        s.reported_day == datetime.date(2026, 9, 18),
        f"补报记入唤醒当天：{s.reported_day}",
    )
    check(not s.should_report(woke), "补报后当天不再重复")

    # 长时间休眠跨过一整天：只补当前这一天，不会倒着补历史
    s3 = HeartbeatScheduler(TZ_NAME)
    mark_day(s3, local(2026, 9, 17, 10, 0))
    far = local(2026, 9, 20, 9, 0)
    check(s3.should_report(far), "跨过多天后唤醒仍会补报")
    mark_day(s3, far)
    check(
        s3.reported_day == datetime.date(2026, 9, 20),
        "只记入唤醒当天的业务日（历史缺失不可追补，符合预期）",
    )


def test_retry_on_failure():
    print("\n[上报失败 -> 当天持续重试]", flush=True)
    s = HeartbeatScheduler(TZ_NAME)
    start = local(2026, 9, 17, 10, 0)

    # 当天一直失败：调度器必须保持"未上报"，且以 watchdog 为节奏重试
    check(s.should_report(start), "尚未成功上报时判定为需要上报")
    check(
        s.next_delay_ms(start) == WATCHDOG_MAX_MS,
        "未成功上报 -> 一个 watchdog 周期后重试（不是等一整天）",
    )

    attempts = 0
    now = start
    for _ in range(4):
        if s.should_report(now):
            attempts += 1                 # 模拟失败：不 mark_reported
        delay = s.next_delay_ms(now)
        check(delay == WATCHDOG_MAX_MS, "失败后延时仍是 watchdog 周期")
        now = now + datetime.timedelta(milliseconds=delay)
    check(attempts == 4, f"失败期间持续重试 {attempts} 次，未把失败当成成功")
    check(
        s.current_day(now) == datetime.date(2026, 9, 17),
        "4 次重试只推进 1 小时，仍在同一业务日",
    )

    # 从临近午夜处开始重试，跨越午夜后应自然转向新的一天
    s2 = HeartbeatScheduler(TZ_NAME)
    late = local(2026, 9, 17, 23, 0)
    now = late
    for _ in range(8):                    # 8 × 15 分钟 = 2 小时，足以跨过午夜
        if s2.should_report(now):
            pass                          # 仍然失败，不 mark_reported
        delay = s2.next_delay_ms(now)
        now = now + datetime.timedelta(milliseconds=delay)
    check(
        s2.current_day(now) == datetime.date(2026, 9, 18),
        f"重试期间跨过午夜，业务日已推进到 {s2.current_day(now)}",
    )
    check(
        s2.should_report(now) and s2.reported_day is None,
        "跨天后仍判定为需要上报（失败次数不影响'是否已上报'）",
    )


def test_mark_reported_day_contract():
    """_reported_day 只由"服务端返回的业务日"驱动，与本地时钟无关。"""
    print("\n[标记契约：记的是服务端那一天]", flush=True)
    s = HeartbeatScheduler(TZ_NAME)
    today = s.current_day(local(2026, 9, 17, 10, 0))

    s.mark_reported_day(today)
    check(s.reported_day == today, "记入传入的服务端业务日")
    check(not s.should_report(local(2026, 9, 17, 23, 0)), "该日不再重复上报")

    # 失败路径：None -> 回到"无已上报日"，保持重试
    s.mark_reported_day(None)
    check(s.reported_day is None, "None 不产生已上报日")
    check(s.should_report(local(2026, 9, 17, 23, 0)), "失败后仍判定为需要上报")
    check(
        s.next_delay_ms(local(2026, 9, 17, 23, 0)) == WATCHDOG_MAX_MS,
        "失败后按 watchdog 周期重试",
    )

    # 服务端说是哪一天就记哪一天，即使与本地此刻的业务日不同
    s.mark_reported_day(datetime.date(2026, 1, 2))
    check(
        s.reported_day == datetime.date(2026, 1, 2),
        "原样记入服务端返回的日期，不参照本地时钟",
    )
    check(
        s.should_report(local(2026, 9, 17, 10, 0)),
        "服务端日期与本地业务日不同 -> 本地这一天仍判定为需要上报",
    )


def test_timezone_unavailable():
    print("\n[业务时区不可用时降级]", flush=True)
    s = HeartbeatScheduler("Not/AZone")
    check(not s.available, "无法解析的时区 -> available 为 False")
    check(
        s.next_delay_ms(local(2026, 9, 17, 10, 0)) is None,
        "时区不可用 -> 不返回延时（调用方应跳过周期调度）",
    )
    check(s.should_report(local(2026, 9, 17, 10, 0)) is False,
          "时区不可用 -> 永不判定为需要上报（不猜偏移、不误记）")


def main():
    global PASSED, FAILED

    print("Phase 7.4 — heartbeat schedule (aligned to business-day boundary)",
          flush=True)
    print("-" * 64, flush=True)

    z = resolve_tz(TZ_NAME)
    if z is None:
        print("  [skip] 缺少 IANA 时区库（未安装 tzdata），无法运行本测试",
              flush=True)
        return 0

    test_boundary_math()
    test_consecutive_days()
    test_no_drift()
    test_sleep_across_midnight()
    test_retry_on_failure()
    test_mark_reported_day_contract()
    test_timezone_unavailable()

    print("\n" + "-" * 64, flush=True)
    print("PASSED=%d  FAILED=%d" % (PASSED, FAILED), flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
