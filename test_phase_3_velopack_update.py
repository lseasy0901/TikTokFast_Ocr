#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 3 Velopack 自动更新接入 —— 验证脚本

覆盖：
    A. utils/update_source 的源地址单一配置入口
    B. utils/update_service.UpdateService 的行为与异常收口
    C. gui/update_dialog.UpdateDialog 的状态机（真实 velopack.UpdateInfo）
    D. gui/main_window 的接线：不阻塞 GUI、无更新不弹框、异常不崩溃
    E. main.py 的 Velopack 启动引导位置

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_phase_3_velopack_update.py`` 运行，不依赖 pytest。

本脚本**不联网**、不涉及安装、不下真实发布包：Velopack 的 UpdateManager 在
未安装运行时会构造失败，这正是源码/测试环境的真实行为，因此 UpdateService
的大部分路径用注入的假 manager 驱动；UI 与接线用假 service 驱动。
LOCALAPPDATA 指向临时目录，不碰用户真实数据。
"""

import os
import re
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-p3-local-")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-p3-appdata-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import velopack  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.update_dialog import UpdateDialog, format_size  # noqa: E402
from utils import update_source  # noqa: E402
from utils.update_service import UpdateError, UpdateService  # noqa: E402
from utils.version import __version__ as APP_VERSION  # noqa: E402

app = QApplication.instance() or QApplication([])

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


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询 + 抽事件循环，等一个条件成立（跨线程信号靠它送达）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    app.processEvents()
    return bool(predicate())


def fake_info(version: str = "1.1.1", size: int = 12 * 1024 * 1024,
              notes: str = "修复若干问题") -> "velopack.UpdateInfo":
    """构造一份真实的 velopack.UpdateInfo（纯数据类，不需要安装环境）。"""
    asset = velopack.VelopackAsset(
        "LiveLensDesktop", version, "Full",
        f"LiveLensDesktop-{version}-full.nupkg", "sha1", "sha256", size,
        notes, f"<p>{notes}</p>",
    )
    return velopack.UpdateInfo(asset, [], False)


class FakeManager:
    """假 UpdateManager：按需返回/抛出，用来驱动 UpdateService 的各条分支。"""

    def __init__(self, current="1.1.0", check_result=None, check_error=None,
                 download_error=None):
        self._current = current
        self._check_result = check_result
        self._check_error = check_error
        self._download_error = download_error
        self.downloaded = []
        self.applied = []

    def get_current_version(self):
        return self._current

    def check_for_updates(self):
        if self._check_error is not None:
            raise self._check_error
        return self._check_result

    def download_updates(self, info, progress=None):
        if self._download_error is not None:
            raise self._download_error
        if progress is not None:
            progress(40)
            progress(100)
        self.downloaded.append(info)

    def apply_updates_and_restart(self, info):
        self.applied.append(info)
        raise SystemExit(0)      # 模拟 Velopack 结束本进程（测试里用 SystemExit 代替真退出）


class FakeService:
    """假 UpdateService：只喂 UI/接线，不碰 Velopack 与网络。"""

    def __init__(self, info=None, download_error=None, delay=0.0):
        self.info = info
        self.download_error = download_error
        self.delay = delay
        self.check_thread = None
        self.downloaded = 0

    def current_version(self):
        return "1.1.0"

    def is_available(self):
        return True

    def check(self):
        self.check_thread = threading.current_thread().name
        if self.delay:
            time.sleep(self.delay)
        return self.info

    def download(self, info, progress=None):
        if self.download_error is not None:
            raise UpdateError(self.download_error)
        if progress is not None:
            progress(35)
            progress(100)
        self.downloaded += 1

    def apply_and_restart(self, info):
        self.info = info


# ======================================================================
# A. 更新源配置
# ======================================================================
def test_source_config() -> None:
    section("A. 更新源单一配置入口")

    default = update_source.DEFAULT_UPDATE_SOURCE
    check(default.startswith("https://"), f"默认源是 https: {default}")
    check("feijipan" not in default.lower(), "默认源不依赖网盘域名")

    saved = os.environ.get(update_source.ENV_OVERRIDE)
    try:
        os.environ.pop(update_source.ENV_OVERRIDE, None)
        check(update_source.get_update_source() == default,
              "无环境变量时取默认源")
        os.environ[update_source.ENV_OVERRIDE] = "http://127.0.0.1:9/test/"
        check(update_source.get_update_source() == "http://127.0.0.1:9/test",
              "环境变量可覆盖（末尾斜杠被规范化掉）")
    finally:
        if saved is None:
            os.environ.pop(update_source.ENV_OVERRIDE, None)
        else:
            os.environ[update_source.ENV_OVERRIDE] = saved

    # 单一入口：更新源 URL 不得在别处再出现一份
    for rel in ("gui/update_dialog.py", "gui/main_window.py", "main.py",
                "scripts/velopack_release.py"):
        with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
            body = f.read()
        check(default not in body, f"{rel} 未硬编码源地址")


# ======================================================================
# B. UpdateService
# ======================================================================
def test_update_service() -> None:
    section("B. UpdateService 行为")

    # 未安装运行（源码 / 旧 ZIP 解压）：这是本机的真实状态
    svc = UpdateService("https://example.invalid/updates/stable")
    check(svc.is_available() is False, "未安装运行 → is_available() False")
    check(svc.check() is None, "未安装运行 → check() 返回 None（不抛异常）")
    check(svc.current_version() == APP_VERSION,
          f"未安装运行 → 版本回落 utils.version（{APP_VERSION}）")
    try:
        svc.download(fake_info())
        check(False, "未安装运行 → download 应抛 UpdateError")
    except UpdateError as e:
        check("无法自动更新" in str(e), "未安装运行 → download 抛明确的 UpdateError")
    try:
        svc.apply_and_restart(fake_info())
        check(False, "未安装运行 → apply 应抛 UpdateError")
    except UpdateError:
        check(True, "未安装运行 → apply 抛明确的 UpdateError")

    # 有 manager 时：正常返回 / 异常收口
    ok = UpdateService("https://example.invalid/updates/stable")
    ok._manager = FakeManager(current="1.1.0", check_result=fake_info())
    check(ok.is_available() is True, "有 manager → is_available() True")
    check(ok.current_version() == "1.1.0", "版本优先取 Velopack 的当前安装版本")
    check(ok.check() is not None and ok.check().TargetFullRelease.Version == "1.1.1",
          "check() 直接返回 Velopack 的 UpdateInfo（不再自己比版本）")

    no_update = UpdateService("https://example.invalid/updates/stable")
    no_update._manager = FakeManager(check_result=None)
    check(no_update.check() is None, "已是最新 → check() 返回 None")

    boom = UpdateService("https://example.invalid/updates/stable")
    boom._manager = FakeManager(check_error=RuntimeError("connection reset"))
    check(boom.check() is None, "检查抛异常 → check() 吞掉并返回 None（不崩溃）")

    ver_boom = UpdateService("https://example.invalid/updates/stable")
    ver_boom._manager = FakeManager()
    ver_boom._manager.get_current_version = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    check(ver_boom.current_version() == APP_VERSION,
          "取版本抛异常 → 回落 utils.version")

    # 下载 / 应用：正常路径 + 异常收口
    dl = UpdateService("https://example.invalid/updates/stable")
    mgr = FakeManager()
    dl._manager = mgr
    seen = []
    dl.download(fake_info(), progress=seen.append)
    check(mgr.downloaded and seen == [40, 100],
          "download 把 Velopack 的百分比回调原样透出（不伪造进度）")

    dl_bad = UpdateService("https://example.invalid/updates/stable")
    dl_bad._manager = FakeManager(download_error=RuntimeError("disk full"))
    try:
        dl_bad.download(fake_info())
        check(False, "下载异常 → 应抛 UpdateError")
    except UpdateError as e:
        check("disk full" in str(e), "下载异常 → 包成面向用户的 UpdateError")

    # 真源不可用时不崩溃（真实网络失败路径：连接被拒绝）
    dead = UpdateService("http://127.0.0.1:9/updates/stable")
    dead._manager = FakeManager(check_error=ConnectionError("refused"))
    check(dead.check() is None, "源不可用 → check() 返回 None，程序照常运行")


# ======================================================================
# C. UpdateDialog 状态机
# ======================================================================
def test_dialog() -> None:
    section("C. UpdateDialog 状态机（薄 UI）")

    info = fake_info(size=12 * 1024 * 1024, notes="修复若干问题")
    svc = FakeService(info=info)
    dialog = UpdateDialog(svc, info)

    check(dialog.version == "1.1.1", "对话框暴露目标版本号")
    check(dialog._stack.currentIndex() == 0, "初始停在更新说明页")
    check(not dialog._start_btn.isHidden() and dialog._background_btn.isHidden(),
          "初始态：有「立即更新」，没有「后台继续」")

    ready = []
    dialog.update_ready.connect(ready.append)
    applied = []
    dialog.apply_requested.connect(applied.append)

    dialog._start_download()
    check(dialog.is_downloading() or dialog._worker is not None, "点击后进入下载态")
    check(wait_until(lambda: svc.downloaded == 1 and not dialog.is_downloading()),
          "后台线程完成下载")
    check(wait_until(lambda: dialog._stack.currentIndex() == 2),
          "完成后切到结果页")
    check(ready and ready[0].TargetFullRelease.Version == "1.1.1",
          "发出 update_ready 信号（带 UpdateInfo）")
    check("下载完成" in dialog._result_title.text(), "结果页提示「更新已下载完成」")
    check("重启" in dialog._result_detail.text(), "结果页提示需要重启生效")

    dialog._request_apply()
    check(applied and applied[0].TargetFullRelease.Version == "1.1.1",
          "「立即重启并更新」发出 apply_requested（应用由主窗口执行）")

    # 失败态
    bad = FakeService(info=info, download_error="网络中断")
    dialog2 = UpdateDialog(bad, info)
    dialog2._start_download()
    check(wait_until(lambda: dialog2._stack.currentIndex() == 2 and dialog2._worker is None),
          "下载失败后回到结果页")
    check("失败" in dialog2._result_title.text(), "结果页显示下载失败")
    check("网络中断" in dialog2._result_detail.text(), "失败原因如实展示")

    # 进度：真百分比，不做假平滑
    dialog3 = UpdateDialog(FakeService(info=info), info)
    dialog3._on_progress(42)
    check(dialog3._progress_bar.value() == 42, "进度条直接用 Velopack 的百分比")
    check("42%" in dialog3._progress_label.text(), "文案带真实百分比")
    check(format_size(12 * 1024 * 1024) == "12.0 MB", "大小格式化正常")

    # 未提供说明 / 大小未知时不假装有进度总量
    bare = velopack.UpdateInfo(
        velopack.VelopackAsset("LiveLensDesktop", "1.1.1", "Full", "f.nupkg",
                               "a", "b", 0, "", ""),
        [], False,
    )
    dialog4 = UpdateDialog(FakeService(info=bare), bare)
    dialog4._on_progress(7)
    check(dialog4._progress_label.text() == "正在下载 7%",
          "清单没给大小时只显示百分比，不编总量")

    dialog5 = UpdateDialog(FakeService(info=bare), bare)
    check(dialog5._progress_note is not None, "进度页说明由 Velopack 校验安装")
    for d in (dialog, dialog2, dialog3, dialog4, dialog5):
        d.close()


# ======================================================================
# D. MainWindow 接线
# ======================================================================
def test_main_window_wiring() -> None:
    section("D. MainWindow 接线（不阻塞 GUI / 无更新不弹框 / 异常不崩溃）")

    import gui.main_window as mw

    recorded = []

    class RecorderDialog(QObject):
        """替身：记录被喂进来的更新信息，不弹真实模态框（否则测试会卡住）。"""

        apply_requested = Signal(object)

        def __init__(self, service, info, parent=None):
            super().__init__(parent)
            recorded.append(info)

        def exec(self):
            return 0

    real_dialog = mw.UpdateDialog
    mw.UpdateDialog = RecorderDialog

    try:
        window = mw.MainWindow()
        window.show()
        wait_until(lambda: not (window._license_load_worker
                                and window._license_load_worker.is_running()), timeout=8.0)

        check(isinstance(window._update_service, UpdateService),
              "MainWindow 持有 UpdateService（旧 UpdateClient 已下线）")
        check(not hasattr(window, "_update_client"),
              "MainWindow 不再持有旧 UpdateClient")
        check(not hasattr(window, "_ready_update"),
              "旧的 download_ready 落点已移除")

        # 慢检查：调用必须立刻返回，检查在后台线程里跑
        slow = FakeService(info=fake_info(), delay=1.2)
        window._update_service = slow
        started = time.monotonic()
        window._on_update_timer_timeout()
        elapsed = time.monotonic() - started
        check(elapsed < 0.5, f"检查不阻塞 GUI 线程（{elapsed:.3f}s 返回）")
        check(window._update_worker is not None and window._update_worker.is_running(),
              "检查在后台线程里进行")
        check(wait_until(lambda: recorded, timeout=8.0), "检查完成后弹出更新对话框")
        check(slow.check_thread != "MainThread",
              f"check 在非 GUI 线程执行（{slow.check_thread}）")
        check(recorded and recorded[0].TargetFullRelease.Version == "1.1.1",
              "对话框收到正确的更新信息")

        # 无更新：不弹框
        recorded.clear()
        window._update_service = FakeService(info=None)
        window._on_update_timer_timeout()
        wait_until(lambda: not window._update_worker.is_running(), timeout=5.0)
        for _ in range(50):
            app.processEvents()
            time.sleep(0.01)
        check(recorded == [], "没有更新时不弹任何对话框")

        # 异常：LicenseWorker 的兜底对象不是有效更新信息，必须被忽略
        window._on_update_check_done(None)
        window._on_update_check_done("not-an-update")
        window._on_update_check_done(42)
        check(recorded == [], "异常/无效的检查结果被安全忽略，不崩溃")

        window.close()
    finally:
        mw.UpdateDialog = real_dialog


def test_apply_stops_session() -> None:
    section("D2. 应用更新前必须停流收尾")

    import gui.main_window as mw

    window = mw.MainWindow()
    disconnected = []
    window._disconnect = lambda *a, **k: disconnected.append(True)

    class BoomService:
        def current_version(self):
            return "1.1.0"

        def apply_and_restart(self, info):
            raise UpdateError("更新未能启动")

    window._update_service = BoomService()
    shown = []
    real_warning = mw.QMessageBox.warning
    mw.QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a))
    try:
        window._on_update_apply_requested(fake_info())
    finally:
        mw.QMessageBox.warning = real_warning

    check(disconnected == [True], "应用更新前先停流（避免留下孤儿 ffmpeg 进程）")
    check(shown, "Velopack 未接管时向用户报错，而不是静默什么都不发生")
    window.close()


# ======================================================================
# E. main.py 启动引导
# ======================================================================
def test_bootstrap() -> None:
    section("E. Velopack 启动引导")

    import main as entry

    check(callable(entry._velopack_bootstrap), "存在独立的启动引导函数")
    # 无 --veloapp-* 参数时：不结束进程、不抛异常、正常返回
    entry._velopack_bootstrap()
    check(True, "正常启动路径下引导返回，不接管进程")

    # 位置：必须是 main() 里最早发生的事（早于日志、Qt、业务导入）
    with open(os.path.join(PROJECT_ROOT, "main.py"), "r", encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("def main() -> int:"):]
    boot = body.index("_velopack_bootstrap()")
    check(boot < body.index("logging.basicConfig"), "引导早于日志初始化")
    check(boot < body.index("QApplication"), "引导早于 QApplication 构造")
    check(boot < body.index("MainWindow()"), "引导早于主窗口构造")


# ======================================================================
# F. 旧自研更新链已清理（Phase 3.5）
# ======================================================================
def test_legacy_chain_removed() -> None:
    section("F. 旧自研更新链已清理")

    import importlib.util

    for name in ("utils.update_client", "utils.update_download"):
        path = os.path.join(PROJECT_ROOT, *name.split(".")) + ".py"
        check(not os.path.exists(path), f"{path} 已删除")
        check(importlib.util.find_spec(name) is None, f"{name} 不可导入")

    # 生产入口与主窗口都不再 import 旧链。断言 import 语句本身，
    # 而不是「文件里不许出现这两个名字」—— 注释里说明「谁被删了」是好事。
    pattern = re.compile(r"^\s*(from|import)\s+utils\.update_(client|download)\b", re.M)
    for rel in ("main.py", "gui/main_window.py", "gui/update_dialog.py"):
        with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
            body = f.read()
        check(pattern.search(body) is None, f"{rel} 没有 import 旧更新链")


# ======================================================================
# 入口
# ======================================================================
def main() -> int:
    print("=" * 68)
    print("Phase 3 Velopack 自动更新接入 —— 验证")
    print("=" * 68)

    test_source_config()
    test_update_service()
    test_dialog()
    test_main_window_wiring()
    test_apply_stops_session()
    test_bootstrap()
    test_legacy_chain_removed()

    print("\n" + "=" * 68)
    print(f"PASS {_PASSED} / FAIL {_FAILED}")
    print("=" * 68)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
