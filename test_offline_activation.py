#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
临时离线灾备激活的验收测试。

覆盖需求里的 A-I：
  A 合法 KEY（1 天 / 30 天）
  B KEY 规范化（大小写、"-"、空格）
  C 同一台电脑重复消费同一个 KEY 必须失败
  D 不同 KEY 可以连续使用
  E 离线激活覆盖当前授权（不累加）
  F 无效 KEY 失败且不破坏当前有效授权
  G 授权写入失败时 KEY 不被消费
  H 重启（重新构造 LicenseManager）后已消费记录仍然存在
  I 原有在线激活路径未被破坏

不联网、不调用 susi_helper、不读写真实的 %APPDATA%。
"""

import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from utils import offline_activation as oa  # noqa: E402
from utils.license_client import REASON_UNREACHABLE, LicenseServerError  # noqa: E402
from utils.license_manager import (  # noqa: E402
    LicenseManager,
    LicenseStatus,
)
from utils.license_store import LicenseStore  # noqa: E402

_PASS = []
_FAIL = []


def check(name, condition, detail=""):
    (_PASS if condition else _FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  -- ' + detail) if detail and not condition else ''}")


def make_manager(data_dir):
    """构造一个完全隔离、不会联网的 LicenseManager。"""
    return LicenseManager(
        server_url="http://127.0.0.1:1",
        store=LicenseStore(data_dir=data_dir),
    )


def days_from_now(expires_at):
    return (expires_at - datetime.now(timezone.utc)).total_seconds() / 86400.0


# ----------------------------------------------------------------------
# KEY 池完整性
# ----------------------------------------------------------------------
def test_key_pool():
    one_day = [k for k, h in oa.OFFLINE_KEYS.items() if h == 24]
    thirty = [k for k, h in oa.OFFLINE_KEYS.items() if h == 720]
    check("KEY 池共 20 个", len(oa.OFFLINE_KEYS) == 20, f"实际 {len(oa.OFFLINE_KEYS)}")
    check("1 天 KEY 10 个", len(one_day) == 10, f"实际 {len(one_day)}")
    check("30 天 KEY 10 个", len(thirty) == 10, f"实际 {len(thirty)}")
    check("只有 1 天与 30 天两种时长",
          all(h in (24, 720) for h in oa.OFFLINE_KEYS.values()))
    check("KEY 规范形式唯一（无重复）",
          len({oa.normalize_key(k) for k in oa.OFFLINE_KEYS}) == len(oa.OFFLINE_KEYS))
    check("规范化不会改变 KEY 池内的 KEY",
          all(normalize_roundtrip(k) for k in oa.OFFLINE_KEYS))
    check("KEY 形状为 12 位字母数字", all(k.isalnum() and len(k) == 12 for k in oa.OFFLINE_KEYS))


def normalize_roundtrip(key):
    return oa.normalize_key(oa.format_key(key)) == key


# ----------------------------------------------------------------------
# B. 规范化
# ----------------------------------------------------------------------
def test_normalization():
    forms = [
        "ABCD-EFGH-IJKL",
        "abcd-efgh-ijkl",
        "ABCD EFGH IJKL",
        "ABCDEFGHIJKL",
        "  abcd efgh ijkl  ",
        "aBcD-eFgH-iJkL",
        "ABCD--EFGH--IJKL",
        "ABCD\nEFGH\tIJKL",
    ]
    check("B 全部写法归一为同一 KEY",
          len({oa.normalize_key(f) for f in forms}) == 1,
          str({oa.normalize_key(f) for f in forms}))
    check("B 归一结果为大写无分隔", oa.normalize_key(forms[0]) == "ABCDEFGHIJKL")
    check("B 空/None 归一为空串",
          oa.normalize_key("") == "" and oa.normalize_key(None) == "" and oa.normalize_key(" - ") == "")


# ----------------------------------------------------------------------
# A / C / D / E / H：主流程
# ----------------------------------------------------------------------
def test_activation_flow():
    day_key = next(k for k, h in oa.OFFLINE_KEYS.items() if h == 24)
    month_key = next(k for k, h in oa.OFFLINE_KEYS.items() if h == 720)

    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d)

        # A. 1 天 KEY
        r = m.activate_offline(oa.format_key(day_key))
        check("A 1 天 KEY 激活成功", r.is_activated, r.status.value)
        check("A 1 天 = 24 小时",
              abs(days_from_now(r.expires_at) - 1.0) < 0.01,
              f"{days_from_now(r.expires_at):.3f} 天")
        check("A 成功文案包含时长", "1 天" in r.message, r.message)

        # B. 用另一种写法重复输入同一个 KEY 必须失败
        r2 = m.activate_offline(day_key.lower().replace("-", " "))
        check("C 同一 KEY 第二次（换写法）失败",
              r2.status is LicenseStatus.INVALID, r2.status.value)
        check("C 提示为已使用", oa.TEXT_KEY_USED in r2.message, r2.message)

        # D / E. 不同 KEY 连续使用，且覆盖而非累加
        r3 = m.activate_offline(oa.format_key(month_key))
        check("D 不同 KEY 可以连续使用", r3.is_activated, r3.status.value)
        check("E 30 天 KEY 覆盖为 30 天",
              abs(days_from_now(r3.expires_at) - 30.0) < 0.01,
              f"{days_from_now(r3.expires_at):.3f} 天")

        other_day = sorted(k for k, h in oa.OFFLINE_KEYS.items() if h == 24 and k != day_key)[0]
        r4 = m.activate_offline(other_day)
        check("E 再次用 1 天 KEY 覆盖为 1 天",
              r4.is_activated and abs(days_from_now(r4.expires_at) - 1.0) < 0.01,
              f"{days_from_now(r4.expires_at):.3f} 天")

        # H. 重启后：已消费记录仍在，授权仍在
        m2 = make_manager(d)
        check("H 重启后授权仍有效",
              m2.load_stored().is_activated,
              m2.load_stored().status.value)
        used = m2.offline.load_used_keys()
        check("H 重启后已消费 KEY 记录仍存在",
              {day_key, month_key, other_day} <= used,
              str(sorted(used)))
        r5 = m2.activate_offline(oa.format_key(month_key))
        check("H 重启后重复使用已消费 KEY 仍然失败",
              r5.status is LicenseStatus.INVALID, r5.status.value)


# ----------------------------------------------------------------------
# F. 无效 KEY 不破坏当前授权
# ----------------------------------------------------------------------
def test_invalid_key_does_not_damage_state():
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d)
        good = next(iter(oa.OFFLINE_KEYS))
        m.activate_offline(good)
        before = m.load_stored().expires_at

        for bad in ["", "XXXX-XXXX-XXXX", "1234", "ABCD-EFGH-IJKM", "abc def ghi"]:
            r = m.activate_offline(bad)
            if r.is_activated:
                check(f"F 无效 KEY {bad!r} 被拒绝", False, "竟然激活成功")
                return
            if r.message != oa.TEXT_INVALID_KEY:
                check(f"F 无效 KEY {bad!r} 提示正确", False, r.message)
                return

        check("F 无效 KEY 一律拒绝且提示「无效的离线 KEY」", True)
        after = m.load_stored()
        check("F 当前授权未被破坏且未改变",
              after.is_activated and after.expires_at == before)


# ----------------------------------------------------------------------
# G. 授权写入失败 → KEY 不被消费
# ----------------------------------------------------------------------
def test_write_failure_does_not_consume_key():
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d)
        key = next(iter(oa.OFFLINE_KEYS))

        original = m.offline.save_license

        def boom(_hours):
            raise OSError("模拟磁盘写入失败")

        m.offline.save_license = boom
        r = m.activate_offline(oa.format_key(key))
        check("G 写入失败返回激活失败",
              r.status is LicenseStatus.ACTIVATION_FAILED, r.status.value)
        check("G 写入失败时 KEY 未被消费", not m.offline.is_used(key))
        check("G 写入失败时未产生授权", m.offline.load_license() is None)

        # 恢复后同一个 KEY 仍然可用 —— 证明上一步没有把 KEY 吃掉
        m.offline.save_license = original
        r2 = m.activate_offline(oa.format_key(key))
        check("G 恢复写入后同一个 KEY 仍可使用", r2.is_activated, r2.status.value)


# ----------------------------------------------------------------------
# I. 原有在线激活路径未被破坏
# ----------------------------------------------------------------------
def test_online_path_intact():
    with tempfile.TemporaryDirectory() as d:
        m = make_manager(d)

        # 服务器不可达 → 仍然映射为「激活失败（可重试）」，不落盘、不消费
        def unreachable(_key, _machine):
            raise LicenseServerError(REASON_UNREACHABLE, "无法连接许可证服务器")

        m.client.activate = unreachable
        m.get_machine_code = lambda refresh=False: "TEST-MACHINE"

        r = m.activate("SOME-ONLINE-KEY")
        check("I 在线激活仍走服务器且失败可重试",
              r.status is LicenseStatus.ACTIVATION_FAILED, r.status.value)

        # 在线失败不会伪造出任何离线授权
        check("I 在线激活失败不产生离线授权", m.offline.load_license() is None)

        # 服务器成功 + 本地验签失败 → 依旧不落盘（原有语义）
        m.client.activate = lambda key, machine: {
            "signed_license": json.dumps({"license_data": "{}", "signature": "x"})
        }
        r2 = m.activate("SOME-ONLINE-KEY")
        check("I 凭据非法时在线激活不落盘",
              not r2.is_activated and m.store.load() is None, r2.status.value)

        # 没有离线授权时，load_stored 仍走原有的「未激活」路径
        check("I 无离线授权时 load_stored 仍为未激活",
              m.load_stored().status is LicenseStatus.NOT_ACTIVATED)

        # 在线激活方法本身仍是原来的入口
        check("I activate() 仍然存在且未被离线逻辑接管",
              callable(getattr(LicenseManager, "activate", None)))


def main():
    test_key_pool()
    test_normalization()
    test_activation_flow()
    test_invalid_key_does_not_damage_state()
    test_write_failure_does_not_consume_key()
    test_online_path_intact()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for name in _FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
