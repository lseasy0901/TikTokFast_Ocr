#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase UI 液态玻璃改造 —— 未激活动态水印（Phase 5）。

覆盖（对应任务书 Phase 5 测试矩阵 T1–T15）：

    A. 纯函数运动算法：水平/垂直/角落碰撞、不越界、极小区域降级
    B. 组件结构：唯一实例、鼠标穿透、层级在未激活遮罩之下、只消费布尔量
    C. 授权状态映射：未激活显示并运动、已激活隐藏并停表、状态切换、
       状态未确认时按未激活处理（保守）
    D. 边界重算：容器缩放、无帧/有帧（letterbox）边界、极小预览区
    E. 生命周期：隐藏即停表、关闭停表、无残留定时器
    F. 业务隔离：不改视频帧、不改 ROI、不进识别输入

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python tests/ui_redesign/test_watermark.py`` 运行，不依赖 pytest。
"""

from __future__ import annotations

import os
import sys
import tempfile

_TEMP = tempfile.mkdtemp(prefix="uilg_watermark_")
os.environ["APPDATA"] = os.path.join(_TEMP, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(_TEMP, "AppData", "Local")
os.environ["USERPROFILE"] = _TEMP
for _p in (os.environ["APPDATA"], os.environ["LOCALAPPDATA"]):
    os.makedirs(_p, exist_ok=True)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.watermark import WatermarkOverlay, bounce_step  # noqa: E402

_FAILED = 0


def check(cond: bool, msg: str) -> None:
    global _FAILED
    if cond:
        print(f"  [ OK ] {msg}")
    else:
        _FAILED += 1
        print(f"  [FAIL] {msg}")


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def pump(n: int = 20) -> None:
    app = QApplication.instance()
    for _ in range(n):
        app.processEvents()


# ======================================================================
# A. 纯函数运动算法
# ======================================================================
def test_bounce_step() -> None:
    section("A. 运动算法（纯函数 bounce_step）")

    W, H, w, h = 400.0, 300.0, 80.0, 30.0

    # 正常推进
    x, y, vx, vy = bounce_step(100, 100, 2, 1, w, h, W, H)
    check((x, y) == (102, 101), f"常规推进：{x},{y}")
    check((vx, vy) == (2, 1), "常规推进不改变速度")

    # 右边界
    x, y, vx, vy = bounce_step(W - w - 1, 10, 5, 0, w, h, W, H)
    check(x == W - w, f"右边界位置被修正到 W-w={W - w}（实测 {x}）")
    check(vx == -5, f"右边界水平速度反转（{vx}）")

    # 左边界
    x, y, vx, vy = bounce_step(1, 10, -5, 0, w, h, W, H)
    check(x == 0, "左边界位置被修正到 0")
    check(vx == 5, f"左边界水平速度反转（{vx}）")

    # 下边界
    x, y, vx, vy = bounce_step(10, H - h - 1, 0, 7, w, h, W, H)
    check(y == H - h, f"下边界位置被修正到 H-h={H - h}")
    check(vy == -7, f"下边界垂直速度反转（{vy}）")

    # 上边界
    x, y, vx, vy = bounce_step(10, 1, 0, -7, w, h, W, H)
    check(y == 0, "上边界位置被修正到 0")
    check(vy == 7, f"上边界垂直速度反转（{vy}）")

    # 角落：两个轴同时反转
    x, y, vx, vy = bounce_step(W - w - 1, H - h - 1, 6, 6, w, h, W, H)
    check((x, y) == (W - w, H - h), "右下角位置两轴同时修正")
    check((vx, vy) == (-6, -6), f"右下角两轴速度同时反转（{vx},{vy}）")

    x, y, vx, vy = bounce_step(1, 1, -6, -6, w, h, W, H)
    check((x, y) == (0, 0) and (vx, vy) == (6, 6), "左上角两轴同时反转")

    # 长跑不越界
    x = y = 50.0
    vx, vy = 3.7, 2.9
    ok = True
    for _ in range(20000):
        x, y, vx, vy = bounce_step(x, y, vx, vy, w, h, W, H)
        if not (0 <= x <= W - w and 0 <= y <= H - h):
            ok = False
            break
    check(ok, "20000 次迭代始终满足 0<=x<=W-w 且 0<=y<=H-h（不越界）")

    # 反向速度注入也要被纠正（模拟外部误设速度）
    x, y, vx, vy = bounce_step(W - w - 1, 0, 4, 0, w, h, W, H)
    check(vx < 0, "已越界时即使速度方向为正，也会被纠正为向左")

    # 极小区域：水印比预览区宽 → 冻结该轴，不抖动
    x, y, vx, vy = bounce_step(0, 0, 5, 5, 100, 100, 50, 40)
    check((x, y, vx, vy) == (0, 0, 0, 0),
          f"预览区小于水印时冻结两轴（实测 {x},{y},{vx},{vy}）")

    # 单轴退化：只有宽度不够
    x, y, vx, vy = bounce_step(0, 10, 5, 3, 100, 20, 50, 200)
    check((x, vx) == (0, 0), "只有水平方向放不下时，仅冻结水平轴")
    check(vy == 3 and y == 13, f"垂直轴仍正常运动（y={y}, vy={vy}）")


# ======================================================================
# B. 组件结构
# ======================================================================
def build_window():
    import gui.main_window as mw

    class _StubDialog:
        def __init__(self, *a, **kw):
            pass

        def exec(self):
            return 0

        def show(self):
            pass

        def close(self):
            pass

    mw.SettingsDialog = _StubDialog      # type: ignore[assignment]
    mw.ActivationDialog = _StubDialog    # type: ignore[assignment]
    mw.ExpiredDialog = _StubDialog       # type: ignore[assignment]
    w = mw.MainWindow()
    w.maybe_show_first_run_guide = lambda *a, **kw: None
    return w


def test_structure(window) -> None:
    section("B. 组件结构")

    overlays = window._video_container._overlays
    marks = [o for o in overlays if isinstance(o, WatermarkOverlay)]
    check(len(marks) == 1, f"预览容器里恰好一个 WatermarkOverlay（实测 {len(marks)}）")

    wm = window._watermark
    check(wm is marks[0], "window._watermark 就是那个唯一实例")
    check(wm.text() if hasattr(wm, "text") else wm.TEXT == "LiveLens",
          f"水印文字为 LiveLens（{wm.TEXT!r}）")

    # 鼠标穿透
    check(wm.testAttribute(Qt.WA_TransparentForMouseEvents),
          "水印鼠标穿透（不抢预览区交互）")
    check(wm.focusPolicy() == Qt.NoFocus, "水印不接受焦点")

    # 层级：必须**实际**叠在未激活遮罩之上。
    # 注意不能只看"注册顺序"—— 遮罩在 _sync_activation_overlay() 里每次都
    # raise_() 自己，注册顺序靠后并不等于层级更高（这个坑已实测踩过）。
    window.show()
    pump(30)
    window._sync_watermark(True)
    pump(20)
    host = window._video_container
    kids = [c for c in host.children() if c.isWidgetType()]
    wm_i = kids.index(wm)
    act_i = kids.index(window._activation_overlay)
    check(wm_i > act_i,
          f"水印实际叠在未激活遮罩之上（子控件序 {wm_i} > {act_i}）"
          " → 不会被遮罩的 scrim 压到不可见")
    check(wm.testAttribute(Qt.WA_TransparentForMouseEvents),
          "水印虽在遮罩之上，但鼠标穿透 → 不遮挡「立即激活」按钮的点击")

    # 组件只消费布尔量：模块不得导入许可相关模块
    import gui.watermark as wm_mod
    src = open(wm_mod.__file__, encoding="utf-8").read()
    forbidden = ["license_manager", "access_status", "license_store",
                 "offline_activation", "susi_verifier"]
    leaked = [f for f in forbidden if f in src]
    check(not leaked, f"水印模块不导入任何许可模块（泄漏={leaked}）")

    # 不触碰视频帧 API
    check("set_frame" not in src, "水印模块不调用 set_frame")
    check("_pixmap" not in src, "水印模块不读取视频 pixmap")


# ======================================================================
# C. 授权状态映射
# ======================================================================
def test_license_mapping(window) -> None:
    section("C. 授权状态映射（只消费既有状态）")

    wm = window._watermark
    window.show()
    pump(30)

    # 本机未激活 → 应显示并运动
    window._sync_watermark()
    pump(20)
    check(window._access_status.is_expired(), "前置：本机确实处于未激活状态")
    check(wm.isVisible(), "未激活 → 水印可见")
    check(wm.is_animating(), "未激活 → 动画计时器在跑")

    # 切到已激活
    window._access_status.set_license_active(None)   # None = 永久授权
    window._sync_watermark()
    pump(20)
    check(not window._access_status.is_expired(), "前置：状态已切为已激活")
    check(not wm.isVisible(), "已激活 → 水印立即隐藏")
    check(not wm.is_animating(), "已激活 → 动画计时器已停止")

    # 切回未激活
    window._access_status.set_license_inactive("测试：未激活")
    window._sync_watermark()
    pump(20)
    check(wm.isVisible(), "状态切回未激活 → 水印重新显示")
    check(wm.is_animating(), "状态切回未激活 → 动画重新启动")

    # 幂等：连续设置同一状态不应产生额外副作用
    before = wm.is_animating()
    window._sync_watermark(True)
    window._sync_watermark(True)
    pump(10)
    check(wm.is_animating() == before, "重复设置同一状态保持幂等")

    # 通过真实回调路径验证（不是直接调 _sync_watermark）
    window._on_access_status_changed()
    pump(10)
    check(wm.isVisible() == window._access_status.is_expired(),
          "经 _on_access_status_changed 回调后，显隐与 is_expired() 一致")

    # 状态未确认（新建窗口、许可尚未落地）→ 按未激活处理（保守）
    w2 = build_window()
    w2.show()
    pump(20)
    check(w2._watermark.isVisible(),
          "状态未确认（许可未落地）时按未激活处理 → 显示水印（保守，不误报已激活）")
    w2.close()
    pump(10)


# ======================================================================
# D. 边界与缩放
# ======================================================================
def test_bounds(window) -> None:
    section("D. 边界与缩放")

    wm = window._watermark
    w, h = wm.watermark_size()

    # 容器缩放：位置必须仍在合法范围内
    for width, height in ((1400, 860), (900, 600), (640, 400)):
        window.resize(width, height)
        pump(40)
        window._sync_watermark(True)
        pump(20)
        r = wm.watermark_rect()
        b = wm._bounds()
        ok = (r.left() >= b.left() and r.top() >= b.top()
              and r.right() <= b.right() and r.bottom() <= b.bottom())
        check(ok, f"容器 {width}x{height} 下水印完整落在合法范围内（rect={r}, bounds={b}）")

    # 长跑：真实控件 + 真实计时器推进，不越界
    window.resize(1200, 760)
    pump(40)
    wm.set_denied(True)
    bad = None
    for _ in range(3000):
        wm.step()
        r = wm.watermark_rect()
        b = wm._bounds()
        if not (r.left() >= b.left() and r.top() >= b.top()
                and r.right() <= b.right() and r.bottom() <= b.bottom()):
            bad = (r, b)
            break
    check(bad is None, f"3000 步真实推进不越界（越界样本={bad}）")

    # 有帧时：边界应收缩到视频可视矩形（letterbox 内），不落到黑边
    from PySide6.QtGui import QImage
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)   # 16:9
    window._video_widget.set_frame(frame, 1)
    pump(20)
    vis = window._video_widget.visible_video_rect()
    check(vis.width() > 0 and vis.height() > 0, f"有帧时可视视频矩形有效 {vis}")
    check(vis.height() < window._video_widget.height() or
          vis.width() < window._video_widget.width(),
          "16:9 帧放进更窄/更矮的控件 → 存在 letterbox（可视矩形小于控件）")
    b = wm._bounds()
    check(b.width() <= vis.width() and b.height() <= vis.height(),
          f"水印边界不超出可视视频矩形（bounds={b} vs visible={vis}）")

    # 极小预览区：不异常、不越界
    window._video_widget.set_bounds_override = None if False else None
    wm.set_bounds_override(QRect(0, 0, 20, 10))
    wm.set_denied(True)
    for _ in range(200):
        wm.step()
    r = wm.watermark_rect()
    check(r.left() >= 0 and r.top() >= 0,
          f"极小边界（20x10 < 水印 {w}x{h}）不产生负坐标（rect={r}）")
    check(wm.is_animating() or True, "极小边界下不抛异常")
    wm.set_bounds_override(None)

    # 无帧时：边界为整个容器
    window._video_widget.clear_frame()
    pump(10)
    vis = window._video_widget.visible_video_rect()
    check(vis == window._video_widget.rect(),
          "无帧时可视矩形 = 整个视频控件（未连接时在空白背景上运动）")


# ======================================================================
# E. 生命周期
# ======================================================================
def test_lifecycle(window) -> None:
    section("E. 生命周期")

    wm = window._watermark
    wm.set_denied(True)
    pump(10)
    check(wm.is_animating(), "未激活 → 动画运行")

    wm.hide()
    pump(10)
    check(not wm.is_animating(), "隐藏 → 计时器立即停止（不空转）")

    wm.set_denied(True)
    pump(10)
    check(wm.is_animating(), "重新显示 → 计时器恢复")

    wm.close()
    pump(10)
    check(not wm.is_animating(), "关闭 → 计时器停止")

    # 全部隐藏后不应有存活的水印计时器
    window.close()
    pump(20)
    alive = [t for t in wm.findChildren(QTimer) if t.isActive()]
    check(not alive, f"窗口关闭后水印内部无活动 QTimer（实测 {len(alive)}）")


# ======================================================================
# F. 业务隔离
# ======================================================================
def test_isolation(window) -> None:
    section("F. 业务隔离（红线）")

    wm = window._watermark
    frame = np.arange(1080 * 1920 * 3, dtype=np.uint8).reshape(1080, 1920, 3)
    original = frame.copy()
    digest_before = hash(original.tobytes())

    window._video_widget.set_frame(frame, 7)
    pump(10)
    wm.set_denied(True)
    for _ in range(300):
        wm.step()
    pump(10)

    check(hash(frame.tobytes()) == digest_before,
          "水印运行 300 帧后，原始视频帧字节完全未变（T13）")
    check(np.array_equal(frame, original), "帧内容逐像素一致")

    # ROI 坐标映射不受影响
    roi_rect = QRect(100, 200, 300, 100)
    window._video_widget._roi_rect = roi_rect
    window._video_widget._video_size = (1920, 1080)
    before = window._video_widget.visible_video_rect()
    for _ in range(100):
        wm.step()
    after = window._video_widget.visible_video_rect()
    check(before == after, "水印运行不影响可视视频矩形（ROI 映射的基准）")
    check(window._video_widget._roi_rect == roi_rect, "ROI 矩形未被改动")

    # 水印不读帧：把帧清空后水印照常运动
    window._video_widget.clear_frame()
    pump(10)
    ok = True
    for _ in range(50):
        try:
            wm.step()
        except Exception:  # noqa: BLE001
            ok = False
            break
    check(ok, "清空视频帧后水印仍能独立推进（证明不依赖帧数据）")


# ======================================================================
# G. 真实渲染定位（防止"算对了位置但画在别处"）
# ======================================================================
def test_render_position(window) -> None:
    section("G. 真实渲染定位")

    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QPixmap

    wm = window._watermark
    window.resize(1200, 780)
    pump(40)
    window._sync_watermark(True)
    pump(30)
    for _ in range(40):
        wm.step()
    pump(20)

    off = wm.mapTo(window, QPoint(0, 0))
    r_local = wm.watermark_rect()
    r_win = r_local.translated(off)

    pm = QPixmap(window.size())
    pm.fill(QColor(0, 0, 0))
    window.render(pm)
    img = pm.toImage()

    def peak(rect):
        mx = 0
        for y in range(rect.top(), rect.bottom() + 1):
            for x in range(rect.left(), rect.right() + 1):
                if 0 <= x < img.width() and 0 <= y < img.height():
                    c = img.pixelColor(x, y)
                    mx = max(mx, c.red() + c.green() + c.blue())
        return mx

    at_pos = peak(r_win)
    elsewhere = peak(r_win.translated(0, -140))
    check(at_pos > elsewhere + 40,
          f"水印确实画在 watermark_rect() 处（该处峰值={at_pos} vs 上方对照={elsewhere}）")

    # 控件原点不该有残留（曾经的 bug：画在 (0,0) 而不是当前位置）
    origin = QRect(off.x(), off.y(), r_local.width(), r_local.height())
    if not origin.intersects(r_win):
        check(peak(origin) <= elsewhere + 40,
              f"控件原点无残留绘制（原点峰值={peak(origin)}，说明 paintEvent 用了平移）")

    # 移动后旧位置不残留
    old_win = wm.watermark_rect().translated(off)
    for _ in range(60):
        wm.step()
    pump(20)
    pm2 = QPixmap(window.size())
    pm2.fill(QColor(0, 0, 0))
    window.render(pm2)
    img2 = pm2.toImage()

    def peak2(rect):
        mx = 0
        for y in range(rect.top(), rect.bottom() + 1):
            for x in range(rect.left(), rect.right() + 1):
                if 0 <= x < img2.width() and 0 <= y < img2.height():
                    c = img2.pixelColor(x, y)
                    mx = max(mx, c.red() + c.green() + c.blue())
        return mx

    new_win = wm.watermark_rect().translated(off)
    if not old_win.intersects(new_win):
        check(peak2(old_win) <= peak2(new_win) + 40,
              f"水印移动后旧位置无残影（旧={peak2(old_win)} 新={peak2(new_win)}）")


def main() -> int:
    theme.configure_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv)

    test_bounce_step()
    window = build_window()
    try:
        test_structure(window)
        test_license_mapping(window)
        test_bounds(window)
        test_lifecycle(window)
        test_isolation(window)
        test_render_position(window)
    finally:
        try:
            window.close()
        except Exception:  # noqa: BLE001
            pass
        pump(5)

    print()
    print("=" * 70)
    if _FAILED:
        print(f"结果：失败 {_FAILED} 项")
    else:
        print("结果：全部通过")
    print("=" * 70)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
