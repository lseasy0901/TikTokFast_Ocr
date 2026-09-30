# -*- coding: utf-8 -*-
"""verify_frozen_bundle 音频补丁：QtMultimedia 产物核对

模拟三种包：
    - 缺 Qt6Multimedia.dll        → 必须失败
    - 缺 multimedia 插件          → 必须失败
    - 完整                        → 必须通过
外加 expected_payload() 的音频期望集检查。
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from scripts.verify_frozen_bundle import (  # noqa: E402
    audio_payload_failures, expected_payload,
    QTMULTIMEDIA_PYD, QTMULTIMEDIA_QT_DLL, MULTIMEDIA_PLUGIN_DIR,
    MULTIMEDIA_BACKEND_PLUGINS,
)


def _pyside_dir() -> Path:
    import importlib.util
    spec = importlib.util.find_spec("PySide6")
    assert spec is not None and spec.submodule_search_locations, \
        "构建环境必须安装 PySide6"
    return Path(list(spec.submodule_search_locations)[0])


class TestAudioPayloadFailures(unittest.TestCase):
    """audio_payload_failures：三种模拟包"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.internal = Path(self.tmp.name) / "_internal"
        self.pyside = _pyside_dir()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_full_bundle(self):
        """按构建环境真实文件搭一个音频完整的 _internal"""
        (self.internal / "PySide6").mkdir(parents=True)
        shutil.copyfile(self.pyside / "QtMultimedia.pyd",
                        self.internal / QTMULTIMEDIA_PYD)
        shutil.copyfile(self.pyside / "Qt6Multimedia.dll",
                        self.internal / QTMULTIMEDIA_QT_DLL)
        plugin_src = self.pyside / "plugins" / "multimedia"
        for name in MULTIMEDIA_BACKEND_PLUGINS:
            src = plugin_src / name
            if src.is_file():
                dst = self.internal / MULTIMEDIA_PLUGIN_DIR / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)

    def test_complete_bundle_passes(self):
        self._make_full_bundle()
        self.assertEqual(audio_payload_failures(str(self.internal)), [])

    def test_missing_qt6multimedia_dll_fails(self):
        self._make_full_bundle()
        os.remove(self.internal / QTMULTIMEDIA_QT_DLL)
        failures = audio_payload_failures(str(self.internal))
        self.assertTrue(failures)
        self.assertTrue(any("Qt6Multimedia.dll" in f for f in failures))

    def test_missing_multimedia_plugin_fails(self):
        self._make_full_bundle()
        shutil.rmtree(self.internal / MULTIMEDIA_PLUGIN_DIR)
        failures = audio_payload_failures(str(self.internal))
        self.assertTrue(failures)
        self.assertTrue(any("后端插件" in f for f in failures))

    def test_empty_plugin_dir_fails(self):
        """插件目录存在但没有任何 Windows 后端 → 失败"""
        self._make_full_bundle()
        plugin_dir = self.internal / MULTIMEDIA_PLUGIN_DIR
        shutil.rmtree(plugin_dir)
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "unrelated.dll").write_bytes(b"x")
        failures = audio_payload_failures(str(self.internal))
        self.assertTrue(any("后端插件" in f for f in failures))

    def test_missing_pyd_fails(self):
        self._make_full_bundle()
        os.remove(self.internal / QTMULTIMEDIA_PYD)
        failures = audio_payload_failures(str(self.internal))
        self.assertTrue(any("QtMultimedia.pyd" in f for f in failures))


class TestExpectedPayloadIncludesAudio(unittest.TestCase):
    """expected_payload 的音频期望集：构建环境有 PySide6 时必须包含音频条目"""

    def test_audio_entries_present(self):
        entries, _warnings = expected_payload(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        relpaths = {entry.relpath for entry in entries}
        self.assertIn(QTMULTIMEDIA_PYD, relpaths)
        self.assertIn(QTMULTIMEDIA_QT_DLL, relpaths)
        audio_plugins = {r for r in relpaths
                         if r.startswith(MULTIMEDIA_PLUGIN_DIR)}
        self.assertTrue(audio_plugins, "期望集必须包含 multimedia 插件")
        # 期望集里的插件必须与构建环境实际文件一一对应（字节数对照用）
        plugin_src = _pyside_dir() / "plugins" / "multimedia"
        for entry in entries:
            if entry.relpath.startswith(MULTIMEDIA_PLUGIN_DIR):
                self.assertTrue(
                    (plugin_src / Path(entry.relpath).name).is_file())


if __name__ == "__main__":
    unittest.main()
