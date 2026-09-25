#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
StreamHistory 用户可写数据目录 —— 打包收口验证

问题：
    Phase 0 之前历史记录写在 ``Path(__file__).parent.parent / "stream_history.json"``。
    源码环境下那确实是项目根；但**冻结（PyInstaller onedir）环境下 ``__file__``
    落在 ``_MEIPASS``，也就是 ``_internal/``**，于是写进了安装目录：
        1. 更新/卸载整体替换安装目录时会连带删掉用户的历史记录；
        2. 安装目录不可写时（例如解压到 Program Files），保存静默失效。

规则（唯一一条）：
    用户可写数据目录 = ``<LOCALAPPDATA>/LiveLens``（其它平台 ``~/.local/share/LiveLens``），
    与 ``sys.frozen`` / ``sys._MEIPASS`` / ``sys.executable`` / 模块 ``__file__``
    **完全无关**。源码环境、冻结 onedir、安装版首次启动走的是同一条路径规则 ——
    没有"开发分支/冻结分支"两套逻辑，只有一套，因此不可能再次漂移回安装目录。

    ``Path(__file__).parent.parent`` 仍然出现在 ``_legacy_file_path()`` 里，但那
    是**一次性迁移的只读来源**（把旧安装目录里的历史搬走），不是写入目标。
    C 节把这件事也钉住：迁移之后旧文件必须逐字节未被改写。

本测试的隔离方式（不依赖当前机器）：
    全程把 LOCALAPPDATA / XDG_DATA_HOME 指向临时目录，**绝不读写当前机器的
    真实用户目录**；冻结分支不靠"当前真的跑在冻结环境里"，而是把 sys.frozen /
    sys._MEIPASS / sys.executable / 模块 __file__ 改写成伪造的安装目录，再断言
    写入位置**不受它们影响**。

覆盖（题目要求的 6 项）：
    A. 目录规则本身                 D. 目录不存在时自动创建（安装版首次启动）
    B. 源码（开发）环境             E. 读写失败时的降级行为
    C. 冻结环境 + 旧文件迁移        F. API / JSON 格式 / 历史语义不变

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_stream_history_paths.py`` 运行，不依赖 pytest。
纯文件系统操作，不联网、不起线程、不启动 GUI。
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import utils.stream_history as sh  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {message}")
        return True
    _FAILED += 1
    print(f"  [FAIL] {message}")
    return False


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


#: 本测试全部临时产物的根。程序退出后由被操作系统回收，不清理也可以。
_TMP_ROOT = tempfile.mkdtemp(prefix="dlv-history-paths-")

#: 需要接管的环境变量：LOCALAPPDATA 对 Windows 生效，XDG_DATA_HOME 对 POSIX 生效。
#: 两个都指到同一个临时根，断言就能跨平台写成同一个表达式。
_ENV_KEYS = ("LOCALAPPDATA", "XDG_DATA_HOME")
_SAVED_ENV = {key: os.environ.get(key) for key in _ENV_KEYS}


def set_data_root(name: str) -> str:
    """把「用户数据根」指向 ``<临时根>/<name>``，返回该目录。

    只改环境变量，不创建目录 —— 「目录不存在时自动创建」正是 D 节要验证的。
    """
    root = os.path.join(_TMP_ROOT, name)
    for key in _ENV_KEYS:
        os.environ[key] = root
    return root


def expected_history_path(root: str) -> str:
    """按规则推出的期望路径：``<用户数据根>/LiveLens/stream_history.json``。"""
    return os.path.join(root, sh.APP_DIR_NAME, sh.HISTORY_FILENAME)


