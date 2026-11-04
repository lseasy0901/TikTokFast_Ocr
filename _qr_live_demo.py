# -*- coding: utf-8 -*-
"""二维码识别端到端演示:合成帧 → 真实检测线程 → 真实弹窗(不连直播间)。

帧源:用 qr_detect 自带 qrencode 后端生成两个假码,贴进 1280×720 的
合成"视频帧",QTimer 每 7 秒切换一次推入 LatestFrameBuffer;
识别/去重/弹窗全部走生产路径(_on_qr_detected)。
"""
import os
import sys
import tempfile

os.environ['APPDATA'] = tempfile.mkdtemp(prefix='dlv-qr-appdata-')
os.environ['LOCALAPPDATA'] = tempfile.mkdtemp(prefix='dlv-qr-local-')
# 原生渲染:弹窗要真实出现在用户桌面上(此前 offscreen 导致用户看不到)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from gui import theme
theme.apply_theme()

from gui.main_window import MainWindow
from qr_detect.encode_backend import encode_qr_matrix, matrix_to_image

CODES = ["https://live.douyin.com/qr-demo-1",
         "LiveLens 二维码识别演示 #2"]

def make_frame(code_text: str) -> np.ndarray:
    """生成 1280×720 BGR 合成帧:深色背景 + 居中白色区贴二维码。"""
    ret = encode_qr_matrix(code_text.encode('utf-8'))
    _ver, matrix = ret
    img, _total = matrix_to_image(matrix, target_px=240)
    import cv2
    qr = cv2.cvtColor(np.array(img.convert('RGB')), cv2.COLOR_RGB2BGR)
    frame = np.full((720, 1280, 3), (40, 35, 30), dtype=np.uint8)
    # 随机噪点纹理,模拟视频画面
    noise = np.random.default_rng(42).integers(0, 30, (720, 1280, 3), dtype=np.uint8)
    frame = frame + noise
    h, w = qr.shape[:2]
    x0, y0 = (1280 - w) // 2, (720 - h) // 2
    frame[y0:y0+h, x0:x0+w] = qr
    return frame

app = QApplication.instance() or QApplication([])
w = MainWindow()
w.show()
w.raise_()
w.activateWindow()
app.processEvents()

# 连接态 + 帧缓冲(手动构造,不连流)
w._connected = True
from stream.frame_buffer import LatestFrameBuffer
w._frame_buffer = LatestFrameBuffer()

frames = [make_frame(c) for c in CODES]
state = {'idx': 0, 'pushes': 0}

def push():
    w._frame_buffer.update(frames[state['idx'] % len(frames)])
    state['idx'] += 1
    state['pushes'] += 1

# 开启二维码识别(走真实开关路径,触发真实 worker)
w._connected = True
w._qr_toggle.setChecked(True)
app.processEvents()
assert w._qr_worker is not None and w._qr_worker.isRunning(), "worker 未启动"

push()
popups = []
def watch():
    app.processEvents()
    from qr_detect.popup import QrPopupDialog
    cur = [x for x in QApplication.topLevelWidgets()
           if isinstance(x, QrPopupDialog) and x.isVisible()]
    if cur and cur[-1] not in popups:
        popups.append(cur[-1])
        cur[-1].raise_()
        cur[-1].activateWindow()   # 把新弹窗带到前台给用户看

watch_timer = QTimer(); watch_timer.timeout.connect(watch); watch_timer.start(100)
push_timer = QTimer(); push_timer.setInterval(7000)
push_timer.timeout.connect(push)
push_timer.start()

def snapshot():
    import ctypes, ctypes.wintypes as wt
    import PIL.ImageGrab as ig
    user32 = ctypes.windll.user32
    r = wt.RECT()
    from qr_detect.popup import QrPopupDialog
    dlg = [x for x in QApplication.topLevelWidgets() if isinstance(x, QrPopupDialog)]
    if dlg:
        user32.GetWindowRect(int(dlg[-1].winId()), ctypes.byref(r))
        ig.grab((r.left-20, r.top-20, r.right+20, r.bottom+20)).save('_qr_shot.png')
        print('[SHOT] popup saved')
    else:
        print('[SHOT] no popup yet')

QTimer.singleShot(2500, snapshot)          # 第一个码已弹出
QTimer.singleShot(3000, lambda: print('[INFO] clipboard:', app.clipboard().text()[:50]))
QTimer.singleShot(8500, snapshot)          # 第二个码
QTimer.singleShot(9000, lambda: print('[INFO] clipboard:', app.clipboard().text()[:50]))

print('[DEMO] running: frame push every 7s, popups should appear on screen')
app.exec()
