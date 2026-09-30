# -*- coding: utf-8 -*-
"""
历史记录 UI：HistoryButton（地址栏入口）+ HistoryPopup（卡片式列表）

职责边界：
    - 本模块只做展示与用户输入转发，不含任何存储/业务逻辑
      （数据来自 HistoryService，经 main_window 注入）
    - 卡片面向"用户可识别信息"：备注名 / 未命名直播间 + 房间号 + 最近使用，
      **绝不展示完整 URL**
    - 为未来扩展留位：每张卡片是独立 widget，加收藏/删除/改备注=加按钮，
      不动列表结构

用法：
    btn = HistoryButton(service)          # 点按钮弹出面板
    btn.enterRequested.connect(on_enter)  # 用户点了某张卡片的 [进入]
"""

from datetime import datetime

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.widgets import set_property
from stream.history import HistoryService, LiveRoomHistoryItem

__all__ = ["HistoryButton", "HistoryPopup"]


class HistoryCard(QFrame):
    """单条历史记录卡片（独立 widget：未来加收藏/删除=在这里加按钮）"""

    enterRequested = Signal(str)     # 携带该条目的 URL

    def __init__(self, item: LiveRoomHistoryItem, parent=None):
        super().__init__(parent)
        self._url = item.url
        self.setObjectName("historyCard")

        col = QVBoxLayout(self)
        col.setContentsMargins(12, 8, 12, 8)
        col.setSpacing(4)

        # 主标题：备注名 / 未命名直播间
        title = QLabel(item.display_name())
        title.setObjectName("historyCardTitle")
        title.setWordWrap(True)
        col.addWidget(title)

        # 房间号 + 最近使用（同一行，次要信息）
        meta_bits = []
        room_id = item.display_room_id()
        if room_id:
            meta_bits.append(f"房间号：{room_id}")
        last_used = item.display_last_used()
        if last_used:
            meta_bits.append(f"最近使用：{last_used}")
        if meta_bits:
            meta = QLabel("    ".join(meta_bits))
            meta.setObjectName("historyCardMeta")
            col.addWidget(meta)

        # [进入] 行：右对齐（未来可并排放收藏/删除/改备注）
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        enter_btn = QPushButton("进入")
        enter_btn.setObjectName("historyCardEnter")
        enter_btn.setCursor(Qt.PointingHandCursor)
        enter_btn.setFixedWidth(72)
        set_property(enter_btn, "variant", "primary")
        enter_btn.clicked.connect(lambda: self.enterRequested.emit(self._url))
        action_row.addWidget(enter_btn)
        col.addLayout(action_row)

    @property
    def url(self) -> str:
        return self._url


class HistoryPopup(QDialog):
    """历史记录弹出面板（卡片列表；点 [进入] 发 enterRequested 并关闭）"""

    enterRequested = Signal(str)

    def __init__(self, service: HistoryService, parent=None):
        super().__init__(parent)
        self._service = service
        self.setObjectName("historyPopup")
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setModal(False)
        self.setMinimumWidth(280)
        self.setMaximumWidth(360)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._list_host)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        self._empty_label = QLabel("暂无历史记录\n成功进入直播间后会自动保存")
        self._empty_label.setObjectName("historyCardMeta")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def refresh_and_show(self, anchor: QWidget) -> None:
        """从服务刷新列表并弹出在 anchor（历史按钮）下方"""
        self._rebuild()
        self.adjustSize()
        self.resize(self.minimumWidth(), self.sizeHint().height())
        # 弹出在按钮正下方，右缘与按钮对齐（地址栏在窗口右上区域）
        pos = anchor.mapToGlobal(anchor.rect().bottomRight())
        pos.setX(max(8, pos.x() - self.width()))
        self.move(pos)
        self.show()

    def _rebuild(self) -> None:
        while self._list_layout.count():
            taken = self._list_layout.takeAt(0)
            if taken.widget():
                taken.widget().deleteLater()
        items = self._service.items()
        if not items:
            self._list_layout.addWidget(self._empty_label)
            return
        for item in items:
            card = HistoryCard(item, self._list_host)
            card.enterRequested.connect(self._on_enter)
            self._list_layout.addWidget(card)

    def _on_enter(self, url: str) -> None:
        self.accept()
        self.enterRequested.emit(url)


