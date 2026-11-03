# -*- coding: utf-8 -*-
"""qr_detect —— 从「闪-live_player」(DouyinQR) 逆向还原的直播二维码识别模块。

分层：
  zbar_backend   zbar ctypes 后端（解码 + 定位多边形）
  encode_backend qrencode ctypes 后端（重生成清晰二维码）
  detector       纯算法检测管线（无 UI 依赖，numpy + opencv）
  worker         QThread 帧循环线程（PySide6，节奏/去重复刻原版）
  popup          识别结果置顶弹窗（PySide6，复刻 QRPopupDialog）
"""
from .detector import QrDetector, QRDetection  # noqa: F401

__all__ = ["QrDetector", "QRDetection"]
