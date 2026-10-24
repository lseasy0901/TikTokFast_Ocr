#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test video empty state message visibility
"""

import sys
import time

# Add project root to path
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication
from gui.main_window import MainWindow


def test_video_message():
    """Test that the video connection message is visible"""
    app = QApplication(sys.argv)

    # Create window
    window = MainWindow()
    window.show()

    # Check if connection status label is visible
    connection_label = window._connection_status_label
    print(f"Connection label visible: {connection_label.isVisible()}")
    print(f"Connection label text: '{connection_label.text()}'")

    # Wait a bit
    time.sleep(2)

    # Simulate getting a frame (should hide the message)
    # This would normally happen in _on_frame_timeout
    # connection_label.hide()

    app.quit()


if __name__ == "__main__":
    test_video_message()