def read_json(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def under(path, parent) -> bool:
    """path 是否位于 parent 之内（都用绝对规范路径比较）。"""
    try:
        Path(os.path.abspath(str(path))).relative_to(
            os.path.abspath(str(parent)))
        return True
    except ValueError:
        return False


@contextlib.contextmanager
def quiet():
    """吞掉被测代码在降级路径上打印的错误信息（E 节会故意制造失败）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# ======================================================================
section("A. 数据目录规则（纯字符串，不写任何东西）")
# ======================================================================
root_a = set_data_root("a")
check(sh.default_data_dir() == os.path.join(root_a, sh.APP_DIR_NAME),
      f"数据目录 = <LOCALAPPDATA>/<APP_DIR_NAME>（实际 {sh.default_data_dir()}）")
check(sh.APP_DIR_NAME == "LiveLens",
      "目录名是 LiveLens（产品名，与 EXE 名 DouyinLowLatencyViewer 刻意不同）")
check(sh.HISTORY_FILENAME == "stream_history.json", "文件名保持不变（兼容旧数据）")
check(sh.default_data_dir() == sh.default_data_dir(),
      "同一环境下重复调用结果稳定")

# 环境变量缺失时的回退：退到用户主目录，**仍然不碰安装目录**
with mock.patch.dict(os.environ, {}, clear=False):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    fake_home = os.path.join(_TMP_ROOT, "fake-home")
    with mock.patch("os.path.expanduser", return_value=fake_home):
        fallback = sh.default_data_dir()
check(fallback == os.path.join(fake_home, sh.APP_DIR_NAME),
      f"环境变量缺失时回退到用户主目录（实际 {fallback}）")
check(not under(fallback, PROJECT_ROOT),
      "回退也不会指回源码树 / 安装目录")

# 环境变量变了，解析结果立刻跟着变（说明没有被缓存成模块级常量）
root_a2 = set_data_root("a2")
check(sh.default_data_dir() == os.path.join(root_a2, sh.APP_DIR_NAME),
      "环境变量改变后立即生效（目录在调用时解析，未缓存）")


# ======================================================================
section("B. 源码（开发）环境：默认位置不落在源码树 / EXE 目录")
# ======================================================================
root_b = set_data_root("b")
store_b = sh.StreamHistory()

check(str(store_b._file_path) == expected_history_path(root_b),
      f"默认构造写入 <LOCALAPPDATA>/LiveLens（实际 {store_b._file_path}）")
check(not under(store_b._file_path, PROJECT_ROOT),
      "默认写入位置不在源码树内")
check(os.path.abspath(str(store_b._file_path)) != os.path.abspath(
          os.path.join(PROJECT_ROOT, sh.HISTORY_FILENAME)),
      "默认写入位置不是历史上的源码树根 stream_history.json")
check(Path(str(store_b._file_path)) != Path(sh.__file__).resolve().parents[1]
      / sh.HISTORY_FILENAME,
      "默认写入位置不再由模块 __file__ 推导")
check(not under(store_b._file_path, os.path.dirname(os.path.abspath(sys.executable))),
      "默认写入位置不在解释器/EXE 目录内")

# file_path 注入是既有 API，必须保持
explicit = os.path.join(_TMP_ROOT, "b-explicit", "hist.json")
store_b2 = sh.StreamHistory(file_path=explicit)
check(str(store_b2._file_path) == os.path.abspath(explicit)
      or str(store_b2._file_path) == explicit,
      "显式 file_path 注入仍然被尊重（测试/调用方 API 未变）")
store_b2.add_or_update("https://live.douyin.com/999", "注入路径")
check(Path(explicit).is_file(), "显式路径可正常写入")


# ======================================================================
section("C. 冻结环境（PyInstaller onedir）+ 旧文件一次性迁移")
# ======================================================================
# 伪造一个安装目录：<tmp>/c-install/DouyinLowLatencyViewer.exe
#                  <tmp>/c-install/_internal/utils/stream_history.py
install_dir = os.path.join(_TMP_ROOT, "c-install")
internal_dir = os.path.join(install_dir, "_internal")
os.makedirs(os.path.join(internal_dir, "utils"), exist_ok=True)
fake_module_file = os.path.join(internal_dir, "utils", "stream_history.py")
# 旧版本真正会写的那个位置（本次修复要离开的地方）
legacy_in_internal = os.path.join(internal_dir, sh.HISTORY_FILENAME)
legacy_payload = {"streams": [{"url": "https://live.douyin.com/111",
                               "nickname": "旧记录",
                               "last_used": "2026-01-01T00:00:00"}]}
with open(legacy_in_internal, "w", encoding="utf-8") as f:
    json.dump(legacy_payload, f, ensure_ascii=False)
legacy_bytes_before = Path(legacy_in_internal).read_bytes()

root_c = set_data_root("c")
with mock.patch.object(sys, "frozen", True, create=True), \
        mock.patch.object(sys, "_MEIPASS", internal_dir, create=True), \
        mock.patch.object(sys, "executable",
                          os.path.join(install_dir, "DouyinLowLatencyViewer.exe")), \
        mock.patch.object(sh, "__file__", fake_module_file):
    # 冻结环境下旧位置的推导：__file__ 在 _MEIPASS 里 -> 推导出 _internal/
    check(Path(sh._legacy_file_path()) == Path(internal_dir) / sh.HISTORY_FILENAME,
          "旧位置推导确实指向 _MEIPASS（即 _internal）—— 这正是要离开的地方")
    store_c = sh.StreamHistory()
    frozen_write_path = str(store_c._file_path)

    check(frozen_write_path == expected_history_path(root_c),
          f"冻结环境下写入用户数据目录（实际 {frozen_write_path}）")
    check(not under(frozen_write_path, install_dir),
          "冻结环境下写入位置**不在安装目录内**（本项修复的核心）")
    check(not under(frozen_write_path, internal_dir),
          "冻结环境下写入位置不在 _internal/ 内")

    # 迁移：旧安装目录里的历史被搬到用户数据目录（只读来源 -> 新目标）
    check(Path(frozen_write_path).is_file(), "旧文件已迁移到用户数据目录")
    migrated = store_c.get_all()
    check([s["url"] for s in migrated] == ["https://live.douyin.com/111"],
          f"迁移保留了旧历史内容（实际 {[s.get('url') for s in migrated]}）")
    check(migrated and migrated[0]["nickname"] == "旧记录",
          "迁移保留了旧历史的昵称")
    check(Path(legacy_in_internal).read_bytes() == legacy_bytes_before,
          "迁移后旧文件逐字节未被改写（旧位置是只读来源，不是写入目标）")

    # 冻结环境下保存：不得触碰安装目录
    store_c.add_or_update("https://live.douyin.com/222", "新记录")
    check(Path(internal_dir, sh.HISTORY_FILENAME).read_bytes() == legacy_bytes_before,
          "冻结环境下新增记录后，_internal/stream_history.json 仍未被动过")
    check(any(s["url"] == "https://live.douyin.com/222"
              for s in read_json(frozen_write_path)["streams"]),
          "新记录落到了用户数据目录")

    # 新位置已有文件时：迁移必须让位，不能覆盖用户现有数据
    root_c2 = set_data_root("c2-exists")
    new_path = Path(expected_history_path(root_c2))
    new_path.parent.mkdir(parents=True, exist_ok=True)
    with open(new_path, "w", encoding="utf-8") as f:
        json.dump({"streams": [{"url": "https://live.douyin.com/333",
                                "nickname": "已有",
                                "last_used": "2026-06-01T00:00:00"}]},
                  f, ensure_ascii=False)
    store_c2 = sh.StreamHistory()
    check([s["url"] for s in store_c2.get_all()] == ["https://live.douyin.com/333"],
          "新位置已有文件时不迁移、不覆盖用户现有数据")


# ======================================================================
section("D. 目录不存在时自动创建（安装版首次启动）")
# ======================================================================
# 真正的「安装版首次启动」不存在任何旧文件，因此这里把迁移来源指到一个
# 不存在的路径 —— 否则测试结果会取决于**当前仓库里是否恰好留着**一份
# 旧 stream_history.json，那正是"依赖本机状态"的脆弱测试。
root_d = set_data_root("d-fresh")
data_dir_d = os.path.join(root_d, sh.APP_DIR_NAME)
no_legacy = Path(_TMP_ROOT) / "d-no-such-legacy" / sh.HISTORY_FILENAME
check(not no_legacy.exists(), "前置条件：迁移来源不存在（模拟全新安装）")
check(not os.path.isdir(data_dir_d), "前置条件：目标目录尚不存在")
check(not os.path.isdir(root_d), "前置条件：连用户数据根都不存在")

with mock.patch.object(sh, "_legacy_file_path", lambda: no_legacy):
    store_d = sh.StreamHistory()
    check(store_d.get_all() == [], "首次启动：无历史时读出空列表，不抛异常")
    check(not os.path.isdir(data_dir_d), "构造（只读）阶段不创建目录")

    store_d.add_or_update("https://live.douyin.com/123456789", "首次")
    check(os.path.isdir(data_dir_d), "首次保存时自动创建目录（含多级父目录）")
    check(os.path.isfile(str(store_d._file_path)), "首次保存时创建了数据文件")

    reopened = sh.StreamHistory()
    check([s["url"] for s in reopened.get_all()]
          == ["https://live.douyin.com/123456789"],
          "同一进程内重新打开能读回刚写入的记录")

# D2. 开发（源码）环境下的迁移：来源不同，规则相同
#     源码树根的旧文件与冻结环境的 _internal 旧文件走同一个 _migrate_legacy_file，
#     因此这里只换来源路径，验证"旧数据不会因为换了环境就丢"。
legacy_dev = Path(_TMP_ROOT) / "d-dev-legacy" / sh.HISTORY_FILENAME
legacy_dev.parent.mkdir(parents=True, exist_ok=True)
with open(legacy_dev, "w", encoding="utf-8") as f:
    json.dump({"streams": [{"url": "https://live.douyin.com/777",
                            "nickname": "源码环境旧记录",
                            "last_used": "2026-02-02T00:00:00"}]},
              f, ensure_ascii=False)
root_d2 = set_data_root("d-dev")
with mock.patch.object(sh, "_legacy_file_path", lambda: legacy_dev):
    store_d2 = sh.StreamHistory()
    check([s["url"] for s in store_d2.get_all()] == ["https://live.douyin.com/777"],
          f"开发环境下旧文件同样被迁移（实际 "
          f"{[s.get('url') for s in store_d2.get_all()]}）")
    check(store_d2.get_all()[0]["nickname"] == "源码环境旧记录",
          "开发环境迁移保留昵称")
    check(str(store_d2._file_path) == expected_history_path(root_d2),
          "开发环境迁移的**目标**仍是用户数据目录")
    check(not under(store_d2._file_path, PROJECT_ROOT),
          "开发环境迁移不会写回源码树")


# ======================================================================
section("E. 读写失败时的降级行为（绝不抛、绝不让程序起不来）")
# ======================================================================
# E1. 数据文件损坏：按空历史处理，不抛
corrupt_path = os.path.join(_TMP_ROOT, "e-corrupt", sh.HISTORY_FILENAME)
os.makedirs(os.path.dirname(corrupt_path), exist_ok=True)
with open(corrupt_path, "w", encoding="utf-8") as f:
    f.write("{ 这不是合法 JSON")
with quiet():
    store_e1 = sh.StreamHistory(file_path=corrupt_path)
check(store_e1.get_all() == [], "损坏的 JSON 退化为空历史，不抛异常")
check(store_e1.get_nickname("x") == "", "损坏后查询接口仍然可用")

# E2. 目录无法创建：写入静默失败，但内存状态与 API 仍然正常
blocker = os.path.join(_TMP_ROOT, "e-blocker")
os.makedirs(blocker, exist_ok=True)
# 让目标目录的"父级"是一个普通文件 -> mkdir 必然失败
blocking_file = os.path.join(blocker, "not-a-dir")
Path(blocking_file).write_text("x", encoding="utf-8")
root_e2 = os.path.join(blocking_file, "LiveLens")
for key in _ENV_KEYS:
    os.environ[key] = os.path.dirname(root_e2)   # 触发回退路径上的失败
with quiet() as buf_e2:
    store_e2 = sh.StreamHistory(file_path=os.path.join(root_e2, sh.HISTORY_FILENAME))
    store_e2.add_or_update("https://live.douyin.com/555", "写不进去")
    store_e2.set_nickname("https://live.douyin.com/555", "仍然可改")
check(store_e2.get_all()[0]["nickname"] == "仍然可改",
      "磁盘写入失败时内存状态仍然正确（保存失败不吞掉本次操作）")
check("保存历史记录失败" in buf_e2.getvalue(),
      "写入失败会打印既有格式的错误信息（降级可见，不是静默死掉）")
with quiet():
    store_e2.remove("https://live.douyin.com/555")
check(store_e2.get_all() == [], "写入失败时 remove 也照常返回，不抛")

# E3. 构造阶段遇到不可写目录也不能抛（迁移失败必须被吞掉）
with quiet():
    store_e3 = sh.StreamHistory(file_path=os.path.join(root_e2, sh.HISTORY_FILENAME))
check(store_e3.get_all() == [], "不可写位置构造实例不抛异常")


# ======================================================================
section("F. API / JSON 格式 / 历史语义不变（回归锁定）")
# ======================================================================
store_f = sh.StreamHistory(file_path=os.path.join(_TMP_ROOT, "f", "hist.json"))

store_f.add_or_update("https://live.douyin.com/1", "甲")
raw = read_json(store_f._file_path)
check(set(raw.keys()) == {"streams"}, f"顶层键仍只有 streams（实际 {list(raw.keys())}）")
check(set(raw["streams"][0].keys()) == {"url", "nickname", "last_used"},
      f"条目字段仍是 url/nickname/last_used（实际 {list(raw['streams'][0].keys())}）")
check(raw["streams"][0]["url"] == "https://live.douyin.com/1", "url 原样保存")
check(raw["streams"][0]["nickname"] == "甲",
      "中文昵称以 UTF-8 原样保存（ensure_ascii=False，文件可直接阅读）")

# 去重：相同 URL 只保留一条，更新昵称与时间
store_f.add_or_update("https://live.douyin.com/1", "甲改名")
items = store_f.get_all()
check(len(items) == 1, f"相同 URL 不产生重复条目（实际 {len(items)} 条）")
check(items[0]["nickname"] == "甲改名", "重复添加时更新昵称")
check(store_f.get_nickname("https://live.douyin.com/1") == "甲改名", "get_nickname 读到更新值")
check(store_f.get_nickname("https://live.douyin.com/不存在") == "",
      "未知 URL 的昵称为空串")

# LRU：超过上限裁剪最旧的
store_f2 = sh.StreamHistory(max_items=10,
                            file_path=os.path.join(_TMP_ROOT, "f2", "hist.json"))
for i in range(15):
    store_f2.add_or_update(f"https://live.douyin.com/{i}", f"n{i}")
kept = store_f2.get_all()
check(len(kept) == 10, f"超过 max_items 时裁剪到 10（实际 {len(kept)}）")
check("https://live.douyin.com/14" in [s["url"] for s in kept], "保留最近使用的记录")
check("https://live.douyin.com/0" not in [s["url"] for s in kept], "裁掉最旧的记录")

# 排序：按 last_used 降序。用显式且乱序的时间戳落盘后重载来验证契约 ——
# 不能用"连续 add 两条再比顺序"来测：Windows 时钟粒度约 15ms，连续两次
# datetime.now() 几乎必然得到**完全相同**的时间戳，此时两条记录是并列的，
# 降序排序无从区分先后（Python 稳定排序于是保留插入顺序）。那是平台的
# 时间戳精度问题，不是排序逻辑的问题，在 GUI 里历史记录相隔数秒写入，
# 不会碰到并列。
f3_path = os.path.join(_TMP_ROOT, "f3", "hist.json")
os.makedirs(os.path.dirname(f3_path), exist_ok=True)
with open(f3_path, "w", encoding="utf-8") as f:
    json.dump({"streams": [
        {"url": "https://live.douyin.com/old", "nickname": "",
         "last_used": "2026-01-01T00:00:00"},
        {"url": "https://live.douyin.com/new", "nickname": "",
         "last_used": "2026-06-01T00:00:00"},
        {"url": "https://live.douyin.com/mid", "nickname": "",
         "last_used": "2026-03-01T00:00:00"},
    ]}, f, ensure_ascii=False)
store_f3 = sh.StreamHistory(file_path=f3_path)
order = [s["url"] for s in store_f3.get_all()]
check(order == ["https://live.douyin.com/new", "https://live.douyin.com/mid",
                "https://live.douyin.com/old"],
      f"按 last_used 降序返回（实际 {order}）")

# 性质：无论时间戳是否并列，返回值必须单调不增
stamps = [s.get("last_used", "") for s in store_f3.get_all()]
check(stamps == sorted(stamps, reverse=True),
      f"返回顺序按 last_used 单调不增（实际 {stamps}）")

# 并列时间戳（同一毫秒内连续写入）：记录不丢、不重，只是先后未定义
store_f5 = sh.StreamHistory(file_path=os.path.join(_TMP_ROOT, "f5", "hist.json"))
store_f5.add_or_update("https://live.douyin.com/a")
store_f5.add_or_update("https://live.douyin.com/b")
check(sorted(s["url"] for s in store_f5.get_all())
      == ["https://live.douyin.com/a", "https://live.douyin.com/b"],
      "同一时刻写入的两条都保留（时间戳并列不丢记录）")

# set_nickname / remove / clear 的返回值语义
check(store_f5.set_nickname("https://live.douyin.com/a", "改") is True,
      "set_nickname 命中时返回 True")
check(store_f5.get_nickname("https://live.douyin.com/a") == "改",
      "set_nickname 落盘后可读回")
check(store_f5.set_nickname("https://live.douyin.com/zzz", "改") is False,
      "set_nickname 未命中时返回 False")
check(store_f5.remove("https://live.douyin.com/a") is True, "remove 命中时返回 True")
check(store_f5.remove("https://live.douyin.com/a") is False, "remove 未命中时返回 False")
store_f5.clear()
check(store_f5.get_all() == [], "clear 清空全部记录")
check(read_json(store_f5._file_path) == {"streams": []}, "clear 也落盘")

# 裁剪上限本身是构造参数，默认仍是 10
check(sh.StreamHistory(file_path=os.path.join(_TMP_ROOT, "f4", "h.json")).max_items == 10,
      "默认 max_items 仍为 10")


# ======================================================================
# 收尾：还原环境变量（本进程内其它测试可能接着跑）
# ======================================================================
for key, value in _SAVED_ENV.items():
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
shutil.rmtree(_TMP_ROOT, ignore_errors=True)

print(f"\n{'=' * 74}")
print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
print("=" * 74)
sys.exit(1 if _FAILED else 0)
