# -*- coding: utf-8 -*-
"""
Phase 7.2-7e - 管理后台 datetime 展示时区（naive UTC → Asia/Shanghai）。

背景
----
数据库里所有 datetime 列都是 naive UTC（SQLAlchemy ``DateTime`` 未声明
``timezone=True``；写入侧是 ``datetime.now(timezone.utc)`` 与 ``func.now()``）。
SQLAdmin 内置的 ``BASE_FORMATTERS`` 只注册了 ``type(None)`` 与 ``bool``，
其自带的 ``datetime_formatter`` **从未被调用**，于是 datetime 列走
``_default_formatter`` 的父类回退并被原样 ``str()`` —— 这就是后台时间
比实际早 8 小时的直接成因。

本测试针对 ``app/admin/views.py`` 的展示层修复：

    1. naive UTC 被显式解释为 UTC 后换算到 Asia/Shanghai
    2. 入参对象不被原地修改（pure）
    3. 跨日边界正确
    4. 已带时区的值不会被二次平移
    5. 非 datetime 值（date / str）保持原有行为
    6. None 仍由 empty_formatter 处理
    7. 未污染上游快照的共享 BASE_FORMATTERS
    8. formatter 真的挂到了两个 ModelView 上（回归根因）

范围
----
只测纯函数与类属性，不连接数据库、不启动服务器、不写入任何文件。
``tzdata`` 缺失时（Windows 上没有系统 IANA 库）换算类断言记为 SKIP，
但结构性断言照常执行。
"""

import datetime
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# 必须在导入任何 app 模块之前设置：config / database 在导入时即读取
# DATABASE_URL 并建立 engine。用临时目录，避免碰到开发库。
TMPDIR = pathlib.Path(tempfile.mkdtemp(prefix="dlv-admintz-test-"))
os.environ["DATABASE_URL"] = "sqlite:///" + (TMPDIR / "admintz.db").as_posix()

sys.path.insert(0, str(HERE / "app"))

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


def skip(msg):
    global SKIPPED
    SKIPPED += 1
    print("  [skip] " + msg, flush=True)


def main():
    global PASSED, FAILED, SKIPPED

    print("Phase 7.2-7e - admin datetime display timezone", flush=True)
    print("-" * 64, flush=True)

    try:
        from admin import vendored_sqladmin_available
    except ImportError as exc:
        print("  [skip] 无法导入 admin 包（%s）" % exc, flush=True)
        SKIPPED += 1
        return 0

    if not vendored_sqladmin_available():
        print(
            "  [skip] admin_web/ 快照未投放（gitignored，单独投放），"
            "本测试无法运行",
            flush=True,
        )
        SKIPPED += 1
        return 0

    from sqladmin.formatters import BASE_FORMATTERS

    from admin import views

    # ------------------------------------------------------------------
    # 7) 未污染上游快照的共享对象
    # ------------------------------------------------------------------
    print("\n[上游快照隔离]", flush=True)
    check(
        views.DISPLAY_TZ_FORMATTERS is not BASE_FORMATTERS,
        "使用副本而非就地修改共享的 BASE_FORMATTERS",
    )
    check(
        datetime.datetime not in BASE_FORMATTERS,
        "未向上游 BASE_FORMATTERS 注入 datetime 键（快照保持逐字节一致）",
    )

    # ------------------------------------------------------------------
    # 5+6) 非 datetime 与 None 的原有行为
    # ------------------------------------------------------------------
    print("\n[保持原有行为]", flush=True)
    d = datetime.date(2026, 9, 17)
    check(views._datetime_formatter(d) is d, "datetime.date 原样返回，不做换算")
    s = "2026-09-17 04:30:00"
    check(views._datetime_formatter(s) == s, "str 原样返回")
    check(
        views.DISPLAY_TZ_FORMATTERS.get(type(None)) is BASE_FORMATTERS[type(None)],
        "None 仍由上游 empty_formatter 处理",
    )
    check(str(views._datetime_formatter(None)) == "", "None 输入返回空串而非空值")

    # ------------------------------------------------------------------
    # 8) formatter 真的挂上去了（回归本次 bug 的根因）
    # ------------------------------------------------------------------
    print("\n[注册生效]", flush=True)
    for cls in (views.LicenseAdmin, views.AuthorizationAdmin):
        check(
            cls.column_type_formatters.get(datetime.datetime)
            is views._datetime_formatter,
            "%s 已注册 datetime formatter" % cls.__name__,
        )
        # SQLAdmin 在 column_type_formatters_detail 保持默认时，会自动沿用
        # column_type_formatters；该分支的触发条件就是这个不等式。
        check(
            cls.column_type_formatters != BASE_FORMATTERS,
            "%s 的列表与详情页都会应用该 formatter" % cls.__name__,
        )

    # ------------------------------------------------------------------
    # 1~4) 时区换算（需要 IANA 时区库）
    # ------------------------------------------------------------------
    print("\n[时区换算]", flush=True)
    if views._DISPLAY_TZ is None:
        skip("缺少 IANA 时区库（未安装 tzdata），换算断言无法执行")
        skip("跨日边界断言无法执行")
        skip("aware 值断言无法执行")
        skip("入参不可变断言无法执行")
    else:
        check(views.DISPLAY_TZ_NAME == "Asia/Shanghai", "展示时区为 Asia/Shanghai")

        # 数据库里读出来的样子：naive，值为 UTC 墙钟
        utc_naive = datetime.datetime(2026, 9, 17, 4, 30, 0)
        before = utc_naive.isoformat()
        html = str(views._datetime_formatter(utc_naive))

        check("12:30:00" in html, "naive UTC 04:30 -> 北京 12:30:00")
        check("04:30:00" not in html, "渲染结果中不再出现原始 UTC 时刻")
        check("Asia/Shanghai" in html, "展示带时区标注，不可误读")
        check(
            utc_naive.isoformat() == before and utc_naive.tzinfo is None,
            "入参对象未被原地修改（仍为 naive，值不变）",
        )

        # 跨日：UTC 09-17 20:00 == 北京时间 09-18 04:00
        html = str(
            views._datetime_formatter(datetime.datetime(2026, 9, 17, 20, 0, 0))
        )
        check(
            "18 September 2026" in html and "04:00:00" in html,
            "UTC 09-17 20:00 -> 北京 09-18 04:00（正确跨日）",
        )

        # aware 值按其自身偏移换算，不二次平移
        aware_utc = datetime.datetime(
            2026, 9, 17, 4, 30, tzinfo=datetime.timezone.utc
        )
        check(
            "12:30:00" in str(views._datetime_formatter(aware_utc)),
            "已带 UTC 时区的 aware 值与 naive 结果一致",
        )
        aware_bj = datetime.datetime(
            2026, 9, 17, 12, 30, tzinfo=views._DISPLAY_TZ
        )
        check(
            "12:30:00" in str(views._datetime_formatter(aware_bj)),
            "已带 +08 的 aware 值原样呈现，未被二次平移",
        )

    print("\n" + "-" * 64, flush=True)
    print(
        "PASSED=%d  FAILED=%d  SKIPPED=%d" % (PASSED, FAILED, SKIPPED),
        flush=True,
    )
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
