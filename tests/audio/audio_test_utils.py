# -*- coding: utf-8 -*-
"""tests/audio 公共桩件：FakeOutput（替代声卡）与工具函数"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from audio.models import BYTES_PER_SECOND

__all__ = ["FakeOutput", "wait_for", "SILENCE_100MS"]


class FakeOutput:
    """QtAudioOutput 替身：记录写入，可注入故障"""

    def __init__(self, volume=0.8, fail_on_open=False, fail_msg="stub device error",
                 accept_all=True, fail_on_write=False):
        self.volume = volume
        self.opened = False
        self.closed = False
        self.data = bytearray()
        self.fail_on_open = fail_on_open
        self.fail_msg = fail_msg
        self.accept_all = accept_all      # False = 设备缓冲满（write 只收一半）
        self.fail_on_write = fail_on_write
        self._underrun = False

    def open(self):
        if self.fail_on_open:
            raise RuntimeError(self.fail_msg)
        self.opened = True

    def write(self, data: bytes) -> int:
        if self.fail_on_write:
            raise RuntimeError("write failed")
        if self.accept_all:
            self.data.extend(data)
            return len(data)
            # 设备缓冲模拟：只接受一半
        n = max(len(data) // 2, 0)
        self.data.extend(data[:n])
        return n

    @property
    def is_underrun(self) -> bool:
        return self._underrun

    def set_underrun(self, value: bool) -> None:
        self._underrun = value

    def close(self):
        self.closed = True


SILENCE_100MS = bytes(BYTES_PER_SECOND // 10)


def wait_for(predicate, timeout=3.0, interval=0.02) -> bool:
    """轮询等待条件成立（测试里避免裸 sleep）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
