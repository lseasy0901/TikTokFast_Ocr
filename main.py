# -*- coding: utf-8 -*-
"""
Douyin Low Latency Stream Viewer - 启动入口 (Phase 1 - Step 4)

功能：
    初始化 PySide6 QApplication，创建并显示 MainWindow
"""

import logging
import os
import sys

# 将项目根目录加入 sys.path，保证以任意方式启动均可正确导入包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    # 配置日志（DEBUG 级别，便于排查 GUI 黑屏）
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s %(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("DouyinLowLatencyViewer")
    logger.info("启动 Douyin Low Latency Stream Viewer")

    # 延迟导入 PySide6，给出明确的依赖缺失提示
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("错误：未安装 PySide6，请执行 pip install PySide6")
        return 1

    from gui import theme
    from gui.main_window import MainWindow

    # 高 DPI 舍入策略必须在 QApplication 构造之前设置
    theme.configure_high_dpi()

    # 创建应用（QApplication 需要 argv 列表）
    app = QApplication(sys.argv)
    app.setApplicationName("Douyin Low Latency Stream Viewer")

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    # 进入事件循环
    exit_code = app.exec()
    logger.info("应用退出 (code=%d)", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
