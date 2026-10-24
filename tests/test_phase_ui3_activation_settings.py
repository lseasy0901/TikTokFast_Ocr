#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase UI-3 —— 统一激活弹窗 / 未激活遮罩 / 设置中心 验证脚本

覆盖：
    A. dispatch_activation 的分派正确性（唯一分派入口，且**没有**跨路径回退）
    B. ActivationDialog 的输入页 / 结果页状态机与失败保留输入
    C. _ActivationOverlay：可点击性（回归）、可见性跟随授权状态、呼吸动画启停
    D. SettingsDialog：导航、真实状态展示、追加授权回调
    E. theme.apply_dialog_acrylic 的回退语义（失败即完全不透明、不改动窗口）

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_phase_ui3_activation_settings.py`` 运行，不依赖 pytest。

关键隔离：APPDATA / LOCALAPPDATA 指向临时目录，测试绝不写用户真实配置。
网络与子进程一律打桩 —— 本脚本不做任何真实在线激活。
"""

import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 必须在导入 utils.license_store / gui.main_window 之前设置：
# default_data_dir() 在 LicenseStore 构造时读取这些变量。
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ui3-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-ui3-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from gui import theme  # noqa: E402
import gui.main_window as mw  # noqa: E402
import gui.settings_dialog as sd  # noqa: E402
from gui.activation_dialog import (  # noqa: E402
    ActivationDialog,
    dispatch_activation,
)
from gui.settings_dialog import SettingsDialog  # noqa: E402
from utils.license_manager import (  # noqa: E402
    LicenseManager,
    LicenseResult,
    LicenseStatus,
)
from utils.offline_activation import OFFLINE_KEYS, normalize_key  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply_theme(app)

# ======================================================================
# 测试脚手架
# ======================================================================
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
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # pytest 下必须真抛断言：check 只打印不抛错，失败会被 pytest 的
        # "5 passed" 静默吞掉（偏好页加入后导航断言静默失败的教训）。
        raise AssertionError(message)
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def pump(rounds: int = 60, delay: float = 0.005) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(delay)


def wait_until(predicate, rounds: int = 400, delay: float = 0.005) -> bool:
    for _ in range(rounds):
        app.processEvents()
        if predicate():
            return True
        time.sleep(delay)
    app.processEvents()
    return predicate()


#: 一个真实存在的离线 KEY（规范化后）
OFFLINE_KEY = normalize_key(sorted(OFFLINE_KEYS)[0])
#: 保证不在离线池里的在线 KEY
ONLINE_KEY = "ZZZZ-9999-YYYY-0000"


class _RecordingManager:
    """记录走了哪条激活路径的假管理器；两条路径都可指定返回值。"""

    def __init__(self, offline_result=None, online_result=None):
        self.calls = []
        # 真实 LicenseManager 有这个属性（弹窗据此展示当前状态）
        self.last_result = None
        self._offline_result = offline_result or LicenseResult(
            LicenseStatus.ACTIVATED, "离线激活成功")
        self._online_result = online_result or LicenseResult(
            LicenseStatus.ACTIVATED, "在线激活成功")

    def activate_offline(self, key):
        self.calls.append(("offline", key))
        return self._offline_result

    def activate(self, key):
        self.calls.append(("online", key))
        return self._online_result


# ======================================================================
# A. 分派规则
# ======================================================================
def test_dispatch() -> None:
    section("A. dispatch_activation 分派")

    check(OFFLINE_KEY in OFFLINE_KEYS, "取到的样本 KEY 确实在离线池里")

    # A1. 离线 KEY → 只走离线，绝不碰在线
    mgr = _RecordingManager()
    result = dispatch_activation(mgr, OFFLINE_KEY)
    check([c[0] for c in mgr.calls] == ["offline"],
          "离线 KEY 只调用 activate_offline（未被在线路径触碰）")
    check(result.is_activated, "离线 KEY 返回已激活")

    # A2. 带分隔符 / 小写的离线 KEY 也要命中（规范化后判定）
    formatted = "-".join(OFFLINE_KEY[i:i + 4] for i in range(0, len(OFFLINE_KEY), 4))
    mgr = _RecordingManager()
    dispatch_activation(mgr, formatted.lower())
    check([c[0] for c in mgr.calls] == ["offline"],
          "带 - 且小写的离线 KEY 仍判定为离线")

    # A3. 非池内 KEY → 只走在线
    mgr = _RecordingManager()
    dispatch_activation(mgr, ONLINE_KEY)
    check([c[0] for c in mgr.calls] == ["online"],
          "非离线池 KEY 只调用在线 activate")

    # A4. 关键不变量：选中的那条路失败后【不会】回退去试另一条
    mgr = _RecordingManager(
        online_result=LicenseResult(LicenseStatus.INVALID, "密钥无效"))
    result = dispatch_activation(mgr, ONLINE_KEY)
    check([c[0] for c in mgr.calls] == ["online"],
          "在线失败后没有回退到离线路径")
    check(not result.is_activated, "在线失败如实返回失败")

    mgr = _RecordingManager(
        offline_result=LicenseResult(LicenseStatus.INVALID, "无效的离线 KEY"))
    result = dispatch_activation(mgr, OFFLINE_KEY)
    check([c[0] for c in mgr.calls] == ["offline"],
          "离线失败后没有回退到在线路径")
    check(not result.is_activated, "离线失败如实返回失败")


# ======================================================================
# B. 激活弹窗
# ======================================================================
def test_activation_dialog() -> None:
    section("B. ActivationDialog")

    # B1. 空输入：就地报错，不起后台任务
    dialog = ActivationDialog(_RecordingManager())
    dialog._on_activate_clicked()
    check(dialog._error_label.text() != "", "空输入即时报错")
    check(dialog._worker is None, "空输入不启动后台任务")
    check(dialog._stack.currentIndex() == 0, "空输入仍停留在输入页")

    # B2. 失败：留在输入页，且保留用户输入
    mgr = _RecordingManager(
        online_result=LicenseResult(LicenseStatus.INVALID, "密钥无效"))
    dialog = ActivationDialog(mgr)
    dialog._key_input.setText(ONLINE_KEY)
    dialog._on_activate_clicked()
    ok = wait_until(lambda: not dialog._worker.is_running())
    check(ok, "失败用例的后台任务已结束")
    pump(20)
    check(dialog._stack.currentIndex() == 0, "失败后仍停留在输入页")
    check(dialog._key_input.text() == ONLINE_KEY, "失败后保留用户输入便于修正")
    check(dialog._key_input.isEnabled(), "失败后输入框重新可用")
    check(dialog._activate_btn.text() == "立即激活", "失败后按钮文案复位")
    check(dialog.result_data() is None, "失败时 result_data() 为 None（不改本地状态）")
    check("密钥无效" in dialog._error_label.text(), "失败原因展示在行内")

    # B3. 成功：切结果页，展示真实凭据并给出「开始使用」
    ok_result = LicenseResult(LicenseStatus.ACTIVATED, "已激活")
    mgr = _RecordingManager(offline_result=ok_result)
    dialog = ActivationDialog(mgr)
    dialog._key_input.setText(OFFLINE_KEY)
    dialog._on_activate_clicked()
    wait_until(lambda: dialog._stack.currentIndex() == 1)
    pump(20)
    check(dialog._stack.currentIndex() == 1, "成功后切换到结果页")
    check(dialog.result_data() is ok_result, "result_data() 返回真实结果对象")
    check(dialog._ok_label.text() == "已激活", "结果页展示真实状态文案")
    check(dialog._start_btn.text() == "开始使用", "结果页提供「开始使用」")
    check(dialog._activate_btn.text() == "立即激活", "成功路径不残留 busy 文案")

    # B4. 结果页点击「开始使用」→ accept（调用方据此收起遮罩）
    dialog._start_btn.click()
    check(dialog.result() == int(ActivationDialog.DialogCode.Accepted),
          "「开始使用」以 Accepted 关闭弹窗")

    # B5. 未提供 features 时不虚构功能权限文案
    check(dialog._features_label.text() == "", "无 features 时权限行保持为空")


# ======================================================================
# C. 未激活遮罩
# ======================================================================
class _StubActivationDialog:
    """替身：只记录「被谁打开」，其 exec() 不是模态的。

    真实 ActivationDialog.exec() 会永久阻塞无头环境（本仓库
    test_first_run_onboarding.py 对同样问题的处理方式）。
    """

    instances = []

    def __init__(self, manager, parent=None):
        self.manager = manager
        self.parent_ = parent
        self._result = None
        _StubActivationDialog.instances.append(self)

    def exec(self):
        # 忠实模拟真实路径：真正的 activate()/activate_offline() 会把结果写回
        # manager.last_result，调用方（设置中心）正是靠它刷新页面。
        if self._result is not None and hasattr(self.manager, "last_result"):
            self.manager.last_result = self._result
        return 0

    def result_data(self):
        return self._result


def build_window():
    """构造真实 MainWindow（离线 schema，不起任何网络请求）。"""
    window = mw.MainWindow()
    window.show()
    wait_until(
        lambda: window._license_load_worker is not None
        and not window._license_load_worker.is_running(),
        rounds=600)
    pump(20)
    return window


def test_activation_overlay() -> None:
    section("C. 未激活遮罩")

    # 必须在构造 MainWindow 之前打桩：遮罩按钮 → 模态弹窗会永久阻塞无头环境
    real_activation = mw.ActivationDialog
    mw.ActivationDialog = _StubActivationDialog
    try:
        window = build_window()
    except Exception:
        mw.ActivationDialog = real_activation
        raise
    overlay = window._activation_overlay

    # C1. 回归：遮罩上的「立即激活」必须真的能被点到。
    #     OverlayHost.add_overlay 的历史实现无条件设了
    #     WA_TransparentForMouseEvents，该属性按 Qt 语义会连子控件一起作废，
    #     导致按钮永远收不到点击。这里用命中测试锁死这个行为。
    window._access_status.set_license_inactive("未激活")
    pump(20)
    check(overlay.isVisible(), "未激活时遮罩可见")

    btn = overlay._activate_btn
    center = btn.mapToGlobal(QPoint(btn.width() // 2, btn.height() // 2))
    hit = app.widgetAt(center)
    check(hit is btn or (hit is not None and btn.isAncestorOf(hit)),
          "遮罩上的「立即激活」按钮是命中目标（可点击回归）")
    check(not overlay.testAttribute(Qt.WA_TransparentForMouseEvents),
          "遮罩本身未开启鼠标穿透")

    # C2. 遮罩按钮与标题栏「激活许可证」走的是【同一个】统一弹窗
    _StubActivationDialog.instances.clear()
    btn.click()  # 真实点击：overlay → MainWindow._on_license_clicked
    pump(20)
    check(len(_StubActivationDialog.instances) == 1,
          "点击遮罩按钮打开统一激活弹窗（与标题栏入口同一条路径）")
    check(
        _StubActivationDialog.instances
        and _StubActivationDialog.instances[0].manager is window._license_manager,
        "弹窗拿到的是主窗口的许可证管理器（没有另起一套授权状态）")

    _StubActivationDialog.instances.clear()
    window._license_btn.click()
    pump(20)
    check(len(_StubActivationDialog.instances) == 1,
          "标题栏「激活许可证」打开同一个统一弹窗")

    # C2b. 标题栏「设置」入口打开设置中心（同样避开模态）
    settings_opened = []
    real_settings = mw.SettingsDialog
    mw.SettingsDialog = lambda **kw: settings_opened.append(kw) or _StubActivationDialog(
        kw.get("manager"))
    try:
        window._settings_btn.click()
        pump(20)
    finally:
        mw.SettingsDialog = real_settings
    check(len(settings_opened) == 1, "标题栏「设置」打开设置中心")
    check(settings_opened and settings_opened[0].get("manager")
          is window._license_manager, "设置中心拿到同一个许可证管理器")
    check(settings_opened and settings_opened[0].get("version"),
          "设置中心拿到真实版本号")

    # C3. 呼吸动画：可见时运行，隐藏即停（不引入常驻定时器）
    check(overlay._anim.state().name == "Running", "遮罩可见时呼吸动画在运行")
    window._access_status.set_license_active(None)
    pump(20)
    check(not overlay.isVisible(), "已有有效授权时遮罩隐藏")
    check(overlay._anim.state().name != "Running", "遮罩隐藏后呼吸动画已停止")

    # C4. 解锁过渡：切成「已激活」文案并收起激活按钮，随后自动回到普通预览
    window._play_unlock_transition()
    pump(10)
    check(overlay.isVisible(), "解锁过渡期间遮罩可见")
    check(overlay._prompt.text() == "已激活", "过渡文案为「已激活」")
    check(not overlay._activate_btn.isVisible(), "过渡期间隐藏激活按钮")

    window._finish_unlock_transition()  # 直接快进，避免依赖 1.4s 真实计时
    pump(20)
    check(not overlay.isVisible(), "过渡结束后遮罩收起")

    # C5. 再次进入未激活：文案复位，按钮重新出现
    window._access_status.clear_license_state()
    pump(20)
    check(overlay.isVisible(), "再次未激活时遮罩重新显示")
    check(overlay._prompt.text() == "请激活 LiveLens", "文案复位为未激活")
    check(overlay._activate_btn.isVisible(), "激活按钮重新出现")

    window.close()
    pump(10)
    mw.ActivationDialog = real_activation


# ======================================================================
# D. 设置中心
# ======================================================================
def test_settings_dialog() -> None:
    section("D. SettingsDialog")

    manager = LicenseManager()
    dialog = SettingsDialog(manager, version="9.9.9-test")

    # D1. 导航只放真实存在的分类，且切换联动内容页
    labels = [dialog._nav.item(i).text() for i in range(dialog._nav.count())]
    check(labels == ["授权", "偏好", "自动加入", "意见反馈", "关于"],
          f"导航分类为 {labels}")
    dialog._nav.setCurrentRow(4)
    check(dialog._stack.currentIndex() == 4, "切到「关于」时内容页联动")
    # D1c. 关于页官网行:标签不再误传 URL,动作是浏览器打开
    dialog._nav.setCurrentRow(4)
    about = dialog._stack.currentWidget()
    about_btns = [b for b in about.findChildren(QPushButton)
                  if b.text() == "打开"]
    check(len(about_btns) == 1, "官网行动作是「打开」按钮")
    from utils.update_source import DEFAULT_UPDATE_SOURCE as _DUS
    from urllib.parse import urlsplit as _split
    _site = f"{_split(_DUS).scheme}://{_split(_DUS).netloc}"
    about_labels = [q.text() for q in about.findChildren(QLabel)]
    check(_site in about_labels and about_labels.count(_site) == 1,
          f"官网 URL 只显示一次：{about_labels}")
    # 不真点「打开」:offscreen 下 openUrl 必失败,open_external_url 会弹
    # 模态 QMessageBox,无人关闭导致测试挂死;行为已由按钮语义+tooltip 覆盖,
    # 打开动作留给真机验收。
    check("浏览器" in (about_btns[0].toolTip() or ""),
          "「打开」按钮 tooltip 说明走浏览器")
    dialog._nav.setCurrentRow(0)
    check(dialog._stack.currentIndex() == 0, "切回「授权」时内容页联动")

    # D1b. 自动加入页:开关/热键/校准入口存在
    dialog._nav.setCurrentRow(2)
    check(dialog._aj_enabled.text().startswith("启用"), "自动加入页有启用开关")
    check(dialog._aj_hotkey.currentText() == "F8", "默认热键 F8")
    check(dialog._aj_cal_btn.text().startswith("开始校准"), "有校准入口")

    # D1b2. 意见反馈页:邮箱 / QQ 群与复制动作
    dialog._nav.setCurrentRow(3)
    page = dialog._stack.currentWidget()
    texts = [q.text() for q in page.findChildren(QLabel)]
    check("1752924057@qq.com" in texts, f"反馈页包含邮箱 {texts}")
    check("1035921366" in texts, "反馈页包含 QQ 群号")
    copy_btns = page.findChildren(QPushButton, name="fbCopyBtn")         if hasattr(page.findChildren(QPushButton, name="fbCopyBtn"), "__len__")         else page.findChildren(QPushButton)
    copy_btns = [b for b in page.findChildren(QPushButton)
                 if b.objectName() == "fbCopyBtn"]
    check(len(copy_btns) == 2, f"两张反馈卡各有一个复制按钮，实际 {len(copy_btns)}")
    from PySide6.QtWidgets import QApplication as _QA
    _QA.clipboard().setText("sentinel")
    copy_btns[0].click()
    check(_QA.clipboard().text() == "1752924057@qq.com",
          "点复制后剪贴板为邮箱")
    dialog._nav.setCurrentRow(0)

    # D2. 授权页展示真实状态：没有凭据就直说未激活
    manager.last_result = None
    dialog._refresh_license_page()
    check(dialog._license_state.text() == "未激活", "无凭据时授权页显示未激活")
    check(dialog._license_expiry.text() == "无有效授权", "无凭据时不虚构有效期")

    # D3. 有签名凭据时展示凭据里的真实到期时间（客户端不做时长计算）
    from datetime import datetime, timedelta, timezone
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    manager.last_result = LicenseResult(
        LicenseStatus.ACTIVATED, "已激活", expires_at=expires)
    dialog._refresh_license_page()
    expected = expires.astimezone().strftime("%Y-%m-%d %H:%M")
    check(dialog._license_state.text() == "已激活", "有凭据时展示真实状态")
    check(dialog._license_expiry.text() == "至 " + expected,
          "有效期来自凭据里的 expires")

    # D4. 追加授权：复用统一激活弹窗，成功把结果交回主窗口并就地刷新
    appended = []

    class _AppendOkDialog(_StubActivationDialog):
        def __init__(self, manager_, parent=None):
            super().__init__(manager_, parent)
            self._result = LicenseResult(
                LicenseStatus.ACTIVATED, "已激活",
                expires_at=expires + timedelta(days=30))

    real = sd.ActivationDialog
    sd.ActivationDialog = _AppendOkDialog
    try:
        dialog = SettingsDialog(manager, version="9.9.9-test",
                                append_license_callback=appended.append)
        dialog._on_append_license()
    finally:
        sd.ActivationDialog = real

    check(len(appended) == 1, "追加成功后回调把结果交回主窗口")
    check(appended[0].expires_at == expires + timedelta(days=30),
          "回调拿到的是真实结果对象")
    check(dialog._license_expiry.text()
          == "至 " + (expires + timedelta(days=30)).astimezone().strftime(
              "%Y-%m-%d %H:%M"),
          "追加后授权页就地刷新为新有效期")

    # D5. 用户取消（result_data() 为 None）时不触发回调
    appended.clear()

    class _CancelActivationDialog(_StubActivationDialog):
        """用户点了「稍后再说」：exec() 正常返回，但没有结果。"""

        def __init__(self, manager_, parent=None):
            super().__init__(manager_, parent)
            self._result = None

    sd.ActivationDialog = _CancelActivationDialog
    try:
        dialog._on_append_license()
    finally:
        sd.ActivationDialog = real
    check(appended == [], "取消追加授权时不触发回调")

    # D6. 官网地址来自既有配置，不新编造域名
    check(sd.website_url() == "https://livelen.icu",
          f"官网地址由更新源推导：{sd.website_url()}")

    dialog.close()
    pump(10)


# ======================================================================
# E. Acrylic 回退
# ======================================================================
def test_glass_dialog_design() -> None:
    """玻璃卡对话框设计契约（替代已移除的 apply_dialog_acrylic 测试）。

    GlassDialog 不再启用窗口级 Acrylic（投影边距会露出发灰白的系统材质），
    改为：无边框 + 半透明窗口 + 近不透明卡片 + 投影。
    """
    section("E. GlassDialog 设计契约")

    dialog = ActivationDialog(_RecordingManager())
    check(dialog.windowFlags() & Qt.FramelessWindowHint != 0,
          "激活卡是无边框窗口")
    check(bool(dialog.testAttribute(Qt.WA_TranslucentBackground)),
          "窗口半透明（圆角与投影需要）")
    check(dialog._card.styleSheet() == "" or "glassCard" not in dialog._card.styleSheet(),
          "卡片样式来自对话框级 QSS（控件级未覆盖）")
    from gui.widgets import glass_dialog_qss
    qss = glass_dialog_qss()
    check("#glassCard" in qss and "acrylic" not in qss,
          "卡片 QSS 不含 Acrylic 规则（灰白图层根因已移除）")
    from gui.theme import Palette as _P
    check(_P.POPUP_BG in qss, "卡片底色取 POPUP_BG（近不透明实底）")
    dialog.close()

def main() -> int:
    test_dispatch()
    test_activation_dialog()
    test_activation_overlay()
    test_settings_dialog()
    test_glass_dialog_design()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
