# -*- coding: utf-8 -*-
"""播放域的 UI 操作面(Phase 4.5 解耦收口,2026-11-05)。

PlaybackCoordinator 只经本类触达界面控件,**不再直接持有/操作任何
QWidget**——控件仍挂在窗口上(测试可整体替换 window._lbl_status 等),
操作面按名字动态取用,因此测试替换控件后行为不变。

窗口方法(_set_chip/_stop_ocr/_sync_note_action 等既有 API)继续可用:
操作面是它们的唯一调用方,协调器不重复知道控件名。
"""

from __future__ import annotations

from gui.widgets import set_property

__all__ = ["PlaybackUI"]


class PlaybackUI:
    """协调器 ↔ 界面的唯一通道(每个方法对应一个 UI 动作)。"""

    #: 芯片名 → 窗口上的 QLabel 属性名
    _CHIPS = {"status": "_lbl_status", "fps": "_lbl_fps",
              "latency": "_lbl_latency", "resolution": "_lbl_resolution"}

    def __init__(self, window):
        self._w = window

    # ------------------------------------------------------------------
    # 连接按钮 / 地址栏
    # ------------------------------------------------------------------
    def read_url(self) -> str:
        return self._w._url_input.text().strip()

    def enter_connecting(self, status_text: str) -> None:
        """连接发起:按钮禁用为「连接中...」,地址栏锁定。"""
        self._w._connect_btn.setEnabled(False)
        self._w._connect_btn.setText("连接中...")
        self._w._url_input.setEnabled(False)
        self.status_message(status_text)

    def enter_connected(self, roi_enabled: bool) -> None:
        """已连接:主按钮变「断开」(危险色),ROI 按访问状态启用。"""
        self._w._connect_btn.setEnabled(True)
        self._w._connect_btn.setText("断开")
        set_property(self._w._connect_btn, "variant", "danger")
        self._w._url_input.setEnabled(False)
        self._w._roi_btn.setEnabled(roi_enabled)
        self.hide_connection_notice()

    def enter_idle(self) -> None:
        """回到空闲:连接按钮恢复 primary,地址栏解锁。"""
        self._w._connect_btn.setEnabled(True)
        self._w._connect_btn.setText("连接")
        set_property(self._w._connect_btn, "variant", "primary")
        self._w._url_input.setEnabled(True)

    def enter_disconnected_visuals(self) -> None:
        """断开后的静态视觉:ROI 禁用、连接提示条恢复、状态栏「已断开」。"""
        self._w._roi_btn.setEnabled(False)
        self.show_connection_notice()
        self.status_message("已断开")

    # ------------------------------------------------------------------
    # 提示
    # ------------------------------------------------------------------
    def status_message(self, text: str) -> None:
        self._w.statusBar().showMessage(text)

    def show_toast(self, text: str, tone: str = "neutral") -> None:
        self._w.show_toast(text, tone)

    def hide_connection_notice(self) -> None:
        self._w._connection_status_label.hide()

    def show_connection_notice(self) -> None:
        self._w._connection_status_label.show()

    # ------------------------------------------------------------------
    # 芯片 / 读数
    # ------------------------------------------------------------------
    def set_chip(self, chip: str, text: str, tone: str = "neutral",
                 dot: bool = False) -> None:
        """按芯片名设置读数(status/fps/latency/resolution)。"""
        self._w._set_chip(getattr(self._w, self._CHIPS[chip]), text, tone, dot)

    def set_roi_text(self, text: str) -> None:
        self._w._lbl_roi.setText(text)

    def reset_perf_value(self, key: str, fallback: str = "—") -> None:
        """性能指标复位到占位值(key 不存在时静默忽略)。"""
        values = getattr(self._w, "_perf_values", None)
        defaults = getattr(self._w, "_perf_defaults", None)
        if values is not None and key in values:
            values[key].setText(defaults.get(key, "—"))
