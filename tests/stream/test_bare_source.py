# -*- coding: utf-8 -*-
"""RELEASE-02-B5: 裸流总开关 / 平台适用性 / 页面直取判据测试。

要点（相对 B5 灰度版的语义变更）：
  - 裸流是**默认正常路径**：不设任何环境变量即启用；
  - 只有显式 `DLV_BARE_STREAM=0/false/no/off` 才关闭；
  - **不再要求** `DLV_FFMPEG7_EXE`（运行时可用性由 utils.ffmpeg7_runtime 负责）；
  - 非抖音平台恒不适用 —— 不请求页面、不等运行时。

全部离线：`_fetch_page` 一律被替换，绝不发起真实网络请求。
"""
import os

import pytest

from stream import bare_source as B


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("DLV_BARE_STREAM", "DLV_FFMPEG7_EXE"):
        monkeypatch.delenv(k, raising=False)
    B._CACHE.clear()
    yield
    B._CACHE.clear()


# ======================================================================
# 1. 总开关
# ======================================================================
class TestEnabled:
    def test_default_on_without_any_env(self):
        """默认开启 —— 普通用户无需设置任何环境变量。"""
        assert B.bare_stream_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", " off "])
    def test_explicit_off(self, monkeypatch, val):
        monkeypatch.setenv("DLV_BARE_STREAM", val)
        assert B.bare_stream_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "", "anything"])
    def test_other_values_stay_on(self, monkeypatch, val):
        monkeypatch.setenv("DLV_BARE_STREAM", val)
        assert B.bare_stream_enabled() is True

    def test_no_longer_requires_ffmpeg7_env(self, monkeypatch):
        """关键语义变更：未设置 DLV_FFMPEG7_EXE 也仍然启用。"""
        monkeypatch.delenv("DLV_FFMPEG7_EXE", raising=False)
        assert B.bare_stream_enabled() is True


# ======================================================================
# 2. 平台适用性
# ======================================================================
class TestApplicable:
    def test_douyin_applicable_by_default(self):
        assert B.bare_stream_applicable("douyin") is True

    @pytest.mark.parametrize("platform", ["huya", "bilibili", "Douyin", "DOUYIN"])
    def test_case_insensitive(self, platform):
        assert B.bare_stream_applicable(platform) is (platform.lower() == "douyin")

    @pytest.mark.parametrize("platform", ["huya", "bilibili", "", None, "fake"])
    def test_other_platforms_not_applicable(self, platform):
        assert B.bare_stream_applicable(platform) is False

    def test_off_switch_wins(self, monkeypatch):
        monkeypatch.setenv("DLV_BARE_STREAM", "0")
        assert B.bare_stream_applicable("douyin") is False


# ======================================================================
# 3. 页面判据（纯函数）
# ======================================================================
_VIDEO_BARE = ("https://pull-flv-l1.douyincdn.com/stage/stream-408474773338456894.flv"
               "?expire=1700000000&sign=abc&unique_id=stream-408474773338456894"
               "&t_id=xyz&s_anchor=1")
_AUDIO_BARE = ("https://pull-flv-l1.douyincdn.com/stage/stream-408474773338456894.flv"
               "?expire=1700000000&sign=abc&unique_id=stream-408474773338456894"
               "&t_id=xyz&only_audio=1")
_TRUNCATED = ("https://pull-flv-l1.douyincdn.com/stage/stream-408474773338456894.flv"
              "?keeptime=123")


