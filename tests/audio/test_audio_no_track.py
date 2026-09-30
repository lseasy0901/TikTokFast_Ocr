# -*- coding: utf-8 -*-
"""音频测试 6/7：无音频轨判定

- FFmpegReader 侧：feed(None) 契约 = 明确无音频轨
- 服务侧：EOF 且从未收到数据 → 同样判无音频轨
- 有过数据后的 EOF：不算无音频轨（正常断流，由视频侧统一处理）
"""

import unittest

from audio import AudioService, AudioState
from audio_test_utils import FakeOutput, SILENCE_100MS, wait_for


class TestNoAudioTrack(unittest.TestCase):
    def test_none_signal(self):
        svc = AudioService(output_factory=FakeOutput)
        svc.start()
        svc.feed(None)
        self.assertIs(svc.state, AudioState.NO_AUDIO_TRACK)
        svc.stop()

    def test_eof_without_data(self):
        svc = AudioService(output_factory=FakeOutput)
        svc.start()
        svc.feed(b"")
        self.assertIs(svc.state, AudioState.NO_AUDIO_TRACK)
        svc.stop()

    def test_eof_after_data_is_not_no_track(self):
        svc = AudioService(output_factory=FakeOutput)
        svc.start()
        svc.feed(SILENCE_100MS)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING))
        svc.feed(b"")                            # 正常断流
        self.assertIs(svc.state, AudioState.PLAYING)
        svc.stop()


class TestReaderCommand(unittest.TestCase):
    """命令构造：音频关闭时命令与改动前一致；打开时仅追加音频段"""

    def test_cmd_without_audio_unchanged(self):
        from stream.ffmpeg_reader import FFmpegReader
        r = FFmpegReader("http://s", width=1920, height=1080)
        cmd = r._build_ffmpeg_cmd()
        self.assertNotIn("tcp://", " ".join(cmd))
        self.assertNotIn("-map", cmd)

    def test_cmd_with_audio_appends_block(self):
        from stream.ffmpeg_reader import FFmpegReader
        from stream.ffmpeg_reader import _AUDIO_SAMPLE_RATE, _AUDIO_CHANNELS
        r = FFmpegReader("http://s", width=1920, height=1080, audio_enabled=True)
        r._audio_port = 59999
        cmd = r._build_ffmpeg_cmd()
        text = " ".join(cmd)
        # stdout 视频段仍在，且位于音频段之前
        self.assertIn("-f rawvideo pipe:1", text)
        tcp_idx = next(i for i, c in enumerate(cmd) if c.startswith("tcp://"))
        self.assertLess(cmd.index("pipe:1"), tcp_idx)
        self.assertIn("-map 0:a", text)
        self.assertIn("-f s16le", text)
        self.assertIn(f"-ar {_AUDIO_SAMPLE_RATE}", text)
        self.assertIn(f"-ac {_AUDIO_CHANNELS}", text)
        self.assertIn("tcp://127.0.0.1:59999", text)


if __name__ == "__main__":
    unittest.main()
