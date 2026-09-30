# -*- coding: utf-8 -*-
"""音频测试 3/7：音量控制"""

import unittest

from audio import AudioService, AudioState
from audio_test_utils import FakeOutput, SILENCE_100MS, wait_for


class TestVolume(unittest.TestCase):
    def test_default_volume(self):
        svc = AudioService(output_factory=FakeOutput)
        self.assertAlmostEqual(svc.volume, 0.8)

    def test_set_volume_clamped(self):
        svc = AudioService(output_factory=FakeOutput)
        svc.set_volume(2.0)
        self.assertEqual(svc.volume, 1.0)
        svc.set_volume(-1)
        self.assertEqual(svc.volume, 0.0)

    def test_volume_applied_to_output(self):
        holder = []

        def factory():
            out = FakeOutput(volume=0.8)
            holder.append(out)
            return out

        svc = AudioService(output_factory=factory, volume=0.4)
        svc.set_volume(0.3)
        svc.start()
        svc.feed(SILENCE_100MS)
        self.assertTrue(wait_for(lambda: svc.state is AudioState.PLAYING))
        svc.set_volume(0.9)
        svc.feed(SILENCE_100MS)
        self.assertTrue(wait_for(lambda: holder[0].volume == 0.9))
        self.assertAlmostEqual(holder[0].volume, 0.9)
        svc.stop()


if __name__ == "__main__":
    unittest.main()
