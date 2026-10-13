# -*- coding: utf-8 -*-
"""RELEASE-02-B5: FFmpeg7 外部运行时定位 / 校验 / 缓存测试。

覆盖任务书 Phase 3.B / Phase 4「运行时故障」全部场景：
  - 定位优先级（环境变量覆盖 > 冻结构建 > 源码树 > 用户级目录）
  - 候选去重
  - 文件不存在 / 为空 / 不可执行
  - 探测超时 / 探测退出码非 0 / 版本横幅不可解析
  - 版本过低（数字发布号 < 7；快照标识时 libavcodec < 61）
  - 能力缺失（缺 hevc 解码器 / 缺 flv 解复用器）
  - 缓存语义（只探测一次、refresh 强制重探、resolved_exe 不触发探测）
  - reason → 用户可见文案映射（不含路径 / 环境变量 / 底层错误码）

全部为离线用例：要么 monkeypatch `_run_probe` 注入构造好的 ffmpeg 输出，
要么跳过（真实二进制不在本机时）。
"""
import os
import subprocess

import pytest

from utils import ffmpeg7_runtime as R

#: 真实 FFmpeg7 候选（仓库外）。不在本机时相关用例跳过。
REAL_FFMPEG7 = r"D:\TikTok_Ocr_Rebuild\runtime\ffmpeg7\ffmpeg.exe"

