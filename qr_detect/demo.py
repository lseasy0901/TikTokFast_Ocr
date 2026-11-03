# -*- coding: utf-8 -*-
"""qr_detect 独立演示。

用法：
    python demo.py <视频文件>            # 逐帧检测，控制台输出 + 高亮窗口
    python demo.py <图片>                # 单图检测
    python demo.py --camera 0            # 摄像头
    python demo.py <视频文件> --popup    # 命中时弹置顶大码弹窗（复刻原版体验）

按 q 退出。
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qr_detect.detector import QrDetector


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", help="视频文件 / 图片路径 / 摄像头序号")
    ap.add_argument("--camera", type=int, default=None)
    ap.add_argument("--popup", action="store_true", help="命中时弹窗展示重生成的大二维码")
    ap.add_argument("--interval-ms", type=int, default=0, help="最小扫描间隔（0=每帧）")
    args = ap.parse_args()

    app = None
    if args.popup:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])

    detector = QrDetector()
    last_content = None

    def on_frame(frame_bgr):
        nonlocal last_content
        det = detector.detect(frame_bgr)
        if det is not None and det.content != last_content:
            last_content = det.content
            print(f"[+] 新二维码: {det.text!r}  bbox={det.bbox}")
            if args.popup:
                from qr_detect.popup import QrPopupDialog
                QrPopupDialog(det.content, auto_close_ms=8000).show()
        if det is not None:
            poly = np.array(det.polygon, dtype=np.int32)
            cv2.polylines(frame_bgr, [poly], True, (45, 212, 191), 2)
        return frame_bgr

    if args.camera is not None:
        cap = cv2.VideoCapture(args.camera)
        win = "qr_detect demo"
        last_ts = 0.0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if args.interval_ms and (time.monotonic() - last_ts) * 1000 < args.interval_ms:
                continue
            last_ts = time.monotonic()
            frame = on_frame(frame)
            cv2.imshow(win, frame)
            if app:
                app.processEvents()
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
        cap.release(); cv2.destroyAllWindows()
        return 0

    src = args.source
    if src is None:
        ap.error("需要视频/图片路径或 --camera N")
    if src.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
        img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
        on_frame(img)
        cv2.imwrite("qr_detect_out.png", img)
        print("结果已存 qr_detect_out.png")
        if app:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2000, app.quit)
            app.exec()
        return 0

    cap = cv2.VideoCapture(src)
    win = "qr_detect demo"
    last_ts = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if not args.interval_ms or (time.monotonic() - last_ts) * 1000 >= args.interval_ms:
            last_ts = time.monotonic()
            frame = on_frame(frame)
        cv2.imshow(win, frame)
        if app:
            app.processEvents()
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release(); cv2.destroyAllWindows()
    detector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
