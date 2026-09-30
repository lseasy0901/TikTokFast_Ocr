# -*- coding: utf-8 -*-
"""音频测试 5/7：FFmpeg 异常 / 输出故障的隔离

核心断言：音频侧任何异常都不向视频链路（调用方）传播。
"""

import unittest

from audio import AudioService, AudioState
from audio.output import AudioOutputError
from audio_test_utils import FakeOutput, SILENCE_100MS, wait_for

CHUNK = SILENCE_100MS


class TestOutputOpenFailure(unittest.TestCase):
    def test_open_failure_becomes_error_state(self):
        states = []
        svc = AudioService(output_factory=lambda: FakeOutput(fail_on_open=True))
        svc.set_state_callback(lambda s, m: states.append((s, m)))
        svc.start()
        self.assertTrue(wait_for(lambda: svc.state is AudioState.ERROR))
        # feed 不得抛异常（调用方=视频读取线程 无感）
        svc.feed(CHUNK)
        svc.stop()
        self.assertIn(AudioState.ERROR, [s for s, _ in states])

    def test_device_missing_message(self):
        def bad_factory():
            raise AudioOutputError("没有可用的音频输出设备")

        svc = AudioService(output_factory=bad_factory)
        msgs = []
        svc.set_state_callback(lambda s, m: msgs.append(m))
        svc.start()
        self.assertTrue(wait_for(lambda: svc.state is AudioState.ERROR))
        self.assertTrue(any("音频输出设备" in m for m in msgs))
        svc.stop()


class TestMidStreamFailure(unittest.TestCase):
    def test_write_failure_does_not_propagate(self):
        states = []
        svc = AudioService(output_factory=lambda: FakeOutput(fail_on_write=True))
        svc.set_state_callback(lambda s, m: states.append((s, m)))
        svc.start()
        # 大量投喂：worker 写入抛异常 → 音频线程退出并转 ERROR，调用方无感
        for _ in range(20):
            svc.feed(CHUNK)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.ERROR, timeout=5))
        svc.feed(CHUNK)     # 已停止：丢弃，不抛
        svc.stop()


class TestReaderCallbackContract(unittest.TestCase):
    """FFmpegReader._notify_audio 的隔离约定：回调异常被吞掉"""

    def test_notify_audio_swallows_callback_exception(self):
        import logging
        logging.disable(logging.CRITICAL)
        try:
            from stream.ffmpeg_reader import FFmpegReader
            r = FFmpegReader("http://stub", width=64, height=64)
            r._audio_pcm_callback = lambda d: (_ for _ in ()).throw(RuntimeError("boom"))
            r._notify_audio(CHUNK)      # 不应抛出
            r._notify_audio(None)
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