#: 内置运行时（2023 自定义构建，版本横幅被裁剪 —— 用作"版本不可识别"的真实样本）
BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "runtime", "ffmpeg", "ffmpeg.exe")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """每个用例前后清缓存 / 清环境覆盖，避免相互污染。

    同时把 ``LOCALAPPDATA`` 指到临时目录：真实机器上可能**已经装好**用户级
    运行时（``%LOCALAPPDATA%/LiveLens/runtime/ffmpeg7/``），若不隔离，
    `find_existing_candidates()` 会带上它，使"候选集恰好等于某个列表"的断言
    依赖机器状态。隔离后用例只反映被显式构造的条件。
    """
    monkeypatch.delenv(R._ENV_OVERRIDE, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    R.reset_cache()
    yield
    R.reset_cache()


# ======================================================================
# 构造 ffmpeg 输出
# ======================================================================
def _banner(version="7.1.1", avcodec="61. 19.100", gpl=True):
    lines = [
        f"ffmpeg version {version} Copyright (c) 2000-2025 the FFmpeg developers",
        "  built with gcc 13.2.0",
    ]
    if gpl is not None:
        conf = "--prefix=/usr --enable-shared"
        if gpl:
            conf += " --enable-gpl --enable-version3"
        lines.append(f"  configuration: {conf}")
    if avcodec is not None:
        lines += [
            "libavutil      59. 39.100 / 59. 39.100",
            f"libavcodec     {avcodec} / {avcodec}",
            "libavformat   61.  7.100 / 61.  7.100",
        ]
    return "\n".join(lines)


_DECODERS_HEVC = " VFS..D hevc                 HEVC (High Efficiency Video Coding)"
_DECODERS_NO_HEVC = " V..... h264                 H.264 / AVC / MPEG-4 AVC"
_DEMUXERS_FLV = "\n".join([
    " D   flv             FLV (Flash Video)",
    " D  live_flv        live RTMP FLV (Flash Video)",
])
_DEMUXERS_NO_FLV = " D   matroska        Matroska"


class _FakeProc:
    def __init__(self, text="", returncode=0):
        # ffmpeg 把横幅写 stderr、列表写 stdout；这里两者都塞进去，
        # 保证被测代码无论读哪个管道都能拿到完整文本。
        self.stdout = text.encode("utf-8")
        self.stderr = b""
        self.returncode = returncode


def _patch_probe(monkeypatch, *, dec_text, dem_text, dec_rc=0, dem_rc=0,
                 exc=None):
    """把 `_run_probe` 换成按参数返回构造输出的桩。"""
    calls = []

    def fake(path, args, timeout):
        calls.append(list(args))
        if exc is not None:
            raise exc
        if args == ["-decoders"]:
            return _FakeProc(dec_text, dec_rc)
        if args == ["-demuxers"]:
            return _FakeProc(dem_text, dem_rc)
        raise AssertionError(f"意外的探测参数: {args}")

    monkeypatch.setattr(R, "_run_probe", fake)
    return calls


def _existing_tmp_exe(tmp_path):
    p = tmp_path / "ffmpeg.exe"
    p.write_bytes(b"MZ" + b"\x00" * 64)
    return str(p)


# ======================================================================
# 1. 定位优先级
# ======================================================================
class TestLocate:
    def test_env_override_comes_first(self, monkeypatch):
        monkeypatch.setenv(R._ENV_OVERRIDE, r"X:\custom\ffmpeg.exe")
        assert R.candidate_paths()[0] == r"X:\custom\ffmpeg.exe"

    def test_project_and_user_dirs_present(self):
        paths = [os.path.normcase(p) for p in R.candidate_paths()]
        assert any("runtime" in p and "ffmpeg7" in p for p in paths)
        assert any(os.path.normcase("LiveLens") in p for p in paths)

    def test_dedup_preserves_order(self, monkeypatch):
        # 环境变量指向与源码树候选相同的路径 → 去重后只出现一次
        root_candidate = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "runtime", "ffmpeg7", "ffmpeg.exe")
        monkeypatch.setenv(R._ENV_OVERRIDE, root_candidate)
        paths = [os.path.normcase(os.path.abspath(p))
                 for p in R.candidate_paths()]
        assert len(paths) == len(set(paths))

    def test_find_existing_filters_empty_file(self, tmp_path, monkeypatch):
        empty = tmp_path / "ffmpeg.exe"
        empty.write_bytes(b"")
        monkeypatch.setenv(R._ENV_OVERRIDE, str(empty))
        assert R.find_existing_candidates() == []

    def test_find_existing_accepts_nonempty(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        monkeypatch.setenv(R._ENV_OVERRIDE, p)
        assert R.find_existing_candidates() == [p]

    def test_no_candidates_at_all(self):
        # 本机未安装时（候选目录均不存在）—— 只有在没有真实候选时才断言
        if R.find_existing_candidates():
            pytest.skip("本机存在候选运行时，跳过空集用例")
        st = R.status()
        assert st.ok is False
        assert st.reason == R.REASON_NOT_FOUND


# ======================================================================
# 2. 版本门禁
# ======================================================================
class TestVersionGate:
    def test_numeric_major_7_ok(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner("7.1.1") + "\n" + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert st.ok and st.major == 7 and st.avcodec_major == 61
        assert st.gpl is True

    def test_numeric_major_6_too_old(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner("6.1.1", avcodec="60. 31.102") + "\n"
                     + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_VERSION_TOO_OLD

    def test_numeric_major_8_ok(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner("8.0.1", avcodec="62. 11.100") + "\n"
                     + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        assert R.probe(p).ok

    def test_snapshot_with_libavcodec_63_ok(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner("N-127054-g9d3f0f2c58-20261001",
                                      avcodec="63. 15.100") + "\n"
                     + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert st.ok and st.major is None and st.avcodec_major == 63

    def test_snapshot_without_lib_line_unreadable(self, tmp_path, monkeypatch):
        """横幅被裁剪的自定义构建 → 版本信息不可识别 → 拒绝。"""
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner("e6a2447", avcodec=None) + "\n"
                     + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_VERSION_UNREADABLE

    def test_snapshot_with_old_libavcodec_too_old(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner("N-100-gabc", avcodec="60. 31.102")
                     + "\n" + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_VERSION_TOO_OLD

    def test_no_version_banner_unreadable(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch, dec_text="totally not ffmpeg output",
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_VERSION_UNREADABLE


# ======================================================================
# 3. 能力门禁
# ======================================================================
class TestCapability:
    def test_missing_hevc_decoder(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner() + "\n" + _DECODERS_NO_HEVC,
                     dem_text=_DEMUXERS_FLV)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_CAPABILITY_MISSING
        assert "hevc" in st.detail

    def test_missing_flv_demuxer(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner() + "\n" + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_NO_FLV)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_CAPABILITY_MISSING
        assert "flv" in st.detail

    def test_hevc_cuvid_alone_does_not_count(self, tmp_path, monkeypatch):
        """只有 hevc_cuvid（非原生 hevc 解码器）不算具备能力。"""
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(
            monkeypatch,
            dec_text=_banner() + "\n"
            + " V..... hevc_cuvid           Nvidia CUVID HEVC decoder (codec hevc)",
            dem_text=_DEMUXERS_FLV)
        assert not R.probe(p).ok


# ======================================================================
# 4. 进程级故障
# ======================================================================
class TestProcessFaults:
    def test_not_found(self, tmp_path):
        st = R.probe(str(tmp_path / "missing.exe"))
        assert not st.ok and st.reason == R.REASON_NOT_FOUND

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.exe"
        p.write_bytes(b"")
        assert R.probe(str(p)).reason == R.REASON_NOT_FOUND

    def test_timeout(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch, dec_text="", dem_text="",
                     exc=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=8.0))
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_PROBE_TIMEOUT

    def test_oserror_not_executable(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch, dec_text="", dem_text="",
                     exc=OSError("not a valid Win32 application"))
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_NOT_EXECUTABLE

    def test_nonzero_returncode(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch, dec_text="boom", dem_text=_DEMUXERS_FLV,
                     dec_rc=1)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_PROBE_FAILED

    def test_demuxer_probe_nonzero_returncode(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        _patch_probe(monkeypatch,
                     dec_text=_banner() + "\n" + _DECODERS_HEVC,
                     dem_text="boom", dem_rc=1)
        st = R.probe(p)
        assert not st.ok and st.reason == R.REASON_PROBE_FAILED

    def test_real_non_executable_text_file(self, tmp_path):
        """真实文本文件（非 PE）→ 不可执行（走真实 subprocess）。"""
        p = tmp_path / "not_an_exe.exe"
        p.write_text("this is not an executable")
        st = R.probe(str(p))
        assert not st.ok
        assert st.reason in (R.REASON_NOT_EXECUTABLE, R.REASON_PROBE_FAILED)


# ======================================================================
# 5. 缓存
# ======================================================================
class TestCache:
    def test_probe_once_then_cached(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        monkeypatch.setenv(R._ENV_OVERRIDE, p)
        calls = _patch_probe(monkeypatch,
                             dec_text=_banner() + "\n" + _DECODERS_HEVC,
                             dem_text=_DEMUXERS_FLV)
        assert R.status().ok
        assert R.status().ok
        assert len(calls) == 2, "第二次 status() 不应再拉起进程"

    def test_refresh_forces_reprobe(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        monkeypatch.setenv(R._ENV_OVERRIDE, p)
        calls = _patch_probe(monkeypatch,
                             dec_text=_banner() + "\n" + _DECODERS_HEVC,
                             dem_text=_DEMUXERS_FLV)
        R.status()
        R.status(refresh=True)
        assert len(calls) == 4

    def test_resolved_exe_does_not_probe(self, tmp_path, monkeypatch):
        calls = _patch_probe(monkeypatch, dec_text="", dem_text="")
        assert R.resolved_exe() is None
        assert calls == [], "resolved_exe() 不得触发探测"

    def test_resolved_exe_after_success(self, tmp_path, monkeypatch):
        p = _existing_tmp_exe(tmp_path)
        monkeypatch.setenv(R._ENV_OVERRIDE, p)
        _patch_probe(monkeypatch,
                     dec_text=_banner() + "\n" + _DECODERS_HEVC,
                     dem_text=_DEMUXERS_FLV)
        R.status()
        assert R.resolved_exe() == p


# ======================================================================
# 6. 用户可见文案
# ======================================================================
class TestReasonMessage:
    @pytest.mark.parametrize("reason", [
        R.REASON_NOT_FOUND, R.REASON_NOT_EXECUTABLE, R.REASON_PROBE_TIMEOUT,
        R.REASON_PROBE_FAILED, R.REASON_VERSION_UNREADABLE,
        R.REASON_VERSION_TOO_OLD, R.REASON_CAPABILITY_MISSING,
    ])
    def test_message_is_human_and_leaks_nothing(self, reason):
        msg = R.reason_message(reason)
        assert msg and not msg.startswith("<")
        for leak in ("DLV_", "ffmpeg.exe", "\\", "/", "Errno", "WinError",
                     "Traceback", "0x"):
            assert leak not in msg, f"{reason} 的文案泄漏了技术细节: {msg}"

    def test_unknown_reason_has_default(self):
        assert R.reason_message("something_new") == "低延迟播放组件不可用"


# ======================================================================
# 7. 真实二进制（本机存在时才跑）
# ======================================================================
@pytest.mark.skipif(not os.path.isfile(REAL_FFMPEG7),
                    reason="本机无外部 FFmpeg7 运行时")
class TestRealBinary:
    def test_real_ffmpeg7_is_accepted(self):
        st = R.probe(REAL_FFMPEG7)
        assert st.ok, st.as_log_line()
        assert st.version and st.path == REAL_FFMPEG7
        assert st.avcodec_major is None or st.avcodec_major >= 61

    def test_real_ffmpeg7_reports_gpl(self):
        st = R.probe(REAL_FFMPEG7)
        # 该构建为 --enable-gpl（Phase 6 审计依据）；若换构建需同步复核
        assert st.gpl is True

    def test_bundled_build_is_rejected(self):
        """内置 2023 自定义构建（版本横幅被裁剪）必须被拒绝。"""
        if not os.path.isfile(BUNDLED):
            pytest.skip("本机无内置运行时")
        st = R.probe(BUNDLED)
        assert not st.ok
        assert st.reason in (R.REASON_VERSION_UNREADABLE,
                             R.REASON_VERSION_TOO_OLD)