class TestExtractVideoBare:
    def test_video_bare_found(self):
        assert B.extract_video_bare(_VIDEO_BARE) == _VIDEO_BARE

    def test_only_audio_rejected(self):
        assert B.extract_video_bare(_AUDIO_BARE) is None

    def test_truncated_fragment_rejected(self):
        assert B.extract_video_bare(_TRUNCATED) is None

    def test_transcoded_variant_rejected(self):
        u = ("https://h/stage/stream-408474773338456894_uhd.flv"
             "?unique_id=stream-408474773338456894&t_id=x")
        assert B.extract_video_bare(u) is None

    def test_empty_input(self):
        assert B.extract_video_bare("") is None
        assert B.extract_video_bare(None) is None

    def test_picks_video_among_audio_and_variants(self):
        page = " ".join([_AUDIO_BARE, _TRUNCATED, _VIDEO_BARE])
        assert B.extract_video_bare(page) == _VIDEO_BARE

    def test_backslash_escaped_url_not_matched_known_limitation(self):
        """**已知限制（沿用 B5 现有解析，本次未改动）**：

        裸流 URL 的正则字符类排除了反斜杠，因此 `\\u0026` 形态的转义会把 URL
        截断（`?expire=1` 之后丢失），判据不满足 → 返回 None → 正常降级到标准
        路径。实测页面中裸流 URL 以原始 `&` 出现（B5 已在真实房间验证取流成功），
        且 `&amp;` 形态可正常识别，故不影响正常取流。
        若平台将来改为 `\\u0026` 形态，此处会静默降级而非取错——不会误报成功。
        """
        escaped = _VIDEO_BARE.replace("&", "\\u0026")
        assert B.extract_video_bare(escaped) is None

    def test_html_entity_ampersand(self):
        assert B.extract_video_bare(_VIDEO_BARE.replace("&", "&amp;")) == _VIDEO_BARE

    def test_page_structure_change_yields_none(self):
        """页面结构变化 → 取不到裸流（正常降级，不抛异常）。"""
        assert B.extract_video_bare("<html>no stream here</html>") is None


# ======================================================================
# 4. video_bare_url_for 的边界（离线）
# ======================================================================
class TestVideoBareUrlFor:
    def test_non_douyin_never_touches_network(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("非抖音平台不得请求页面")
        monkeypatch.setattr(B, "_fetch_page", _boom)
        assert B.video_bare_url_for("huya", "123") is None
        assert B.video_bare_url_for("bilibili", "123") is None

    def test_switch_off_never_touches_network(self, monkeypatch):
        monkeypatch.setenv("DLV_BARE_STREAM", "0")

        def _boom(*a, **kw):
            raise AssertionError("总开关关闭时不得请求页面")
        monkeypatch.setattr(B, "_fetch_page", _boom)
        assert B.video_bare_url_for("douyin", "123") is None

    def test_empty_room_id(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("房间号为空时不得请求页面")
        monkeypatch.setattr(B, "_fetch_page", _boom)
        assert B.video_bare_url_for("douyin", "") is None

    def test_fetch_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(B, "_fetch_page", lambda url: None)
        assert B.video_bare_url_for("douyin", "123") is None

    def test_success_and_cache(self, monkeypatch):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return _VIDEO_BARE
        monkeypatch.setattr(B, "_fetch_page", fake_fetch)

        assert B.video_bare_url_for("douyin", "123") == _VIDEO_BARE
        assert B.video_bare_url_for("douyin", "123") == _VIDEO_BARE
        assert len(calls) == 1, "45s TTL 内应命中缓存，不重复请求页面"

    def test_different_rooms_not_shared(self, monkeypatch):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return _VIDEO_BARE
        monkeypatch.setattr(B, "_fetch_page", fake_fetch)
        B.video_bare_url_for("douyin", "111")
        B.video_bare_url_for("douyin", "222")
        assert len(calls) == 2

    def test_cache_negative_result_too(self, monkeypatch):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return None
        monkeypatch.setattr(B, "_fetch_page", fake_fetch)
        assert B.video_bare_url_for("douyin", "123") is None
        assert B.video_bare_url_for("douyin", "123") is None
        assert len(calls) == 1, "取不到也应缓存，避免反复打页面"


# ======================================================================
# 5. 有界超时
# ======================================================================
class TestTimeoutBound:
    def test_fetch_timeout_is_bounded(self):
        """正式接入后页面请求必须有界（B5 原实现为 20s，已收紧）。"""
        assert 0 < B.FETCH_TIMEOUT_S <= 10.0