class RemarkDialog(QDialog):
    """首次进入直播间的备注询问框

    两个等价出口：保存（非空才写）/ 跳过（直接关闭，历史已保存、remark 空）。
    Enter=保存，Esc=跳过。
    """

    def __init__(self, room_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加直播间备注")
        self.setModal(True)
        self.setFixedWidth(380)

        col = QVBoxLayout(self)
        col.setSpacing(8)

        head = QLabel(f"首次进入直播间{' ' + room_id if room_id else ''}，要添加备注吗？")
        head.setWordWrap(True)
        col.addWidget(head)

        self._input = QLineEdit()
        self._input.setPlaceholderText("例如：今晚比赛直播（可留空跳过）")
        self._input.setMinimumHeight(28)
        col.addWidget(self._input)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        skip_btn = QPushButton("跳过")
        skip_btn.clicked.connect(self.reject)
        btn_row.addWidget(skip_btn)
        save_btn = QPushButton("保存备注")
        set_property(save_btn, "variant", "primary")
        save_btn.clicked.connect(self.accept)
        save_btn.setDefault(True)
        btn_row.addWidget(save_btn)
        col.addLayout(btn_row)

    def remark(self) -> str:
        """用户输入的备注（strip 过；跳过/留空 = 空串）"""
        return self._input.text().strip()


class HistoryButton(QPushButton):
    """地址栏的「历史」按钮：点击弹出历史面板

    按钮自身是唯一挂载点：service 由构造注入，enterRequested 由上层连接；
    未来加收藏排序/删除等只改 HistoryPopup/卡片，不动本类与 main_window。
    """

    enterRequested = Signal(str)

    def __init__(self, service: HistoryService, parent=None):
        super().__init__("历史", parent)
        self._service = service
        self._popup: HistoryPopup | None = None
        self.setObjectName("historyBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("成功进入过的直播间（点击进入）")
        self.clicked.connect(self._on_clicked)

    def _on_clicked(self) -> None:
        popup = self._popup
        if popup is not None:
            # wrapper 仍被 Python 持有 ⇒ C++ 对象必然存活（见 destroyed 清理），
            # 可安全调用：已有弹窗则关闭（toggle）。
            popup.close()
            return
        popup = HistoryPopup(self._service, self.window())
        popup.enterRequested.connect(self.enterRequested)
        # 生命周期契约：WA_DeleteOnClose 使关闭即销毁 C++ 对象；destroyed 信号
        # 在销毁时把 Python 引用清掉 —— 保证 self._popup 非 None 时 wrapper
        # 必定有效，后续点击永不触达"Internal C++ object already deleted"。
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup.destroyed.connect(self._on_popup_destroyed)
        self._popup = popup
        popup.refresh_and_show(self)

    def _on_popup_destroyed(self, *_args) -> None:
        """popup 的 C++ 对象被销毁（关闭/进入/外部点击）→ 清理 Python 引用"""
        self._popup = None

    def on_remark_request(self, url: str, room_id: str = "") -> None:
        """HistoryService 备注询问回调：弹备注询问框。

        出口写回状态（状态机收敛在 HistoryService）：
            - 保存非空备注 → set_note → SET
            - 跳过 / 留空确认 → mark_remark_skipped → SKIPPED（以后不自动弹）
        由 main_window 一行接线：service.set_remark_callback(btn.on_remark_request)
        """
        dialog = RemarkDialog(room_id, self.window())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        remark = dialog.remark()
        if accepted and remark:
            self._service.set_note(url, remark)
        else:
            # 用户主动跳过（reject）或确认了空备注：固化 SKIPPED
            self._service.mark_remark_skipped(url)
