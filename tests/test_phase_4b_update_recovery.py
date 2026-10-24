#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 4B 启动失败自动恢复 —— 验证脚本

覆盖：
    A. utils/update_state 的读写（缺失 / 损坏 / 原子写 / 字段容错）
    B. 状态机全部转换（resolve_startup，纯函数）
    C. 健康标记的硬约束（mark_healthy_if_booting 不得覆盖 applying_new）
    D. blocked_version 在 UpdateService.check() 里的生效与解除
    E. downgrade_to 的资产选取与调用链（HTTP 用桩替换，不联网）
    F. main.py 的接线位置（状态机早于 Qt 导入）

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_phase_4b_update_recovery.py`` 运行，不依赖 pytest。

本脚本**不联网、不安装、不下载**。LOCALAPPDATA 指向临时目录，因此状态文件
落在临时目录里，**不碰真实用户数据**。
"""

import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 必须在 import utils.update_state 之前改掉：数据目录是按环境变量解析的
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-p4b-local-")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-p4b-appdata-")

import velopack  # noqa: E402

from utils import update_service as us  # noqa: E402
from utils import update_state  # noqa: E402

# ======================================================================
# 测试脚手架
# ======================================================================
_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {message}", flush=True)
    else:
        _FAILED += 1
        print(f"    [FAIL] {message}", flush=True)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def state_file() -> str:
    return update_state.state_path()


def reset_state() -> None:
    try:
        os.unlink(state_file())
    except FileNotFoundError:
        pass


def write_raw(text: str) -> None:
    with open(state_file(), "w", encoding="utf-8") as f:
        f.write(text)


class FakeManager:
    """假 UpdateManager：记录调用，用来观察 downgrade 的调用链。"""

    def __init__(self, check_result=None):
        self._check_result = check_result
        self.downloaded = []
        self.applied = []

    def check_for_updates(self):
        return self._check_result

    def download_updates(self, info, progress=None):
        if progress is not None:
            progress(50)
        self.downloaded.append(info)

    def apply_updates_and_restart(self, info):
        self.applied.append(info)
        raise SystemExit(0)      # 模拟 Velopack 结束本进程


def info_for(version: str):
    asset = velopack.VelopackAsset(
        "LiveLensDesktop", version, "Full", f"LiveLensDesktop-{version}-stable-full.nupkg",
        "sha1", "sha256", 100, "", "",
    )
    return velopack.UpdateInfo(asset, [], False)


# ======================================================================
# A. 状态文件读写
# ======================================================================
def test_state_io() -> None:
    section("A. 状态文件读写")

    reset_state()
    st = update_state.read_state()
    check(st.state == update_state.STATE_NORMAL, "文件缺失 → 默认 normal")
    check(st.blocked_version is None and st.attempts == 0, "缺失时其余字段为默认值")
    check(state_file().endswith(os.path.join("LiveLens", "update_state.json")),
          "状态文件放在 LiveLens 数据目录下")
    check("current" not in state_file().lower().split(os.sep),
          "状态文件在 Velopack current/ 之外")

    original = update_state.UpdateState(
        state=update_state.STATE_BOOTING_NEW, from_version="1.1.0",
        to_version="1.1.1", blocked_version=None, attempts=0,
    )
    update_state.write_state(original)
    check(update_state.read_state() == original, "写入后可原样读回")

    write_raw("{ this is not json")
    check(update_state.read_state().state == update_state.STATE_NORMAL,
          "损坏 JSON → 安全降级为 normal")

    write_raw(json.dumps({"state": "weird_state", "to": "1.1.1"}))
    check(update_state.read_state().state == update_state.STATE_NORMAL,
          "非法 state 值 → 安全降级为 normal")

    write_raw(json.dumps({"state": "booting_new", "to": "1.1.1", "attempts": "x"}))
    st = update_state.read_state()
    check(st.state == update_state.STATE_BOOTING_NEW and st.attempts == 0,
          "非法 attempts → 归零，其余字段仍可用")

    write_raw(json.dumps({"state": "booting_new", "to": "", "from": None}))
    st = update_state.read_state()
    check(st.to_version is None and st.from_version is None,
          "空字符串/None 版本号 → None 而不是空串")

    # 原子写：临时文件不残留
    leftovers = [n for n in os.listdir(os.path.dirname(state_file()))
                 if n.startswith(".update_state-")]
    check(leftovers == [], "原子写不残留临时文件")
    reset_state()


# ======================================================================
# B. 状态机转换
# ======================================================================
def test_transitions() -> None:
    section("B. 状态机转换")

    S = update_state.UpdateState
    R = update_state.resolve_startup

    # 正常更新：A → B，B 第一次启动
    d = R(S(update_state.STATE_APPLYING_NEW, "1.1.0", "1.1.1"), "1.1.1")
    check(d.action == update_state.ACTION_BOOT, "applying_new + current==to → 启动健康窗口")
    check(d.state == S(update_state.STATE_BOOTING_NEW, "1.1.0", "1.1.1", None, 0),
          "applying_new → booting_new（字段与设计一致）")

    # 更新实际没落地
    d = R(S(update_state.STATE_APPLYING_NEW, "1.1.0", "1.1.1"), "1.1.0")
    check(d.action == update_state.ACTION_NORMAL and d.state.state == update_state.STATE_NORMAL,
          "applying_new 但版本没变 → normal（不误判为启动失败）")
    check(d.state.blocked_version is None, "未落地时不封锁任何版本")

    # 启动失败 → 回滚
    d = R(S(update_state.STATE_BOOTING_NEW, "1.1.0", "1.1.1", None, 0), "1.1.1")
    check(d.action == update_state.ACTION_RECOVER, "booting_new 未健康 + attempts=0 → 回滚")
    check(d.state == S(update_state.STATE_RECOVERY, None, "1.1.0", "1.1.1", 1),
          "进入 recovery{target: A, blocked: B, attempts: 1}")

    # 陈旧状态
    d = R(S(update_state.STATE_BOOTING_NEW, "1.1.0", "1.1.1", None, 0), "1.1.2")
    check(d.action == update_state.ACTION_NORMAL, "booting_new 与当前版本不符 → 当陈旧状态重置")

    # 回滚目标自己的健康窗口
    d = R(S(update_state.STATE_RECOVERY, None, "1.1.0", "1.1.1", 1), "1.1.0")
    check(d.action == update_state.ACTION_BOOT, "回滚版 A 启动 → 也要过一次健康窗口")
    check(d.state == S(update_state.STATE_BOOTING_NEW, None, "1.1.0", "1.1.1", 1),
          "回滚版转 booting_new 且 attempts 保持 1")

    # 回滚版再次启动失败 → 不再回滚
    d = R(S(update_state.STATE_BOOTING_NEW, None, "1.1.0", "1.1.1", 1), "1.1.0")
    check(d.action == update_state.ACTION_FAILED, "回滚版也起不来 → 停止自动动作（防死循环）")
    check(bool(d.notice), "给出面向用户的提示文案")
    check("重新安装" in (d.notice or "") or "手动" in (d.notice or ""),
          "提示里给出可执行的动作")

    # 回滚没能落地
    d = R(S(update_state.STATE_RECOVERY, None, "1.1.0", "1.1.1", 1), "1.1.1")
    check(d.action == update_state.ACTION_FAILED, "回滚未完成（无网络/源缺版本）→ 只提示不重试")
    check("1.1.1" in (d.notice or ""), "提示里说明当前仍在哪个版本")

    # healthy / normal
    d = R(S(update_state.STATE_HEALTHY, None, "1.1.1", None, 0), "1.1.1")
    check(d.action == update_state.ACTION_NORMAL and d.state.state == update_state.STATE_NORMAL,
          "healthy → normal，照常启动")
    d = R(S(update_state.STATE_NORMAL, None, None, None, 0), "1.1.0")
    check(d.action == update_state.ACTION_NORMAL and d.state.to_version == "1.1.0",
          "normal → normal，并刷新当前版本")
    d = R(S(update_state.STATE_HEALTHY, None, "1.1.0", "1.1.1", 1), "1.1.2")
    check(d.state.blocked_version == "1.1.1", "normal 化时保留 blocked_version")


# ======================================================================
# C. 健康标记的硬约束
# ======================================================================
def test_mark_healthy() -> None:
    section("C. 健康标记（不得覆盖 applying_new）")

    reset_state()
    update_state.write_state(update_state.mark_applying("1.1.0", "1.1.1"))
    check(not update_state.mark_healthy_if_booting("1.1.1"),
          "state=applying_new 时拒绝写 healthy")
    check(update_state.read_state().state == update_state.STATE_APPLYING_NEW,
          "applying_new 原样保留（否则启动失败就再也恢复不了）")

    update_state.write_state(
        update_state.UpdateState(update_state.STATE_BOOTING_NEW, "1.1.0", "1.1.1", None, 0))
    check(not update_state.mark_healthy_if_booting("1.1.0"),
          "booting_new 但版本不是 to → 拒绝写")
    check(update_state.read_state().state == update_state.STATE_BOOTING_NEW, "状态未被改动")

    check(update_state.mark_healthy_if_booting("1.1.1"), "booting_new + 版本匹配 → 写 healthy")
    st = update_state.read_state()
    check(st.state == update_state.STATE_HEALTHY and st.to_version == "1.1.1",
          "healthy 记录当前版本")
    check(st.from_version is None, "healthy 不再保留 from（没有待完成的迁移）")

    # 重复写是幂等的空操作
    check(not update_state.mark_healthy_if_booting("1.1.1"), "已 healthy 时不再重复写")

    reset_state()


# ======================================================================
# D. blocked_version
# ======================================================================
def test_blocked_version() -> None:
    section("D. blocked_version 生效与解除")

    reset_state()
    svc = us.UpdateService("https://example.invalid/updates/stable")

    mgr = FakeManager(check_result=info_for("1.1.1"))
    svc._manager = mgr
    check(svc.check() is not None, "未封锁时正常返回更新信息")

    update_state.write_state(
        us.update_state.UpdateState(update_state.STATE_HEALTHY, None, "1.1.0", "1.1.1", 1))
    check(svc.check() is None, "最新版 == blocked_version → 视为无更新（不重复回滚/更新）")
    check(update_state.read_state().blocked_version == "1.1.1", "跳过时封锁保持不变")

    # 出现更高版本 → 解除封锁
    svc._manager = FakeManager(check_result=info_for("1.1.2"))
    info = svc.check()
    check(info is not None and info.TargetFullRelease.Version == "1.1.2",
          "更高版本出现时允许更新")
    check(update_state.read_state().blocked_version is None, "同时解除封锁")
    reset_state()


# ======================================================================
# E. downgrade_to
# ======================================================================
FEED = {
    "Assets": [
        {"PackageId": "LiveLensDesktop", "Version": "1.1.1", "Type": "Delta",
         "FileName": "LiveLensDesktop-1.1.1-stable-delta.nupkg", "SHA1": "d1",
         "SHA256": "d2", "Size": 10},
        {"PackageId": "LiveLensDesktop", "Version": "1.1.1", "Type": "Full",
         "FileName": "LiveLensDesktop-1.1.1-stable-full.nupkg", "SHA1": "f1",
         "SHA256": "f2", "Size": 100},
        {"PackageId": "LiveLensDesktop", "Version": "1.1.0", "Type": "Full",
         "FileName": "LiveLensDesktop-1.1.0-stable-full.nupkg", "SHA1": "a1",
         "SHA256": "a2", "Size": 90},
    ]
}


def test_downgrade() -> None:
    section("E. downgrade_to（HTTP 用桩，不联网）")

    real_fetch = us._fetch_bytes
    svc = us.UpdateService("https://example.invalid/updates/stable")

    try:
        us._fetch_bytes = lambda url: json.dumps(FEED).encode("utf-8")
        asset = svc.fetch_full_asset("1.1.0")
        check(asset is not None, "能从清单里取到旧版本资产")
        check(asset.Version == "1.1.0" and asset.Type == "Full", "取到的是该版本的全量包")
        check(asset.FileName == "LiveLensDesktop-1.1.0-stable-full.nupkg", "FileName 来自清单")
        check(asset.SHA256 == "a2" and asset.Size == 90, "校验信息来自清单（不伪造）")

        check(svc.fetch_full_asset("1.1.1").Type == "Full",
              "同版本有 Delta 与 Full 时只取 Full")
        check(svc.fetch_full_asset("9.9.9") is None, "清单里没有该版本 → None")

        us._fetch_bytes = lambda url: json.dumps({"foo": 1}).encode("utf-8")
        try:
            svc.fetch_full_asset("1.1.0")
            check(False, "清单结构异常应抛 UpdateError")
        except us.UpdateError:
            check(True, "清单结构异常 → 明确的 UpdateError")

        def boom(url):
            raise OSError("network down")

        us._fetch_bytes = boom
        try:
            svc.fetch_full_asset("1.1.0")
            check(False, "网络失败应抛 UpdateError")
        except us.UpdateError as e:
            check("无法访问更新源" in str(e), "网络失败 → 面向用户的 UpdateError")

        # 完整调用链：桩 manager 观察真实传入 Velopack 的参数
        us._fetch_bytes = lambda url: json.dumps(FEED).encode("utf-8")
        mgr = FakeManager()
        svc._downgrade_mgr = mgr
        progress = []
        try:
            svc.downgrade_to("1.1.0", progress=progress.append)
            check(False, "apply 应结束进程（测试里以 SystemExit 模拟）")
        except SystemExit:
            pass
        check(len(mgr.downloaded) == 1 and len(mgr.applied) == 1, "下载与应用各一次")
        info = mgr.applied[0]
        check(info.IsDowngrade is True, "构造的 UpdateInfo 标记为 downgrade")
        check(info.DeltasToTarget == [], "不回退增量包（强制全量）")
        check(info.TargetFullRelease.Version == "1.1.0", "目标是旧版本")
        check(progress == [50], "下载进度回调透传")

        # 源上没有旧版本 → 明确失败，不静默
        try:
            svc.downgrade_to("9.9.9")
            check(False, "源缺版本应抛 UpdateError")
        except us.UpdateError as e:
            check("无法自动恢复" in str(e), "源缺该版本 → 明确报错（不猜、不改用别的包）")

        # 不可用环境（未安装运行）也不崩
        svc2 = us.UpdateService("https://example.invalid/updates/stable")
        try:
            svc2.downgrade_to("1.1.0")
            check(False, "非 Velopack 安装版应抛 UpdateError")
        except us.UpdateError:
            check(True, "非 Velopack 安装环境 → UpdateError，不抛裸异常")
    finally:
        us._fetch_bytes = real_fetch
        reset_state()


# ======================================================================
# F. main.py 接线位置
# ======================================================================
def test_wiring() -> None:
    section("F. main.py 接线")

    import main as entry

    for name in ("_advance_update_state", "_start_recovery"):
        check(callable(getattr(entry, name, None)), f"存在 {name}")

    with open(os.path.join(PROJECT_ROOT, "main.py"), "r", encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("def main() -> int:"):]
    check(body.index("_advance_update_state()") < body.index("from PySide6.QtWidgets import QApplication"),
          "状态机早于 Qt 导入（新版起不来时也要能推进状态）")
    check(body.index("_advance_update_state()") < body.index("MainWindow()"),
          "状态机早于主窗口构造")
    check("HEALTH_WINDOW_MS" in body, "接线了健康窗口")
    check("aboutToQuit" in body, "接线了退出时的尽力补写")
    check("ACTION_RECOVER" in body, "接线了回滚分支")

    # 状态机函数本身不 import Qt（必须能在 Qt 起不来时运行）
    with open(os.path.join(PROJECT_ROOT, "main.py"), "r", encoding="utf-8") as f:
        src = f.read()
    fn = src[src.index("def _advance_update_state"):src.index("def _start_recovery")]
    check("import PySide6" not in fn and "from PySide6" not in fn,
          "状态机函数不 import PySide6（静态检查，注释里提到不算）")


# ======================================================================
# 入口
# ======================================================================
def main() -> int:
    print("=" * 68)
    print("Phase 4B 启动失败自动恢复 —— 验证")
    print("=" * 68)

    test_state_io()
    test_transitions()
    test_mark_healthy()
    test_blocked_version()
    test_downgrade()
    test_wiring()

    print("\n" + "=" * 68)
    print(f"PASS {_PASSED} / FAIL {_FAILED}")
    print("=" * 68)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
