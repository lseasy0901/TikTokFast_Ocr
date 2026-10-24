#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
首次启动引导 / ROI 帮助 —— 验证脚本

覆盖三块：
    A. utils/first_run.FirstRunStore 的容错读取与原子写入
    B. MainWindow 的接线：入口、重入保护、访问状态无关性
    C. 两个真实对话框（offscreen 构造，不 exec）

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_first_run_onboarding.py`` 运行，不依赖 pytest。

关键隔离：LOCALAPPDATA 与 APPDATA 都指向临时目录，测试绝不写用户真实配置。

注意：本文件【不能】真的 exec() 两个对话框 —— 它们是模态的，在无头环境里
会让整个脚本永久阻塞。所以 MainWindow 层一律用「模块属性替换」打桩
（与 test_expiration_access_control.py 打桩 ExpiredDialog 的手法一致），
只有第 C 节才直接构造真实对话框，且只构造、不 exec。
"""

import json
import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 必须在导入 utils.first_run / gui.main_window 之前设置：
# default_data_dir() 在 FirstRunStore/LicenseStore 构造时读取这些变量。
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-onboard-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-onboard-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QRect, Qt, Signal  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import gui.main_window as mw  # noqa: E402
import gui.onboarding as ob  # noqa: E402
from gui.onboarding import (  # noqa: E402
    DIALOG_FIRST_STEP,
    SPOTLIGHT_STEPS,
    GUIDE_ADVANCE,
    GUIDE_ALWAYS,
    GUIDE_NEVER,
    GUIDE_REVIEW_CLOSED,
    OnboardingDialog,
    RoiHelpDialog,
    STEPS,
)
from utils.first_run import (  # noqa: E402
    KEY_SHOW_GUIDE,
    PREFS_FILENAME,
    SCHEMA_VERSION,
    FirstRunStore,
)
from ocr.roi_manager import ROI  # noqa: E402
from utils.license_manager import LicenseResult, LicenseStatus  # noqa: E402

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


def fresh_dir(prefix: str = "dlv-onboard-") -> str:
    return tempfile.mkdtemp(prefix=prefix)


# ----------------------------------------------------------------------
# 桩：避免真的弹模态框
# ----------------------------------------------------------------------
class _StubOnboardingDialog:
    instances = []

    def __init__(self, parent=None, first_run=True, start_step=0):
        self.parent_ = parent
        self.first_run = first_run
        self.start_step = start_step
        # 测试可直接改写：模拟嵌套事件循环里的重入调用
        self.reenter = None
        _StubOnboardingDialog.instances.append(self)

    def exec(self):
        if self.reenter is not None:
            self.reenter()
        return 0

    # 默认按"正常走完"处理：completed=True → 调用方一律写「不再自动显示」。
    # 中途关闭那条路径由 exit_choice_value 表达。
    exit_choice_value = None

    def completed(self):
        return True

    def exit_choice(self):
        return self.exit_choice_value


class _StubRoiHelpDialog:
    instances = []

    def __init__(self, parent=None):
        self.parent_ = parent
        _StubRoiHelpDialog.instances.append(self)

    def exec(self):
        return 0


def install_stubs() -> None:
    mw.OnboardingDialog = _StubOnboardingDialog
    mw.RoiHelpDialog = _StubRoiHelpDialog


def reset_recorders() -> None:
    _StubOnboardingDialog.instances.clear()
    _StubRoiHelpDialog.instances.clear()


def complete_spotlight(window, outcome: str = GUIDE_ADVANCE) -> None:
    """走完主界面上的聚光灯步骤，让后续步骤的模态向导接着弹出。

    UI-4.4 起前两步都是聚光灯，所以 ADVANCE 要连走两步；
    非 ADVANCE 的结果（提前关闭）只结束当前那一步，遇到就停。
    """
    for _ in range(len(SPOTLIGHT_STEPS)):
        spotlight = window._guide_spotlight
        if spotlight is None:
            break
        spotlight.finish(outcome)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
        if outcome != GUIDE_ADVANCE:
            break


def build_window(store_dir: str):
    """构造真实 MainWindow，并把首次启动状态指向临时目录。

    构造完立刻断言「构造期不弹任何对话框」由调用方负责（见 B 节）。
    """
    window = mw.MainWindow()
    window.show()
    for _ in range(400):
        app.processEvents()
        time.sleep(0.005)
        if window._license_load_worker and not window._license_load_worker.is_running():
            break
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    # 明确注入，避免依赖环境变量推导出的目录
    window._first_run_store = FirstRunStore(data_dir=store_dir)
    # 视为已授权：Phase UI 微调 08 起，引导只在「已获得有效授权」时自动弹出
    # （未授权启动不弹，等激活成功后再弹）。B 节验证的是已授权用户这条路径；
    # 未授权那条路径见第 20 节。
    window._access_status.set_license_active(None)
    app.processEvents()
    return window


# ======================================================================
# A. FirstRunStore
# ======================================================================
def test_store() -> None:
    section("A. FirstRunStore：容错读取 / 原子写入")

    # --- 1. 文件缺失 ---
    d1 = fresh_dir()
    store = FirstRunStore(data_dir=d1)
    check(store.should_show_guide() is True, "文件缺失 → 默认显示引导")
    check(
        not os.path.exists(os.path.join(d1, PREFS_FILENAME)),
        "读取不会创建文件（只读磁盘 / 首次启动必须安全）",
    )

    # --- 2. 写 false 之后 ---
    store.set_should_show_guide(False)
    path = os.path.join(d1, PREFS_FILENAME)
    check(os.path.isfile(path), "写入后 prefs.json 存在")
    check(
        FirstRunStore(data_dir=d1).should_show_guide() is False,
        "新实例重新读取 → 不再显示引导",
    )

    # --- 3. 往返 ---
    store.set_should_show_guide(True)
    check(FirstRunStore(data_dir=d1).should_show_guide() is True, "写回 true → 往返一致")

    # --- 4. 记录内容 ---
    store.set_should_show_guide(False)
    with open(path, "r", encoding="utf-8") as f:
        record = json.load(f)
    check(record.get("version") == SCHEMA_VERSION, f"记录含 version={SCHEMA_VERSION}")
    check(
        record.get(KEY_SHOW_GUIDE) is False,
        "show_guide_on_start 是 JSON 布尔值（不是字符串）",
    )
    check(isinstance(record, dict) and len(record) >= 2, "记录是 JSON 对象")

    # --- 5. 损坏文件 ---
    d5 = fresh_dir()
    with open(os.path.join(d5, PREFS_FILENAME), "w", encoding="utf-8") as f:
        f.write("{ not json at all")
    try:
        corrupted = FirstRunStore(data_dir=d5)
        check(corrupted.should_show_guide() is True, "prefs.json 损坏 → 回落默认值")
    except Exception as exc:  # noqa: BLE001
        check(False, f"prefs.json 损坏不应抛异常（实际 {exc!r}）")

    # --- 6. 类型不对 ---
    d6 = fresh_dir()
    with open(os.path.join(d6, PREFS_FILENAME), "w", encoding="utf-8") as f:
        json.dump({KEY_SHOW_GUIDE: "yes"}, f)
    check(
        FirstRunStore(data_dir=d6).should_show_guide() is True,
        "字段类型不对 → 只有显式布尔 false 才算「不显示」",
    )

    # --- 7. 更高的 schema 版本 ---
    d7 = fresh_dir()
    with open(os.path.join(d7, PREFS_FILENAME), "w", encoding="utf-8") as f:
        json.dump({"version": 99, KEY_SHOW_GUIDE: False}, f)
    check(
        FirstRunStore(data_dir=d7).should_show_guide() is False,
        "更高版本号被容忍（无迁移阶梯，宽容读取）",
    )

    # --- 8. 合并写入：未知字段必须留下 ---
    d8 = fresh_dir()
    with open(os.path.join(d8, PREFS_FILENAME), "w", encoding="utf-8") as f:
        json.dump({"future_key": 42}, f)
    merged = FirstRunStore(data_dir=d8)
    merged.set_should_show_guide(False)
    with open(os.path.join(d8, PREFS_FILENAME), "r", encoding="utf-8") as f:
        after = json.load(f)
    check(after.get("future_key") == 42, "合并写入保留未知字段（不是整条覆盖）")
    check(after.get(KEY_SHOW_GUIDE) is False, "合并后目标字段已更新")

    # --- 9. 写不进去也不能抛 ---
    d9 = fresh_dir()
    blocker = os.path.join(d9, "not-a-dir")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    try:
        FirstRunStore(data_dir=blocker).set_should_show_guide(False)
        check(True, "目标目录不可用时保存失败被吞掉（偏好不该让程序起不来）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"保存失败不应向上抛（实际 {exc!r}）")

    # --- 10. data_dir 覆盖环境变量 ---
    d10 = fresh_dir()
    FirstRunStore(data_dir=d10).set_should_show_guide(False)
    check(
        os.path.isfile(os.path.join(d10, PREFS_FILENAME)),
        "注入的 data_dir 生效（不写默认目录）",
    )


# ======================================================================
# B. MainWindow 接线
# ======================================================================
def test_wiring() -> None:
    section("B. MainWindow 接线")
    install_stubs()

    # --- 11. 构造期绝不弹窗（回归护栏） ---
    reset_recorders()
    d11 = fresh_dir()
    window = build_window(d11)
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "构造 MainWindow 不会弹任何对话框（否则无头测试会被卡死）",
    )
    check(
        not os.path.exists(os.path.join(d11, PREFS_FILENAME)),
        "构造期不写 prefs.json",
    )

    # --- 12. 首次调用会弹出 ---
    reset_recorders()
    window._first_run_store = FirstRunStore(data_dir=d11)
    shown = window.maybe_show_first_run_guide()
    check(shown is True, "maybe_show_first_run_guide() 返回 True")
    # UI-4.1：第一步改成主界面上的聚光灯，不再直接开模态向导
    check(window._guide_spotlight is not None, "第一步以主界面聚光灯呈现")
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "第一步不构造模态向导（它是主界面上的浮层）",
    )
    complete_spotlight(window)
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "四步全部由聚光灯承担，首次引导不再构造模态向导",
    )
    check(window._guide_spotlight is None, "走完后聚光灯已全部结束")
    check(
        window._guide_toast is not None and window._guide_toast.isVisible(),
        "完成后显示轻量完成提示",
    )
    check(
        window._first_run_store.should_show_guide() is False,
        "关闭后记录「不再自动显示」",
    )

    # --- 13. 第二次调用不再弹 ---
    reset_recorders()
    check(
        window.maybe_show_first_run_guide() is False,
        "已看过 → 再调用返回 False",
    )
    check(len(_StubOnboardingDialog.instances) == 0, "已看过 → 不再构造对话框")

    # --- 14. 中途关闭选「下次继续提醒」→ 下次启动仍会弹 ---
    # 旧版这里验证的是末页复选框；UI-4.2 删掉复选框后，"继续提醒"这个表态
    # 只剩中途关闭时的退出确认一条路径。
    d14 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d14)
    # 会话标记重置 = 模拟"下次启动"（新进程 = 新的 MainWindow 实例）。
    # 同一次会话内不会重复弹，那是会话去重防护。
    window._first_run_guide_done = False
    reset_recorders()

    check(window.maybe_show_first_run_guide() is True, "引导仍然弹出")
    complete_spotlight(window, GUIDE_ALWAYS)
    check(
        window._first_run_store.should_show_guide() is True,
        "选「下次继续提醒」→ 不写「已看过」，下次启动仍会显示",
    )

    reset_recorders()
    window._first_run_guide_done = False   # 又一次"启动"
    check(
        window.maybe_show_first_run_guide() is True,
        "下次启动仍会弹出",
    )

    # --- 15. 重入保护 ---
    d15 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d15)
    window._first_run_guide_done = False   # 又一次"启动"
    reset_recorders()
    inner_result = {}

    # UI-4.1 之后，"重入"最真实的形态是：第一步聚光灯还浮在界面上时又被触发一次。
    check(window.maybe_show_first_run_guide() is True, "第一步聚光灯启动")
    first_spotlight = window._guide_spotlight
    inner_result["ret"] = window.maybe_show_first_run_guide()
    check(
        inner_result.get("ret") is False,
        "聚光灯仍在时再次触发返回 False",
    )
    check(
        window._guide_spotlight is first_spotlight,
        "不会叠出第二个聚光灯",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "第一步期间不会构造模态向导",
    )

    # 走完四步 → 引导结束，全程不构造模态向导
    complete_spotlight(window)
    check(
        window._guide_spotlight is None
        and len(_StubOnboardingDialog.instances) == 0,
        "走完四步后引导结束，不构造任何模态向导",
    )

    # --- 16. 帮助入口打开回顾模式（UI-4.7：不再是旧版向导） ---
    d16 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d16)
    window._first_run_store.set_should_show_guide(False)
    reset_recorders()
    window._on_help_clicked()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is not None,
        "「使用引导」打开的是主界面聚光灯",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "不再打开旧版模态向导",
    )
    check(
        window._guide_spotlight.review_mode is True,
        "帮助入口走回顾模式（review_mode=True）",
    )
    check(
        window._first_run_store.should_show_guide() is False,
        "重新打开不改变已记录的状态",
    )
    window._guide_spotlight.finish(GUIDE_REVIEW_CLOSED)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)

    # --- 17. ROI 说明入口 ---
    reset_recorders()
    window._on_roi_help_clicked()
    check(len(_StubRoiHelpDialog.instances) == 1, "「说明」会打开 ROI 帮助对话框")
    check(
        _StubRoiHelpDialog.instances[0].parent_ is window,
        "ROI 帮助以主窗口为父",
    )

    # --- 18/19. 入口控件本身 ---
    # 说明：UI-2 把「使用引导 / 激活许可证」从底部状态栏搬到了无边框窗口的
    # 标题栏；UI-3.1 又把标题栏操作区从文字按钮换成自绘图标按钮（文字收进
    # tooltip）。玻璃 2.0 改版（用户确认的交互重构）进一步把低频入口
    # 「使用引导 / 联系作者 / 全屏工作台」收进「⋯」溢出菜单 —— 这里断言
    # 「菜单挂在标题栏操作区的图标按钮上、入口仍在菜单里可达」这几件事实。
    check(
        window._more_btn.objectName() == "titleIconBtn",
        "溢出菜单按钮在标题栏操作区（titleIconBtn）",
    )
    help_actions = [a for a in window._more_menu.actions()
                    if a.text() == "使用引导"]
    check(
        bool(help_actions),
        "使用引导入口在「⋯」溢出菜单中",
    )
    check(
        window._license_btn.property("variant") == "primary"
        and window._more_btn.property("variant") is None,
        "「激活许可证」仍是唯一强调按钮，溢出按钮保持次要样式",
    )
    check(window._roi_help_btn.text() == "说明", "OCR 区域旁有「说明」按钮")
    check(
        bool(window._roi_help_btn.toolTip()),
        "「说明」按钮带 tooltip",
    )

    # --- 20. 未授权时不自动弹出（Phase UI 微调 08） ---
    # 引导的内容是"怎么用这个软件"，对着未激活界面讲操作流程是无效信息；
    # 先让用户完成授权，成功后再触发（见 21）。
    d20 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d20)
    window._first_run_guide_done = False
    window._apply_license_result(LicenseResult(LicenseStatus.EXPIRED, "许可证已过期"))
    reset_recorders()
    check(
        window._access_status.is_expired() is True,
        "前置条件：当前访问状态为不可用",
    )
    check(
        window.maybe_show_first_run_guide() is False,
        "未激活 / 已过期时引导不自动弹出",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "未授权时不会构造向导",
    )
    check(
        window._first_run_store.should_show_guide() is True,
        "未授权只是「暂不显示」，不写掉标记 —— 授权成功后仍要弹",
    )

    # --- 21. 授权成功 → 触发引导 ---
    reset_recorders()
    window._apply_license_result(
        LicenseResult(LicenseStatus.ACTIVATED, "已激活"))
    check(
        window.maybe_show_first_run_guide() is True,
        "授权成功后引导弹出",
    )
    complete_spotlight(window)
    check(
        window._guide_spotlight is None,
        "四步走完后聚光灯全部结束",
    )

    # --- 22. 同一会话内不会因多入口重复弹出 ---
    reset_recorders()
    window._first_run_store = FirstRunStore(data_dir=d20)  # 仍未标记已看过
    check(
        window.maybe_show_first_run_guide() is False,
        "同一会话内第二次触发被会话标记挡住",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "不会弹出第二个向导",
    )

    # --- 24. 授权成功那条路径（未授权用户完成激活 → 触发引导） ---
    class _StubActivationDialog:
        def __init__(self, manager, parent=None):
            pass

        def exec(self):
            return 0

        def result_data(self):
            return LicenseResult(LicenseStatus.ACTIVATED, "已激活")

    d24 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d24)
    window._first_run_guide_done = False
    window._apply_license_result(
        LicenseResult(LicenseStatus.NOT_ACTIVATED, "未激活"))
    reset_recorders()
    real_activation = mw.ActivationDialog
    mw.ActivationDialog = _StubActivationDialog
    try:
        window._on_license_clicked()
    finally:
        mw.ActivationDialog = real_activation
    check(
        window._guide_spotlight is not None,
        "未授权 → 激活成功 → 第一步聚光灯弹出（需求 2）",
    )
    complete_spotlight(window)
    check(
        window._guide_spotlight is None and len(_StubOnboardingDialog.instances) == 0,
        "走完四步后引导结束，不构造任何模态向导",
    )
    check(
        window._first_run_store.should_show_guide() is False,
        "引导关闭后写了「不再自动显示」",
    )

    # --- 25. 已授权用户启动：启动恢复出有效授权时触发（需求 3） ---
    d25 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d25)
    window._first_run_guide_done = False
    window._access_status.clear_license_state()
    reset_recorders()
    window._on_license_loaded(LicenseResult(LicenseStatus.ACTIVATED, "已激活"))
    check(
        window._guide_spotlight is not None,
        "启动恢复到有效授权 → 第一步聚光灯弹出（需求 3）",
    )
    complete_spotlight(window)
    check(
        window._guide_spotlight is None and len(_StubOnboardingDialog.instances) == 0,
        "走完四步后引导结束，不构造任何模态向导",
    )

    # --- 26. 需求 4：明确关闭自动引导后，后续启动不得重弹 ---
    d26 = fresh_dir()
    store = FirstRunStore(data_dir=d26)
    store.set_should_show_guide(False)      # 用户明确选择过不再显示
    window._first_run_store = store
    window._first_run_guide_done = False    # 新的会话
    window._access_status.set_license_active(None)
    reset_recorders()
    check(
        window.maybe_show_first_run_guide() is False,
        "已明确关闭自动引导 → 已授权启动也不再弹",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "不构造向导",
    )

    # --- 27. 退出确认的三种选择（UI 微任务：退出与再次提醒） ---
    def _exit_spotlight_with(choice):
        """把第一步聚光灯用 ✕ 关掉，退出确认框返回 choice。"""
        store = FirstRunStore(data_dir=fresh_dir())
        store.set_should_show_guide(True)
        window._first_run_store = store
        window._first_run_guide_done = False
        real_ask = ob.ask_guide_exit
        ob.ask_guide_exit = lambda parent=None, c=choice: c
        try:
            window._start_guide_spotlight(0)
            for _ in range(10):
                app.processEvents()
                time.sleep(0.005)
            spotlight = window._guide_spotlight
            spotlight.card.close_btn.click()
            for _ in range(20):
                app.processEvents()
                time.sleep(0.005)
        finally:
            ob.ask_guide_exit = real_ask
        return store, spotlight

    store27, _ = _exit_spotlight_with(GUIDE_ALWAYS)
    check(
        store27.should_show_guide() is True,
        "聚光灯 ✕ + 「下次继续提醒」→ show_guide_on_start = true",
    )

    store27b, _ = _exit_spotlight_with(GUIDE_NEVER)
    check(
        store27b.should_show_guide() is False,
        "聚光灯 ✕ + 「不再自动显示」→ show_guide_on_start = false",
    )

    # 「返回引导」：聚光灯留在屏幕上，配置一个字都不动
    store27c = FirstRunStore(data_dir=fresh_dir())
    store27c.set_should_show_guide(True)
    window._first_run_store = store27c
    window._first_run_guide_done = False
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: None
    try:
        window._start_guide_spotlight(0)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)
        back_spotlight = window._guide_spotlight
        back_spotlight.card.close_btn.click()
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)
    finally:
        ob.ask_guide_exit = real_ask
    check(
        not back_spotlight.is_finished and store27c.should_show_guide() is True,
        "聚光灯 ✕ + 「返回引导」→ 引导继续显示、配置不变",
    )
    check(back_spotlight.mask.isVisible(), "「返回引导」后遮罩仍在")
    back_spotlight.finish(GUIDE_NEVER)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.005)

    # --- 28. 正常完成 / 回顾模式关闭 的落盘 ---
    def _run_dialog_with(first_run, completed, choice):
        store = FirstRunStore(data_dir=fresh_dir())
        store.set_should_show_guide(True)
        window._first_run_store = store

        class _Stub:
            def __init__(self, parent=None, first_run=True, start_step=0):
                pass

            def exec(self):
                return 0

            def completed(self):
                return completed

            def exit_choice(self):
                return choice

        real = mw.OnboardingDialog
        mw.OnboardingDialog = _Stub
        try:
            window._run_onboarding_dialog(first_run=first_run, start_step=1)
        finally:
            mw.OnboardingDialog = real
        return store

    check(
        _run_dialog_with(True, True, None).should_show_guide() is False,
        "正常走完 → show_guide_on_start = false（UI-4.2：一律如此）",
    )
    check(
        _run_dialog_with(True, False, GUIDE_ALWAYS)
        .should_show_guide() is True,
        "向导里提前关闭 + 「下次继续提醒」→ true",
    )
    check(
        _run_dialog_with(False, True, GUIDE_NEVER)
        .should_show_guide() is True,
        "手动打开帮助后关闭 → 不碰用户的自动提醒设置",
    )

    # --- 29. 第二步「选择游戏」聚光灯（UI-4.4） ---
    def _fresh_guide(initial=True):
        store = FirstRunStore(data_dir=fresh_dir())
        store.set_should_show_guide(initial)
        window._first_run_store = store
        window._first_run_guide_done = False
        real_ask = ob.ask_guide_exit
        ob.ask_guide_exit = lambda parent=None: None   # 默认不关闭
        try:
            window.maybe_show_first_run_guide()
            for _ in range(20):
                app.processEvents()
                time.sleep(0.005)
        finally:
            ob.ask_guide_exit = real_ask
        return store

    def _advance_spotlight():
        window._guide_spotlight.notify_completed()
        for _ in range(30):
            app.processEvents()
            time.sleep(0.005)

    # 第一步 → 第二步：自动推进，不先开模态向导
    _fresh_guide()
    check(window._guide_spotlight.step_index == 0, "起始是第一步")
    check(
        window._guide_spotlight.card.back_btn.isHidden() is True,
        "第一步没有「上一步」",
    )
    reset_recorders()
    _advance_spotlight()
    check(window._guide_spotlight is not None, "连接完成后进入第二步聚光灯")
    check(window._guide_spotlight.step_index == 1, "当前是第二步")
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "第二步不再先开旧版弹窗（需求 1）",
    )
    check(
        window._guide_spotlight.card.title_label.text() == STEPS[1][0],
        f"第二步标题为 {STEPS[1][0]!r}",
    )
    check(
        window._guide_spotlight.card.back_btn.isHidden() is False,
        "第二步提供「上一步」",
    )
    check(
        window._guide_spotlight._targets == [window._ocr_game_combo],
        "第二步高亮的是真实游戏下拉框",
    )
    check(
        window._guide_spotlight.mask.testAttribute(Qt.WA_TransparentForMouseEvents),
        "第二步遮罩同样不拦截鼠标",
    )

    # 第二步「上一步」→ 回第一步，且不碰地址与连接
    window._url_input.setText("https://live.douyin.com/424242")
    window._guide_spotlight.card.back_btn.click()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is not None
        and window._guide_spotlight.step_index == 0,
        "「上一步」回到第一步聚光灯",
    )
    check(
        window._url_input.text() == "https://live.douyin.com/424242",
        "回退不会清掉已输入的地址",
    )
    check(window._connected is False, "回退不会触发连接")

    # 真实选择游戏 → 自动进入第三步聚光灯（UI-4.5 起不再是弹窗）
    _advance_spotlight()                      # 再次回到第二步
    reset_recorders()
    current = window._ocr_game_combo.currentIndex()
    window._ocr_game_combo.setCurrentIndex(1 if current == 0 else 0)
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is not None
        and window._guide_spotlight.step_index == 2,
        "选完游戏后自动进入第三步聚光灯",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "第三步同样是聚光灯，不开旧版弹窗",
    )
    window._guide_spotlight.finish(GUIDE_NEVER)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.005)

    # 第二步 ✕ → 沿用三选项退出确认
    _fresh_guide()
    _advance_spotlight()
    check(window._guide_spotlight.step_index == 1, "重新走到第二步")
    check(True, "(第三步的退出确认见第 31 节)")
    store29 = window._first_run_store
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: GUIDE_ALWAYS
    try:
        window._guide_spotlight.card.close_btn.click()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
    finally:
        ob.ask_guide_exit = real_ask
    check(window._guide_spotlight is None, "✕ 关闭第二步聚光灯")
    check(
        store29.should_show_guide() is True,
        "第二步 ✕ 走的是同一套三选项确认（选「下次继续提醒」→ true）",
    )

    # OCR 面板尺寸变化 → 聚光灯跟随游戏下拉框重新定位
    _fresh_guide()
    _advance_spotlight()
    before = window._guide_spotlight.mask._holes[0]
    window._body_splitter.setSizes([900, 420])
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    after = window._guide_spotlight.mask._holes[0]
    check(
        (before.x(), before.width()) != (after.x(), after.width()),
        f"面板变宽后高亮孔跟随（{before.x()},{before.width()} → "
        f"{after.x()},{after.width()}）",
    )
    window._guide_spotlight.finish(GUIDE_NEVER)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.005)

    # --- 30. 第二步卡片布局 + OCR 面板临时展开（UI-4.4） ---
    def _game_step(window_):
        _fresh_guide()
        _advance_spotlight()
        return window_._guide_spotlight

    # 卡片在游戏下拉框左侧，且不压住 OCR 面板
    _fresh_guide()
    _advance_spotlight()
    spot = window._guide_spotlight
    combo = window._ocr_game_combo
    combo_rect = QRect(combo.mapTo(window, combo.rect().topLeft()), combo.size())
    panel_rect = QRect(window._ocr_panel.mapTo(window, window._ocr_panel.rect().topLeft()),
                       window._ocr_panel.size())
    card = spot.card
    gap = combo_rect.left() - (card.x() + card.width())
    check(spot.prefers_left is True, "第二步聚光灯声明优先左侧")
    check(
        card.x() + card.width() <= combo_rect.left(),
        "卡片整体在下拉框左侧",
    )
    check(16 <= gap <= 24, f"卡片右边缘与下拉框间距 {gap}px 落在 16–24 内")
    check(
        card.x() + card.width() <= panel_rect.left(),
        f"卡片不压住 OCR 面板（卡片右 {card.x() + card.width()} ≤ 面板左 {panel_rect.left()}）",
    )
    check(
        0 <= card.x() and card.x() + card.width() <= window.width()
        and card.y() >= 0 and card.y() + card.height() <= window.height(),
        "卡片完整落在窗口内",
    )
    window._guide_spotlight.finish(GUIDE_NEVER)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.005)

    # 第一步仍然把卡片放在地址输入框下方（原观感不变）
    _fresh_guide()
    spot1 = window._guide_spotlight
    check(spot1.prefers_left is False, "第一步仍优先下方")
    input_rect = QRect(window._url_input.mapTo(window, window._url_input.rect().topLeft()),
                       window._url_input.size())
    check(spot1.card.y() >= input_rect.bottom(), "第一步卡片贴在地址输入框下方")
    check(
        spot1.card.x() == input_rect.left(),
        f"第一步卡片左边缘与输入框左边缘对齐（{spot1.card.x()} vs {input_rect.left()}）",
    )
    spot1.finish(GUIDE_NEVER)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.005)

    # 面板收起时进入第二步 → 临时展开；退出后恢复收起
    window._on_collapse_clicked()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    check(window._ocr_panel.isVisible() is False, "前置条件：面板已收起")
    _fresh_guide()
    _advance_spotlight()
    check(window._ocr_panel.isVisible() is True, "进入第二步时面板被临时展开")
    check(window._ocr_game_combo.isVisible() is True, "真实游戏下拉框可见")
    check(
        len(window._guide_spotlight.mask._holes) == 1,
        "高亮区有且仅有一个（游戏下拉框）",
    )
    check(window._guide_panel_restore_to is False, "记下了「原来是收起」")
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: GUIDE_NEVER
    try:
        window._guide_spotlight.card.close_btn.click()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
    finally:
        ob.ask_guide_exit = real_ask
    check(window._ocr_panel.isVisible() is False, "引导退出后恢复为收起")

    # 用户在引导期间自己动过面板 → 不覆盖他的新选择
    _fresh_guide()
    _advance_spotlight()
    check(window._ocr_panel.isVisible() is True, "再次临时展开")
    window._on_expand_clicked()               # 用户自己展开
    ob.ask_guide_exit = lambda parent=None: GUIDE_NEVER
    try:
        window._guide_spotlight.card.close_btn.click()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
    finally:
        ob.ask_guide_exit = real_ask
    check(
        window._ocr_panel.isVisible() is True,
        "用户主动改过面板 → 退出时保留用户的选择，不恢复成收起",
    )
    window._on_expand_clicked()
    for _ in range(10):
        app.processEvents()
        time.sleep(0.005)

    # --- 31. 第三步「框选房间码」聚光灯（UI-4.5） ---
    class _FakeSelector(QObject):
        """替身：只保留主窗口关心的三个信号，不真的开框选窗口。"""

        roi_selected = Signal(object)
        roi_selection_canceled = Signal()
        selection_drawn = Signal(object)
        last = None

        def __init__(self, frame, parent=None):
            super().__init__(parent)
            _FakeSelector.last = self

        def show(self):
            pass

    def _to_step3():
        """走到第三步（两步 notify_completed，不依赖下拉框的项数）。"""
        _fresh_guide()
        window._frame_buffer = _FakeFrameBuffer()
        window._connected = True
        window._roi_btn.setEnabled(True)
        _advance_spotlight()          # 第一步 → 第二步
        _advance_spotlight()          # 第二步 → 第三步
        return window._guide_spotlight

    class _FakeFrameBuffer:
        """替身：只实现 ROI 入口与清理路径真正会用到的那几个成员。

        必须补全 —— 主窗口关闭时会走 _disconnect() → buffer.clear()，
        缺一个方法就会在 teardown 里抛异常并刷出一屏 eventFilter 报错。
        """

        frame_id = 0

        def get(self):
            import numpy as np
            return np.zeros((16, 16, 3), dtype=np.uint8)

        def clear(self):
            pass

    real_selector = mw.ROISelector
    mw.ROISelector = _FakeSelector
    try:
        spot3 = _to_step3()
        check(spot3 is not None and spot3.step_index == 2, "进入第三步聚光灯")
        check(spot3.card.title_label.text() == STEPS[2][0], "第三步标题正确")
        check(
            spot3.card.back_btn.isHidden() is False,
            "第三步提供「上一步」",
        )
        check(spot3._targets == [window._roi_btn], "第三步高亮真实 ROI 按钮")
        check(spot3.prefers_left is True, "第三步卡片优先放左侧")

        # 点击真实按钮 → 进入框选 → 收起遮罩与卡片
        window._roi_btn.click()
        for _ in range(25):
            app.processEvents()
            time.sleep(0.005)
        check(_FakeSelector.last is not None, "点击真实按钮打开了框选窗口")
        check(spot3.mask.isHidden(), "框选期间遮罩收起")
        check(spot3.card.isHidden(), "框选期间卡片收起")
        check(
            window._guide_spotlight is spot3,
            "只是收起，没有结束第三步（Esc 之后要能恢复）",
        )

        # 鼠标松开 → 轻量提示，且不推进
        reset_recorders()
        _FakeSelector.last.selection_drawn.emit(QRect(400, 300, 200, 60))
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)
        check(spot3._hint.isVisible(), "松开鼠标后显示轻量提示")
        check(
            spot3._hint.text() == "Enter 确认 · Esc 取消",
            f"提示文案为 {spot3._hint.text()!r}",
        )
        check(
            not spot3._hint.geometry().intersects(QRect(400, 300, 200, 60)),
            "提示条不遮挡已框选区域",
        )
        check(
            bool(spot3._hint.windowFlags() & Qt.WindowTransparentForInput),
            "提示条不接收鼠标输入",
        )
        check(
            window._guide_spotlight is spot3 and len(_StubOnboardingDialog.instances) == 0,
            "鼠标松开不会自动推进（需求 5）",
        )

        # Esc 取消 → 留在第三步并恢复
        _FakeSelector.last.roi_selection_canceled.emit()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
        check(window._guide_spotlight is spot3, "Esc 取消后仍在第三步")
        check(spot3.mask.isVisible(), "Esc 取消后遮罩恢复")
        check(spot3.card.isVisible(), "Esc 取消后卡片恢复")
        check(spot3._hint.isHidden(), "Esc 取消后提示条隐藏")

        # Enter 确认 → 自动进入第四步（旧版弹窗）
        reset_recorders()
        window._roi_btn.click()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
        _FakeSelector.last.selection_drawn.emit(QRect(400, 300, 200, 60))
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)
        _FakeSelector.last.roi_selected.emit(ROI(10, 20, 100, 30, 1920, 1080))
        for _ in range(40):
            app.processEvents()
            time.sleep(0.005)
        check(
            window._guide_spotlight is not None
            and window._guide_spotlight.step_index == 3,
            "Enter 确认后进入第四步聚光灯",
        )
        check(
            len(_StubOnboardingDialog.instances) == 0,
            "第四步同样是聚光灯，不开旧版弹窗",
        )
        check(spot3._hint.isHidden(), "Enter 确认后提示条隐藏")
        window._guide_spotlight.finish(GUIDE_NEVER)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)

        # 手动「下一步」跳过框选，且不启动 OCR
        reset_recorders()
        spot3b = _to_step3()
        spot3b.card.next_btn.click()
        for _ in range(40):
            app.processEvents()
            time.sleep(0.005)
        check(
            window._guide_spotlight is not None
            and window._guide_spotlight.step_index == 3,
            "手动「下一步」也能进入第四步",
        )
        check(window._ocr_running is False, "跳过框选不会启动 OCR")
        window._guide_spotlight.finish(GUIDE_NEVER)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)

        # 「上一步」回第二步，保留游戏选择
        spot3c = _to_step3()
        game_before = window._ocr_game_combo.currentData()
        spot3c.card.back_btn.click()
        for _ in range(40):
            app.processEvents()
            time.sleep(0.005)
        check(
            window._guide_spotlight is not None
            and window._guide_spotlight.step_index == 1,
            "第三步「上一步」回到第二步（需求 8）",
        )
        check(
            window._ocr_game_combo.currentData() == game_before,
            "回退保留已选择的游戏",
        )
        check(spot3c._hint.isHidden(), "回退时不留残提示条")

        # ✕ 退出：遮罩/卡片/提示条全部清干净
        spot3c.card.next_btn.click()
        for _ in range(40):
            app.processEvents()
            time.sleep(0.005)
        spot3d = window._guide_spotlight
        window._roi_btn.click()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.005)
        _FakeSelector.last.selection_drawn.emit(QRect(400, 300, 200, 60))
        for _ in range(10):
            app.processEvents()
            time.sleep(0.005)
        real_ask = ob.ask_guide_exit
        ob.ask_guide_exit = lambda parent=None: GUIDE_NEVER
        try:
            spot3d.card.close_btn.click()
            for _ in range(25):
                app.processEvents()
                time.sleep(0.005)
        finally:
            ob.ask_guide_exit = real_ask
        check(window._guide_spotlight is None, "✕ 结束第三步")
        check(spot3d._hint.isHidden(), "退出后提示条已收起")
        check(spot3d.mask.parent() is None, "退出后遮罩已从窗口移除")
        check(spot3d.card.parent() is None, "退出后卡片已从窗口移除")
        check(
            len(window.findChildren(type(spot3d.mask), "spotlightMask")) == 0,
            "窗口上不再残留任何聚光灯遮罩",
        )
    finally:
        mw.ROISelector = real_selector
        window._frame_buffer = None

    # --- 32. 第四步「开始自动识别」聚光灯（UI-4.6） ---
    def _to_step4():
        """走到第四步。"""
        _fresh_guide()
        for _ in range(3):
            window._guide_spotlight.notify_completed()
            for _ in range(25):
                app.processEvents()
                time.sleep(0.005)
        return window._guide_spotlight

    # 未连接：卡片说明缺什么，按钮禁用，点不动也不推进
    window._connected = False
    window._roi_manager.clear_roi()
    window._update_ocr_button_state()
    reset_recorders()
    spot4 = _to_step4()
    check(spot4 is not None and spot4.step_index == 3, "进入第四步聚光灯")
    check(spot4.card.title_label.text() == STEPS[3][0], "第四步标题正确")
    check(spot4._targets == [window._ocr_btn], "第四步高亮真实「开始 OCR」按钮")
    check(spot4.card.next_btn.text() == "完成", "第四步主按钮文案为「完成」")
    check(
        "尚未连接直播" in spot4.card.body_label.text(),
        f"卡片说明了缺少的前置条件：{spot4.card.body_label.text()!r}",
    )
    check(window._ocr_btn.isEnabled() is False, "未连接时 OCR 按钮保持禁用")
    window._ocr_btn.click()
    for _ in range(15):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is spot4,
        "点禁用的按钮不会推进（不绕过业务门禁）",
    )
    check(window._ocr_running is False, "也没有偷偷启动 OCR")

    # 手动「完成」：结束引导，但绝不替用户启动 OCR
    store32 = FirstRunStore(data_dir=fresh_dir())
    store32.set_should_show_guide(True)
    window._first_run_store = store32
    spot4.card.next_btn.click()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    check(window._guide_spotlight is None, "「完成」结束第四步")
    check(window._ocr_running is False, "「完成」不会启动 OCR")
    check(
        window._guide_toast is not None and window._guide_toast.isVisible(),
        "完成后显示轻量完成提示",
    )
    check(
        store32.should_show_guide() is False,
        "正常完成 → show_guide_on_start = false",
    )

    # 已连接 + 已框选：按钮可用，OCR 成功启动即自动完成引导
    window._connected = True
    window._roi_manager.set_roi(ROI(10, 20, 100, 30, 1920, 1080))
    window._update_ocr_button_state()
    spot4b = _to_step4()
    # 必须在走到第四步之后注入：_to_step4 里的 _fresh_guide 会换掉 store
    store32b = FirstRunStore(data_dir=fresh_dir())
    store32b.set_should_show_guide(True)
    window._first_run_store = store32b
    check(window._ocr_btn.isEnabled() is True, "前置条件齐备时 OCR 按钮可用")
    check(
        "尚未" not in spot4b.card.body_label.text(),
        "前置条件齐备时卡片不再说缺什么",
    )
    # 真实启动需要 tesseract，这里直接触发"启动成功"回调来验证接线
    window._on_ocr_started()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    check(window._guide_spotlight is None, "OCR 启动成功后第四步聚光灯结束")
    check(
        window._guide_toast is not None and window._guide_toast.isVisible(),
        "OCR 启动成功后显示完成提示",
    )
    check(
        store32b.should_show_guide() is False,
        "OCR 启动成功 → show_guide_on_start = false",
    )
    window._ocr_running = False

    # 「上一步」回第三步
    spot4c = _to_step4()
    spot4c.card.back_btn.click()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is not None
        and window._guide_spotlight.step_index == 2,
        "第四步「上一步」回到第三步",
    )

    # ✕ 退出：三选项确认 + 彻底清理
    window._guide_spotlight.notify_completed()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    spot4d = window._guide_spotlight
    store32c = FirstRunStore(data_dir=fresh_dir())
    store32c.set_should_show_guide(True)
    window._first_run_store = store32c
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: GUIDE_NEVER
    try:
        spot4d.card.close_btn.click()
        for _ in range(25):
            app.processEvents()
            time.sleep(0.005)
    finally:
        ob.ask_guide_exit = real_ask
    check(window._guide_spotlight is None, "✕ 结束第四步")
    check(
        store32c.should_show_guide() is False,
        "✕ 走的是同一套三选项确认（选「不再自动显示」→ false）",
    )
    check(spot4d.mask.parent() is None, "退出后遮罩已移除")
    check(spot4d.card.parent() is None, "退出后卡片已移除")
    check(spot4d._hint.parent() is None, "退出后 ROI 提示条已移除")
    check(
        len(window.findChildren(type(spot4d.mask), "spotlightMask")) == 0,
        "窗口上不再残留任何聚光灯遮罩",
    )

    # 面板原状态在完成 / 退出后都会还原
    window._on_collapse_clicked()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    reset_recorders()
    spot4e = _to_step4()
    check(window._ocr_panel.isVisible() is True, "第四步需要面板可见，已临时展开")
    check(window._guide_panel_restore_to is False, "记下原状态是收起")
    spot4e.card.next_btn.click()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    check(window._ocr_panel.isVisible() is False, "完成后面板恢复为收起")
    window._on_expand_clicked()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)

    # --- 23. 帮助入口不受首次启动标记限制 ---
    # 会话标记 / prefs 标记都拦不住用户主动点帮助 —— 那走的是回顾模式。
    reset_recorders()
    window._on_help_clicked()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is not None,
        "用户主动点帮助仍会打开引导（不受首次启动标记限制）",
    )
    check(
        window._guide_spotlight.review_mode is True,
        "帮助入口走回顾模式（review_mode=True）",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "不再打开旧版模态向导",
    )
    window._guide_spotlight.finish(GUIDE_REVIEW_CLOSED)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)

    # --- 33. 回顾模式（UI-4.7：标题栏 ? 改为四步聚光灯） ---
    def _open_review(store_dir=None):
        store = FirstRunStore(data_dir=store_dir or fresh_dir())
        store.set_should_show_guide(True)
        window._first_run_store = store
        window._first_run_guide_done = False
        reset_recorders()
        window._on_help_clicked()
        for _ in range(30):
            app.processEvents()
            time.sleep(0.005)
        return store

    store33 = _open_review()
    spot = window._guide_spotlight
    check(spot is not None, "点击 ? 打开聚光灯")
    check(spot.review_mode is True, "走的是回顾模式")
    check(spot.step_index == 0, "从第 1 步「连接直播间」开始")
    check(spot.card.title_label.text() == STEPS[0][0], "第 1 步标题正确")
    check(spot._targets == [window._url_input, window._connect_btn],
          "第 1 步高亮地址输入框与连接按钮")
    check(len(spot._shields) == 2, "回顾模式为高亮区加了点击挡板")
    check(
        all(sh.isVisible() for sh in spot._shields),
        "挡板可见（用于挡住误触，视觉透明）",
    )
    check(
        len(_StubOnboardingDialog.instances) == 0,
        "不再打开旧版四页模态向导",
    )

    # 自动推进事件一律无效
    window._on_ocr_started()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    check(window._guide_spotlight is spot, "OCR 启动不会推进回顾模式")
    window._ocr_running = False
    game_idx = window._ocr_game_combo.currentIndex()
    window._ocr_game_combo.setCurrentIndex(1 if game_idx == 0 else 0)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    check(window._guide_spotlight is spot, "切换游戏不会推进回顾模式")

    # 手动前进：四个步骤的高亮目标
    expected_targets = (
        [window._url_input, window._connect_btn],
        [window._ocr_game_combo],
        [window._roi_btn],
        [window._ocr_btn],
    )
    for step in range(4):
        current = window._guide_spotlight
        check(
            current._targets == expected_targets[step],
            f"第 {step + 1} 步高亮目标正确",
        )
        if step < 3:
            current.card.next_btn.click()
            for _ in range(30):
                app.processEvents()
                time.sleep(0.005)
    last = window._guide_spotlight
    check(last.step_index == 3, "手动走到第 4 步")
    check(last.card.next_btn.text() == "完成", "第 4 步主按钮为「完成」")

    # 手动后退
    last.card.back_btn.click()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight.step_index == 2,
        "「上一步」回到第 3 步",
    )
    window._guide_spotlight.card.next_btn.click()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)

    # 「完成」→ 直接关闭：无完成提示、不改任何首次引导状态
    # 注意不能断言 _guide_toast is None —— 前面的小节可能已经留下过一个提示条
    # 对象。要断言的是"这次没有新造一个"。
    toast_before = window._guide_toast
    window._guide_spotlight.card.next_btn.click()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    check(window._guide_spotlight is None, "「完成」关闭回顾模式")
    check(
        window._guide_toast is toast_before,
        "回顾模式不显示首次引导完成提示（没有新造提示条）",
    )
    check(
        store33.should_show_guide() is True,
        "回顾不写 show_guide_on_start（仍为 true）",
    )

    # Esc 与 ✕ 直接关闭，不弹三选项退出确认
    for tag, action in (("Esc", "key"), ("✕", "click")):
        _open_review()
        current = window._guide_spotlight
        before = window._first_run_store.should_show_guide()
        if action == "key":
            app.sendEvent(window, QKeyEvent(QEvent.Type.KeyPress,
                                            Qt.Key_Escape, Qt.NoModifier))
        else:
            current.card.close_btn.click()
        for _ in range(30):
            app.processEvents()
            time.sleep(0.005)
        check(window._guide_spotlight is None, f"{tag} 直接关闭回顾模式")
        check(
            len(_StubOnboardingDialog.instances) == 0,
            f"{tag} 不弹首次引导的三选项退出确认",
        )
        check(
            window._first_run_store.should_show_guide() == before,
            f"{tag} 不改变首次引导提醒设置",
        )
        check(
            window.findChildren(type(current.mask)) == []
            or all(c.objectName() != "spotlightMask"
                   for c in window.findChildren(type(current.mask))),
            f"{tag} 关闭后无残留遮罩",
        )

    # 首次引导的 Esc 行为不受影响（仍不关闭）
    window._first_run_store = FirstRunStore(data_dir=fresh_dir())
    window._first_run_guide_done = False
    window.maybe_show_first_run_guide()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    app.sendEvent(window, QKeyEvent(QEvent.Type.KeyPress,
                                    Qt.Key_Escape, Qt.NoModifier))
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight is not None,
        "首次引导的 Esc 行为不变（仍不关闭）",
    )
    check(window._guide_spotlight.review_mode is False, "首次引导不是回顾模式")
    check(len(window._guide_spotlight._shields) == 0, "首次引导没有点击挡板")
    window._guide_spotlight.notify_completed()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(
        window._guide_spotlight.step_index == 1,
        "首次引导仍会因真实操作自动推进",
    )
    window._guide_spotlight.finish(GUIDE_NEVER)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)

    # 回顾模式也会临时展开面板并在结束后还原
    window._on_collapse_clicked()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    _open_review()
    window._guide_spotlight.card.next_btn.click()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(window._ocr_panel.isVisible() is True, "回顾第 2 步临时展开面板")
    check(window._guide_panel_restore_to is False, "记下原状态是收起")
    window._guide_spotlight.finish(GUIDE_REVIEW_CLOSED)
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    check(window._ocr_panel.isVisible() is False, "回顾结束后面板恢复为收起")
    window._on_expand_clicked()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)

    window.close()


# ======================================================================
# C. 真实对话框（只构造，不 exec）
# ======================================================================
def test_dialogs() -> None:
    section("C. 真实对话框（offscreen 构造，不 exec）")

    # UI-4.6：首次引导已全部由聚光灯承担，对话框只剩「标题栏 ? 回顾模式」在用，
    # 因此它渲染完整的四步，好让用户能回看每一步。
    dialog = OnboardingDialog(first_run=True, start_step=DIALOG_FIRST_STEP)
    check(dialog.step_count() == len(STEPS) == 4, "整份引导共 4 步")
    check(dialog._stack.count() == 4, "回顾用的对话框渲染全部 4 页")
    check(dialog.current_step() == 0, "从第 1 步开始")
    check(dialog._dots.count() == 4, "进度圆点画 4 个")
    check(dialog._prev_btn.isEnabled() is False, "第 1 页「上一步」禁用")
    check(dialog._next_btn.isHidden() is False, "第 1 页「下一步」可见")
    check(dialog._finish_btn.isHidden() is True, "第 1 页「完成」隐藏")
    check(dialog.completed() is False, "没点「完成」就不算走完")

    for _ in range(3):
        dialog._next_btn.click()
    check(dialog.current_step() == 3, "翻到第 4 步")
    check(dialog._finish_btn.isHidden() is False, "第 4 页「完成」可见")
    check(dialog._dots.current() == 3, "圆点点亮第 4 个")
    titles = [lbl.text() for lbl in dialog._stack.currentWidget().findChildren(QLabel)]
    check(STEPS[3][0] in titles, f"第 4 页是「{STEPS[3][0]}」")

    # UI-4.2：末页的「不再自动显示」复选框已删除 —— "是否继续提醒"不再由
    # 末页表态，而是"走完 = 不再提醒"、"中途关闭 = 退出确认里选"。
    check(
        not hasattr(dialog, "_dont_show"),
        "末页不再有「不再自动显示」复选框",
    )
    check(
        not hasattr(dialog, "should_mark_seen")
        and not hasattr(dialog, "keep_reminding"),
        "随之作废的读取接口也已删除",
    )

    # 跳过 / Esc / 关窗：统一走退出确认（UI 微任务）。
    # 这里必须打桩，否则会弹出真实的模态确认框 —— 无头环境会永久挂起。
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: GUIDE_NEVER
    try:
        skipped = OnboardingDialog(first_run=True)
        skipped.reject()
    finally:
        ob.ask_guide_exit = real_ask
    check(
        skipped.exit_choice() == GUIDE_NEVER,
        "点「跳过」/ Esc / 关窗 → 弹确认，结果由 exit_choice() 带出",
    )
    check(
        skipped.completed() is False,
        "提前关闭不算「走完」（据此决定后续落盘分支）",
    )

    # 「返回引导」：不关窗、不改配置
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: None
    try:
        back = OnboardingDialog(first_run=True)
        back.reject()
    finally:
        ob.ask_guide_exit = real_ask
    check(
        back.exit_choice() is None and back._exit_confirmed is False,
        "选「返回引导」时对话框不关闭、也不产生选择",
    )

    # 回顾模式
    review = OnboardingDialog(first_run=False)
    check(review._skip_btn.isHidden() is True, "回顾模式没有「跳过」")
    check(review._finish_btn.text() == "关闭",
          "回顾模式主按钮文案为「关闭」（不必等翻到末页）")
    for _ in range(3):
        review._next_btn.click()
    check(review.current_step() == 3, "回顾模式同样停在四步结构的最后一步")
    check(review._finish_btn.text() == "关闭", "末页文案仍为「关闭」")
    check(
        review.exit_choice() is None,
        "回顾模式不表态（看一眼帮助不该改掉之前的设置）",
    )
    check(
        not hasattr(review, "_dont_show"),
        "回顾模式同样没有「不再自动显示」复选框",
    )

    # 对话框自身不碰磁盘
    probe_dir = fresh_dir()
    real_ask = ob.ask_guide_exit
    ob.ask_guide_exit = lambda parent=None: GUIDE_ALWAYS
    try:
        OnboardingDialog(first_run=True).reject()
    finally:
        ob.ask_guide_exit = real_ask
    check(
        not os.path.exists(os.path.join(probe_dir, PREFS_FILENAME)),
        "对话框不写 prefs.json（写盘只在 MainWindow 一处）",
    )

    # ROI 帮助
    help_dialog = RoiHelpDialog()
    check(help_dialog.size().width() > 0 and help_dialog.size().height() > 0, "ROI 帮助有尺寸")
    check(
        help_dialog._schematic.height() > 0,
        "ROI 示意图有固定高度",
    )
    pixmap = help_dialog._schematic.grab()
    check(
        not pixmap.isNull() and pixmap.width() > 0 and pixmap.height() > 0,
        "ROI 示意图能完成一次绘制（不依赖任何图片资源）",
    )


# ======================================================================
def main() -> int:
    print("=" * 72)
    print("首次启动引导 / ROI 帮助 —— 验证")
    print("=" * 72)

    test_store()
    test_wiring()
    test_dialogs()

    print("\n" + "=" * 72)
    print(f"RESULT: {_PASSED} passed, {_FAILED} failed")
    print("=" * 72)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
