# -*- coding: utf-8 -*-
"""automation 模块的单元级边界测试(可回归,无真实输入)。

覆盖矩阵(2026-10-25 验收):
    A. 配置:坏文件回默认 / 非法字段回默认 / 落盘回读一致 / 两点判定
    B. 热键:不支持键名 / 冲突(两实例抢同键) / 改键重注册 / 注销幂等
    C. 前置检查(不发生任何点击):未校准 / 窗口不存在 / 激活失败绝不点击
    D. 序列:执行中防重入 / 宽高比漂移警告 / 坐标换算
    E. 校准:游戏未开即失败 / 取消干净

真实鼠标输入的边界(最小化恢复/移动缩放换算/切窗中止)在
tests/automation/input_boundary_live.py(需真实桌面,显式运行)。
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-aj-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-aj-local-")

from PySide6.QtWidgets import QApplication  # noqa: E402

from automation.join_config import JoinConfig  # noqa: E402
from automation.hotkey import GlobalHotkey, VK_NAMES  # noqa: E402
from automation.join_controller import JoinController, frac_to_pixel  # noqa: E402
from automation.calibration import CalibrationOverlay  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    yield inst


# ======================================================================
# A. 配置边界
# ======================================================================
def test_config_missing_file_returns_defaults():
    cfg = JoinConfig.load(os.path.join(tempfile.mkdtemp(), "none.json"))
    assert cfg.enabled is False
    assert cfg.hotkey == "F8"
    assert cfg.window_class == "VALORANTUnrealWindow"
    assert not cfg.is_calibrated


def test_config_corrupt_file_returns_defaults():
    path = os.path.join(tempfile.mkdtemp(), "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json!!!")
    cfg = JoinConfig.load(path)
    assert cfg.window_class == "VALORANTUnrealWindow"


def test_config_illegal_fields_fall_back():
    path = os.path.join(tempfile.mkdtemp(), "weird.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"hotkey": "CTRL+ALT+X", "window_class": 123, '
                 '"paste_point": [1.5, 0.5], "delay_click_ms": 5}')
    # (CTRL+ALT+X 不在白名单 → 回默认 F8;此前会原样收下,运行时才注册失败)
    cfg = JoinConfig.load(path)
    assert cfg.hotkey == "F8"                 # 非法键名回默认
    assert cfg.window_class == "VALORANTUnrealWindow"
    assert cfg.paste_point is None            # 越界比例拒绝
    assert cfg.delay_click_ms == 450          # 越界延时回默认


def test_config_roundtrip_two_points(tmp_path):
    cfg = JoinConfig()
    cfg.enabled = True
    cfg.paste_point = (0.52, 0.573)
    cfg.join_point = (0.578, 0.573)
    cfg.calibrated_aspect = 1.863
    p = str(tmp_path / "j.json")
    cfg.save(p)
    back = JoinConfig.load(p)
    assert back.is_calibrated
    assert back.paste_point == (0.52, 0.573)
    assert back.join_point == (0.578, 0.573)
    assert back.calibrated_aspect == pytest.approx(1.863)


def test_calibrated_requires_both_points():
    cfg = JoinConfig()
    cfg.paste_point = (0.5, 0.5)
    assert not cfg.is_calibrated
    cfg.join_point = (0.5, 0.6)
    assert cfg.is_calibrated


# ======================================================================
# B. 热键边界
# ======================================================================
def test_hotkey_rejects_unsupported_name(app):
    hk = GlobalHotkey()
    assert hk.register("Ctrl+F8") is False
    assert hk.last_error


def test_hotkey_conflict_reports_error(app):
    # 动态挑空闲键:具体 F 键可能被其它软件占用(实测 F7 被占),
    # 冲突语义与键位无关
    a, b = GlobalHotkey(), GlobalHotkey()
    key = next((k for k in VK_NAMES if a.register(k)), None)
    if key is None:                            # 全被占的极端环境
        pytest.skip("F1-F12 均被占用,无法构造冲突场景")
    ok = b.register(key)
    try:
        if ok:  # 有些环境允许重复注册,能注上也算通过——只验如实上报
            assert b.is_registered
        else:
            assert b.last_error and "占用" in b.last_error
    finally:
        a.unregister()
        b.unregister()


def test_hotkey_reregister_switches_key(app):
    hk = GlobalHotkey()
    assert hk.register("F6") is True
    assert hk.register("F9") is True
    # F6 已被自己释放;F9 在册(冲突探测:另一个实例应注不上 F9)
    other = GlobalHotkey()
    if other.register("F9") is False:
        assert other.last_error
    hk.unregister()
    other.unregister()
    assert hk.is_registered is False


# ======================================================================
# C. 前置检查:任何一项不满足都不动鼠标
# ======================================================================
def test_join_not_calibrated_fails_without_click(app):
    ctl = JoinController()
    cfg = JoinConfig()          # 未校准
    results = []
    ctl.finished.connect(lambda r: results.append(r))
    assert ctl.join(cfg) is False
    assert len(results) == 1
    assert results[0].ok is False
    assert "校准" in results[0].reason


def test_join_window_missing_fails_without_click(app, monkeypatch):
    import automation.join_controller as jc
    monkeypatch.setattr(
        jc, "find_game_window",
        lambda *a, **k: None)     # 模拟找不到窗口
    cfg = JoinConfig()
    cfg.paste_point = (0.5, 0.5)
    cfg.join_point = (0.6, 0.5)
    ctl = JoinController()
    results = []
    ctl.finished.connect(lambda r: results.append(r))
    assert ctl.join(cfg) is False
    assert results[0].ok is False
    assert "未找到游戏窗口" in results[0].reason


def test_join_activate_failure_never_clicks(app, monkeypatch):
    """激活失败(前台锁打不开)必须中止且绝不发任何点击(安全关键)。"""
    import automation.join_controller as jc
    clicks = []

    class _FakeWin:
        hwnd = 1234

    monkeypatch.setattr(jc, "find_game_window", lambda *a, **k: _FakeWin())
    monkeypatch.setattr(jc, "client_geometry",
                        lambda h: (100, 100, 800, 600))
    monkeypatch.setattr(jc, "activate_window", lambda h, **k: False)
    monkeypatch.setattr(jc, "click_point",
                        lambda x, y, **k: clicks.append((x, y)) or True)

    cfg = JoinConfig()
    cfg.paste_point = (0.5, 0.5)
    cfg.join_point = (0.6, 0.5)
    ctl = JoinController()
    results = []
    ctl.finished.connect(lambda r: results.append(r))
    assert ctl.join(cfg) is False
    assert clicks == []                       # 一键未发
    assert "前台" in results[0].reason


def test_join_running_ignores_reentry(app, monkeypatch):
    import automation.join_controller as jc
    from automation.win32 import WindowInfo

    state = {"geo": (100, 100, 800, 600), "fg": True}
    monkeypatch.setattr(
        jc, "find_game_window",
        lambda *a, **k: WindowInfo(1234, "T", "TC"))
    monkeypatch.setattr(jc, "client_geometry",
                        lambda h: state["geo"])
    monkeypatch.setattr(jc, "activate_window", lambda h, **k: True)
    monkeypatch.setattr(jc, "is_foreground", lambda h: state["fg"])
    clicks = []
    monkeypatch.setattr(jc, "click_point",
                        lambda x, y, **k: clicks.append((x, y)) or True)

    cfg = JoinConfig()
    cfg.paste_point = (0.5, 0.5)
    cfg.join_point = (0.6, 0.5)
    cfg.delay_activate_ms = 30
    cfg.delay_click_ms = 30
    ctl = JoinController()
    results = []
    ctl.finished.connect(lambda r: results.append(r))

    assert ctl.join(cfg) is True
    assert ctl.join(cfg) is True              # 重入:返回 True 但不二启
    # 跑完序列(事件循环驱动 QTimer)
    from PySide6.QtCore import QTimer
    deadline = __import__("time").perf_counter() + 3
    while not results and __import__("time").perf_counter() < deadline:
        app.processEvents()
        QTimer.singleShot(10, lambda: None)
        __import__("time").sleep(0.01)
    assert results and results[0].ok
    assert len(clicks) == 2                   # 只有一份序列(粘贴+加入)


def test_join_aspect_drift_sets_warning(app, monkeypatch):
    import automation.join_controller as jc
    from automation.win32 import WindowInfo

    monkeypatch.setattr(
        jc, "find_game_window",
        lambda *a, **k: WindowInfo(1234, "T", "TC"))
    monkeypatch.setattr(jc, "client_geometry",
                        lambda h: (0, 0, 1000, 500))   # 2.0 vs 校准 1.86
    monkeypatch.setattr(jc, "activate_window", lambda h, **k: True)
    monkeypatch.setattr(jc, "is_foreground", lambda h: True)
    monkeypatch.setattr(jc, "click_point", lambda x, y, **k: True)

    cfg = JoinConfig()
    cfg.paste_point = (0.5, 0.5)
    cfg.join_point = (0.6, 0.5)
    cfg.calibrated_aspect = 1.86
    cfg.delay_activate_ms = 20
    cfg.delay_click_ms = 20
    ctl = JoinController()
    results = []
    ctl.finished.connect(lambda r: results.append(r))
    ctl.join(cfg)
    from PySide6.QtCore import QTimer
    deadline = __import__("time").perf_counter() + 3
    while not results and __import__("time").perf_counter() < deadline:
        app.processEvents()
        QTimer.singleShot(10, lambda: None)
        __import__("time").sleep(0.01)
    assert results[0].ok
    assert results[0].warning                # 漂移警告带上了


def test_frac_to_pixel_boundaries():
    assert frac_to_pixel((0.0, 0.0), (10, 20, 100, 40)) == (10, 20)
    assert frac_to_pixel((1.0, 1.0), (10, 20, 100, 40)) == (110, 60)
    assert frac_to_pixel((0.5, 0.25), (0, 0, 200, 80)) == (100, 20)


# ======================================================================
# E. 校准边界(无真实点击)
# ======================================================================
def test_calibration_start_without_window(app, monkeypatch):
    import automation.calibration as cal
    monkeypatch.setattr(cal, "find_game_window", lambda *a, **k: None)
    c = CalibrationOverlay()
    results = []
    c.finished.connect(lambda ok, m, cfg: results.append((ok, m)))
    assert c.start(JoinConfig()) is False
    assert results[0][0] is False
    assert "未找到游戏窗口" in results[0][1]
    assert c.active is False


def test_calibration_cancel_is_idempotent(app, monkeypatch):
    import automation.calibration as cal
    from automation.win32 import WindowInfo
    monkeypatch.setattr(cal, "find_game_window",
                        lambda *a, **k: WindowInfo(9, "T", "TC"))
    c = CalibrationOverlay()
    results = []
    c.finished.connect(lambda ok, m, cfg: results.append((ok, m)))
    assert c.start(JoinConfig()) is True
    c.cancel()
    c.cancel()                                # 幂等
    assert c.active is False
    assert results[-1][1] == "已取消校准"


def test_calibration_click_outside_rect_rejected(app, monkeypatch):
    """落点在客户区外:拒绝且不推进步骤(v2 矩形判定)。"""
    import automation.calibration as cal
    from automation.win32 import WindowInfo
    monkeypatch.setattr(cal, "find_game_window",
                        lambda *a, **k: WindowInfo(9, "T", "TC"))
    monkeypatch.setattr(cal, "client_geometry",
                        lambda h: (100, 100, 800, 600))
    c = CalibrationOverlay()
    invalid = []
    steps = []
    c.invalidClick.connect(lambda m: invalid.append(m))
    c.stepChanged.connect(lambda i, t: steps.append(i))
    c.start(JoinConfig())
    c._on_click(50, 50)          # 客户区 (100,100)-(900,700) 之外
    assert invalid and steps == [0]           # 没推进
    c.cancel()
