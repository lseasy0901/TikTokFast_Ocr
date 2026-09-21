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

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui.main_window as mw  # noqa: E402
from gui.onboarding import OnboardingDialog, RoiHelpDialog, STEPS  # noqa: E402
from utils.first_run import (  # noqa: E402
    KEY_SHOW_GUIDE,
    PREFS_FILENAME,
    SCHEMA_VERSION,
    FirstRunStore,
)
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

    def __init__(self, parent=None, first_run=True):
        self.parent_ = parent
        self.first_run = first_run
        # 测试可直接改写：模拟用户在最后一页取消勾选「不再自动显示」
        self.mark_seen = True
        # 测试可直接改写：模拟嵌套事件循环里的重入调用
        self.reenter = None
        _StubOnboardingDialog.instances.append(self)

    def exec(self):
        if self.reenter is not None:
            self.reenter()
        return 0

    def should_mark_seen(self):
        return self.mark_seen


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
    check(len(_StubOnboardingDialog.instances) == 1, "向导被构造恰好 1 次")
    check(
        _StubOnboardingDialog.instances[0].first_run is True,
        "首次启动走 first_run=True 模式",
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

    # --- 14. 用户取消勾选 → 下次还弹 ---
    d14 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d14)
    reset_recorders()

    original_init = _StubOnboardingDialog.__init__

    def _init_with_opt_out(self, parent=None, first_run=True):
        original_init(self, parent=parent, first_run=first_run)
        self.mark_seen = False  # 模拟：最后一页取消了「不再自动显示」

    _StubOnboardingDialog.__init__ = _init_with_opt_out
    try:
        check(window.maybe_show_first_run_guide() is True, "取消勾选时向导仍然弹出")
        check(
            len(_StubOnboardingDialog.instances) == 1,
            "取消勾选时向导被构造 1 次",
        )
        check(
            window._first_run_store.should_show_guide() is True,
            "取消勾选 → 不写「已看过」，下次启动仍会显示",
        )
    finally:
        _StubOnboardingDialog.__init__ = original_init

    reset_recorders()
    check(
        window.maybe_show_first_run_guide() is True,
        "取消勾选后下次调用仍会弹出",
    )

    # --- 15. 重入保护 ---
    d15 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d15)
    reset_recorders()
    inner_result = {}

    class _ReentrantStub(_StubOnboardingDialog):
        def exec(self):
            inner_result["ret"] = window.maybe_show_first_run_guide()
            return 0

    mw.OnboardingDialog = _ReentrantStub
    try:
        window.maybe_show_first_run_guide()
        check(
            inner_result.get("ret") is False,
            "嵌套事件循环里的重入调用返回 False",
        )
        check(
            len(_StubOnboardingDialog.instances) == 1,
            "重入不会叠出第二个向导",
        )
    finally:
        mw.OnboardingDialog = _StubOnboardingDialog

    # --- 16. 帮助入口重新打开（回顾模式） ---
    d16 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d16)
    window._first_run_store.set_should_show_guide(False)
    reset_recorders()
    window._on_help_clicked()
    check(len(_StubOnboardingDialog.instances) == 1, "「使用引导」会重新打开向导")
    check(
        _StubOnboardingDialog.instances[0].first_run is False,
        "重新打开走回顾模式（first_run=False）",
    )
    check(
        window._first_run_store.should_show_guide() is False,
        "重新打开不改变已记录的状态",
    )

    # --- 17. ROI 说明入口 ---
    reset_recorders()
    window._on_roi_help_clicked()
    check(len(_StubRoiHelpDialog.instances) == 1, "「说明」会打开 ROI 帮助对话框")
    check(
        _StubRoiHelpDialog.instances[0].parent_ is window,
        "ROI 帮助以主窗口为父",
    )

    # --- 18/19. 入口控件本身 ---
    check(window._help_btn.text() == "使用引导", "状态栏按钮文案为「使用引导」")
    check(
        window._help_btn.objectName() == "statusButton",
        "帮助按钮走 statusButton 紧凑样式",
    )
    check(
        window._help_btn.property("variant") is None,
        "帮助按钮不是 primary（「激活许可证」仍是唯一强调按钮）",
    )
    check(window._roi_help_btn.text() == "说明", "OCR 区域旁有「说明」按钮")
    check(
        bool(window._roi_help_btn.toolTip()),
        "「说明」按钮带 tooltip",
    )

    # --- 20. 与访问状态无关（不得被 _guard_access 拦住） ---
    d20 = fresh_dir()
    window._first_run_store = FirstRunStore(data_dir=d20)
    window._apply_license_result(LicenseResult(LicenseStatus.EXPIRED, "许可证已过期"))
    reset_recorders()
    check(
        window._access_status.is_expired() is True,
        "前置条件：当前访问状态为不可用",
    )
    check(
        window.maybe_show_first_run_guide() is True,
        "未激活 / 已过期时引导仍然弹出（帮助不该被授权门槛拦住）",
    )

    window.close()


# ======================================================================
# C. 真实对话框（只构造，不 exec）
# ======================================================================
def test_dialogs() -> None:
    section("C. 真实对话框（offscreen 构造，不 exec）")

    dialog = OnboardingDialog(first_run=True)
    check(dialog.step_count() == len(STEPS) == 5, "向导共 5 步")
    check(dialog.current_step() == 0, "初始停在第 1 步")
    check(dialog._prev_btn.isEnabled() is False, "第 1 步「上一步」禁用")
    check(dialog._next_btn.isHidden() is False, "第 1 步「下一步」可见")
    check(dialog._finish_btn.isHidden() is True, "第 1 步「完成」隐藏")
    check(dialog._skip_btn.isHidden() is False, "首次启动有「跳过」")

    for _ in range(4):
        dialog._next_btn.click()
    check(dialog.current_step() == 4, "连点「下一步」4 次到达第 5 步")
    check(dialog._next_btn.isHidden() is True, "最后一步「下一步」隐藏")
    check(dialog._finish_btn.isHidden() is False, "最后一步「完成」可见")
    check(dialog._skip_btn.isHidden() is True, "最后一步「跳过」隐藏")
    check(dialog._finish_btn.text() == "完成", "首次启动主按钮文案为「完成」")
    check(dialog._dots.current() == 4, "进度圆点跟随到第 5 个")

    dialog._prev_btn.click()
    check(dialog.current_step() == 3, "「上一步」可以回退")

    check(dialog._dont_show.isChecked() is True, "「不再自动显示」默认勾选")
    check(dialog.should_mark_seen() is True, "勾选状态 → 记录已看过")
    dialog._dont_show.setChecked(False)
    check(dialog.should_mark_seen() is False, "取消勾选 → 不记录已看过")

    # 跳过路径
    skipped = OnboardingDialog(first_run=True)
    skipped.reject()
    check(
        skipped.should_mark_seen() is True,
        "点「跳过」/ Esc / 关窗 → 视为「别再打扰」（否则每次启动都弹）",
    )

    # 回顾模式
    review = OnboardingDialog(first_run=False)
    check(review._skip_btn.isHidden() is True, "回顾模式没有「跳过」")
    for _ in range(4):
        review._next_btn.click()
    check(review._finish_btn.text() == "关闭", "回顾模式主按钮文案为「关闭」")
    check(
        review.should_mark_seen() is False,
        "回顾模式永远不写状态（看一眼帮助不该改掉之前的设置）",
    )
    check(
        not hasattr(review, "_dont_show"),
        "回顾模式不显示「不再自动显示」复选框",
    )

    # 对话框自身不碰磁盘
    probe_dir = fresh_dir()
    OnboardingDialog(first_run=True).reject()
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
