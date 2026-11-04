# -*- coding: utf-8 -*-
"""Cookie 缓存 TTL/refresh 语义回归(2026-10-25 排障改造)。

事故背景:部分用户"所有候选流均不可用"而同房间同时刻他人正常——
客户端指纹差异(cookie 被风控标记)是主要假设之一,旧缓存曾无 TTL 且
refresh 语义错误(只跳 TTL 检查仍返回旧缓存,自愈重试换不掉 ttwid)。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream.adapters.douyin import _load_cached_cookies, _save_cookies  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    p = Path(tmp_path) / "douyin_cookies.json"
    monkeypatch.setattr(
        "stream.adapters.douyin._cookie_store_path", lambda: p)
    return p


def _age(path, days):
    data = json.load(open(path, encoding="utf-8"))
    data["saved_at"] = time.time() - days * 86400
    json.dump(data, open(path, "w", encoding="utf-8"))


def test_fresh_cache_usable(store):
    _save_cookies({"ttwid": "fresh"})
    got = _load_cached_cookies()
    assert got and got["ttwid"] == "fresh"


def test_expired_cache_rejected(store):
    _save_cookies({"ttwid": "old"})
    _age(store, 8)                                   # > 7 天 TTL
    assert _load_cached_cookies() is None


def test_refresh_ignores_cache_even_when_fresh(store):
    _save_cookies({"ttwid": "stale-but-young"})
    assert _load_cached_cookies(refresh=True) is None


def test_missing_ttwid_rejected(store):
    _save_cookies({"not_ttwid": "x"})
    assert _load_cached_cookies() is None
