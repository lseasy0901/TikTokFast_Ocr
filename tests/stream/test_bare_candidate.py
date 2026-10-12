# -*- coding: utf-8 -*-
"""B4: 裸流候选派生与门禁测试。"""
import os

import pytest

from stream.bare_candidate import bare_candidate_from, derive_bare_url
from stream.models import StreamCandidate

UHD_URL = "https://pull-flv-q1.douyincdn.com/x/stream-123_uhd.flv?k=1&x=2"


def _cand(url=UHD_URL, protocol="flv"):
    return StreamCandidate(url=url, protocol=protocol, quality="FULL_HD1")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DLV_BARE_STREAM", raising=False)


class TestDeriveBareUrl:
    def test_uhd_derived(self):
        assert derive_bare_url(UHD_URL) == \
            "https://pull-flv-q1.douyincdn.com/x/stream-123.flv?k=1&x=2"

    def test_hd_derived(self):
        assert derive_bare_url("https://h/stream-9_hd.flv") == \
            "https://h/stream-9.flv"

    def test_bare_itself_not_rederived(self):
        assert derive_bare_url("https://h/stream-9.flv") is None

    def test_hls_rejected(self):
        assert derive_bare_url("https://h/index.m3u8") is None

    def test_non_flv_rejected(self):
        assert derive_bare_url("") is None
        assert derive_bare_url(None) is None

    def test_or4_excluded(self):
        # or4 = 原画+摄像头合成档，与裸流内容不同源，不派生
        assert derive_bare_url("https://h/stream-9_or4.flv") is None


class TestGate:
    def test_gate_off_returns_none(self):
        assert bare_candidate_from(_cand()) is None

    def test_gate_on_derives_with_runtime(self, monkeypatch):
        monkeypatch.setenv("DLV_BARE_STREAM", "1")
        b = bare_candidate_from(_cand())
        assert b is not None
        assert b.url == "https://pull-flv-q1.douyincdn.com/x/stream-123.flv?k=1&x=2"
        assert b.runtime == "ffmpeg7"
        assert b.protocol == "flv"
        assert b.quality == "FULL_HD1"

    def test_gate_on_non_derivable_returns_none(self, monkeypatch):
        monkeypatch.setenv("DLV_BARE_STREAM", "1")
        assert bare_candidate_from(_cand("https://h/index.m3u8", "hls")) is None

    def test_original_candidate_untouched(self, monkeypatch):
        monkeypatch.setenv("DLV_BARE_STREAM", "1")
        c = _cand()
        bare_candidate_from(c)
        assert c.url == UHD_URL
        assert c.runtime is None
