#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feijipan resolver 服务端封装（services/feijipan_resolver.py）的回归测试。

约定与 license_server 下其它 test_*.py 一致：扁平脚本、check() 自行统计。

测试策略
--------
不需要访问真实小飞机：测试起一个**真实的本地 aiohttp 服务端**，按上游同样的
三步链路（vip → recommend → file/redirect）响应，客户端通过 ``api_base``
指向它。因此跑的是真实的 aiohttp 请求链与真实的 AES 算法，只有对端是假的。

覆盖：
  1 合法 share URL（三个允许域名）通过
  2 非允许域名被拒绝
  3 非 /s/<id> 路径被拒绝
  4 无效 share URL 被拒绝
  5 AES / 内部解析失败 → 明确失败（不再静默回落到替代算法）
  6 超时 / 网络异常 → 明确失败，不崩溃
  7 成功结果结构
  8 direct_url 只存在于返回值，不被持久化
  9 日志中不出现完整 direct_url

真实临时直链**不写入本文件**：测试用的是自造的哨兵 URL。
"""

import asyncio
import contextlib
import logging
import pathlib
import sys

import aiohttp
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))  # 与服务器运行方式一致：app/ 在 sys.path 上

from services import feijipan_resolver as fr  # noqa: E402

_PASS = []
_FAIL = []

#: 自造哨兵直链。真实直链绝不写进测试文件。
SENTINEL_DIRECT = "https://cdn.invalid.example/download/SENTINEL-DIRECT-TOKEN-9f3a"

#: 一条形状正确的上游文件信息响应。
RECOMMEND_OK = {
    "code": 200,
    "list": [
        {
            "fileIds": "FILE-ID-1",
            "userId": "USER-ID-1",
            "shareId": "gg95ZrAn",
            "map": {"userId": "USER-ID-1"},
            "fileList": [
                {"fileName": "LiveLensDesktop-1.1.1-stable-full.nupkg",
                 "fileSize": 267541653, "fileType": 1}
            ],
        }
    ],
}


#: 多分享项 / 多文件的上游响应（Phase 1.5）：目标 nupkg 在**第二个分享项**里，
#: 用来证明「不会错误地取第一个文件」，也证明用的是命中项的 fileIds/userId。
RECOMMEND_MULTI = {
    "code": 200,
    "list": [
        {
            "fileIds": "ZIP-FILE-ID",
            "userId": None,               # 故意缺失：走 map.userId 兜底
            "shareId": "share-1",
            "map": {"userId": "ZIP-USER"},
            "fileList": [
                {"fileName": "DouyinLowLatencyViewer.zip",
                 "fileSize": 270554, "fileType": 1},
            ],
        },
        {
            "fileIds": "NUPKG-FILE-ID",
            "userId": "NUPKG-USER",
            "shareId": "share-2",
            "map": {"userId": "NUPKG-USER"},
            "fileList": [
                {"fileName": "LiveLensDesktop-1.1.1-stable-full.nupkg",
                 "fileSize": 267541653, "fileType": 1},
            ],
        },
    ],
}

TARGET_NUPKG = "LiveLensDesktop-1.1.1-stable-full.nupkg"


def check(condition, message, detail=""):
    (_PASS if condition else _FAIL).append(message)
    suffix = f"  -- {detail}" if (detail and not condition) else ""
    print(f"{'PASS' if condition else 'FAIL'}  {message}{suffix}")


# ----------------------------------------------------------------------
# 本地假上游
# ----------------------------------------------------------------------
@contextlib.contextmanager
def client_points_at(api_base, **kw):
    """把模块级 ``resolve()`` 会构造的 FeijipanClient 指向本地服务端。

    ``resolve()`` 的签名是写死的 ``(share_url, share_password="")``，不接受
    api_base（那是刻意的：它是给 FastAPI 层用的稳定契约）。因此测试只能在
    模块上替换这个类，而不是给它加参数。
    """
    original = fr.FeijipanClient
    fr.FeijipanClient = lambda: original(api_base=api_base, **kw)
    try:
        yield
    finally:
        fr.FeijipanClient = original


def _make_app(recommend_payload, redirect_location=SENTINEL_DIRECT,
              redirect_status=302, delay=0.0, raw_recommend=False, capture=None):
    async def vip(request):
        if delay:
            await asyncio.sleep(delay)
        return web.json_response({"code": 200, "msg": "ok"})

    async def recommend(request):
        if raw_recommend:
            # 上游返回的不是 JSON（例如错误页 HTML）。
            return web.Response(text="<html>not json</html>",
                                content_type="text/html")
        # 上游要求 shareId 必填；缺了就报错，与真实行为一致。
        if not request.query.get("shareId"):
            return web.json_response({"code": -1, "msg": "分享id不能为空！"})
        return web.json_response(recommend_payload)

    async def redirect(request):
        if capture is not None:
            # 记录服务端实际收到的参数，用于断言「用的是哪个文件的 id」。
            capture["redirect_query"] = dict(request.query)
        if redirect_status in (301, 302, 303, 307, 308):
            return web.Response(status=redirect_status,
                                headers={"Location": redirect_location})
        return web.Response(status=redirect_status, text="no location")

    app = web.Application()
    app.router.add_post("/ws/buy/vip/list", vip)
    app.router.add_post("/ws/recommend/list", recommend)
    app.router.add_get("/ws/file/redirect", redirect)
    return app


class LocalUpstream:
    """起一个本地 aiohttp 服务端，返回可直接喂给 FeijipanClient 的 api_base。"""

    def __init__(self, **kw):
        self._kw = kw
        self.runner = None
        self.api_base = None

    async def __aenter__(self):
        self.runner = web.AppRunner(_make_app(**self._kw))
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        host, port = self.runner.addresses[0]
        self.api_base = f"http://{host}:{port}/ws/"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


# ----------------------------------------------------------------------
# 1 / 4 — URL 校验
# ----------------------------------------------------------------------
def test_valid_urls_accepted():
    print("\n[1] 合法 share URL")
    for url, key in (
        ("https://share.feijipan.com/s/gg95ZrAn", "gg95ZrAn"),
        ("https://www.feijix.com/s/AbC123", "AbC123"),
        ("https://www.feijipan.com/s/x_y-z", "x_y-z"),
        ("https://share.feijipan.com/s/gg95ZrAn?pwd=1234", "gg95ZrAn"),
    ):
        try:
            check(fr.validate_share_url(url) == key, f"接受 {url}", "key 不匹配")
        except fr.FeijipanResolveError as e:
            check(False, f"接受 {url}", str(e))


def test_bad_hosts_rejected():
    print("\n[2] 非允许域名")
    for url in (
        "https://evil.com/s/abc",
        "https://api.feijipan.com/s/abc",
        "https://feijipan.com/s/abc",
        # 后缀欺骗：以允许域名为前缀的另一个域
        "https://share.feijipan.com.evil.com/s/abc",
        # 非 https
        "http://share.feijipan.com/s/abc",
        "ftp://share.feijipan.com/s/abc",
        # 带认证信息
        "https://user:pass@share.feijipan.com/s/abc",
    ):
        try:
            fr.validate_share_url(url)
            check(False, f"拒绝 {url}", "竟然通过了")
        except fr.FeijipanResolveError:
            check(True, f"拒绝 {url}")


def test_bad_paths_rejected():
    print("\n[3] 非 /s/<id> 路径")
    for url in (
        "https://share.feijipan.com/u/abc",
        "https://share.feijipan.com/",
        "https://share.feijipan.com/s/",
        "https://share.feijipan.com/s/a/b",
    ):
        try:
            fr.validate_share_url(url)
            check(False, f"拒绝 {url}", "竟然通过了")
        except fr.FeijipanResolveError:
            check(True, f"拒绝 {url}")


def test_invalid_url_inputs():
    print("\n[4] 无效 share URL")
    for value in (None, "", "   ", 12345, "not a url", "javascript:alert(1)",
                  "file:///etc/passwd"):
        try:
            fr.validate_share_url(value)
            check(False, f"拒绝 {value!r}", "竟然通过了")
        except fr.FeijipanResolveError:
            check(True, f"拒绝 {value!r}")


# ----------------------------------------------------------------------
# 5 — AES 失败必须明确失败
# ----------------------------------------------------------------------
def test_aes_failure_is_explicit():
    print("\n[5] AES / 内部解析失败")
    original = fr.AESUtils._get_cipher_aes0

    def boom(cls):
        raise fr.FeijipanResolveError("内置密钥解密失败: boom", code=500)

    try:
        fr.AESUtils._get_cipher_aes0 = classmethod(boom)
        try:
            fr.AESUtils.encrypt_to_hex("1234567890")
            check(False, "AES 失败时抛异常", "竟然返回了值")
        except fr.FeijipanResolveError as e:
            check(True, "AES 失败时抛异常")
            check(str(e).startswith("内置密钥解密失败"), "异常信息明确", str(e))
    finally:
        fr.AESUtils._get_cipher_aes0 = original

    # 内置密钥本身能否解出（原工具的 fallback 常量绝不能被用到）
    key = fr.AESUtils._get_cipher_aes0()
    check(key != "feijipan_default_key", "未回落到 feijipan_default_key", key[:20])
    check(len(fr.AESUtils._generate_key(key)) == 16, "派生密钥为 16 字节")

    # encrypt_to_hex 绝不再回落到 md5
    import hashlib
    out = fr.AESUtils.encrypt_to_hex("hello")
    check(out != hashlib.md5("hello".encode()).hexdigest(), "未回落到 md5 摘要")
    check(len(out) % 32 == 0 and all(c in "0123456789abcdef" for c in out),
          "输出为整块 hex")


def test_aes_is_deterministic_for_same_input():
    print("\n[5b] AES 同一输入结果稳定")
    a = fr.AESUtils.encrypt_to_hex("FILE|USER")
    b = fr.AESUtils.encrypt_to_hex("FILE|USER")
    check(a == b == fr.AESUtils.encrypt_to_hex("FILE|USER"), "同一输入 → 同一输出")
    check(a != fr.AESUtils.encrypt_to_hex("FILE|USER2"), "不同输入 → 不同输出")


# ----------------------------------------------------------------------
# 6 — 网络异常 / 超时
# ----------------------------------------------------------------------
async def test_network_error():
    print("\n[6a] 网络异常（端口无人监听）")
    # 找一个几乎肯定没人监听的端口：绑定后立刻释放。
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    url = f"http://127.0.0.1:{dead_port}/ws/"

    # parse() 与原工具一致：返回 code!=200 的字典。
    try:
        r = await fr.FeijipanClient(api_base=url, timeout_seconds=5).parse(
            "https://share.feijipan.com/s/gg95ZrAn")
        check(r["code"] != 200 and r["url"] is None,
              "网络异常 → parse() 返回失败且无 url", str(r))
    except Exception as e:  # noqa: BLE001
        check(False, "网络异常 → parse() 返回失败且无 url",
              f"竟然抛了 {type(e).__name__}: {e}")

    # resolve() 是服务端入口：网络异常必须抛异常。
    with client_points_at(url, timeout_seconds=5):
        try:
            await fr.resolve("https://share.feijipan.com/s/gg95ZrAn")
            check(False, "网络异常 → resolve() 抛 FeijipanResolveError", "竟然返回了")
        except fr.FeijipanResolveError as e:
            check(True, "网络异常 → resolve() 抛 FeijipanResolveError", str(e))


async def test_timeout():
    print("\n[6b] 超时")
    async with LocalUpstream(recommend_payload=RECOMMEND_OK, delay=2.0) as up:
        client = fr.FeijipanClient(api_base=up.api_base, timeout_seconds=0.4)
        try:
            r = await client.parse("https://share.feijipan.com/s/gg95ZrAn")
            check(r["code"] != 200 and r["url"] is None,
                  "超时 → parse() 返回失败且无 url", str(r))
        except Exception as e:  # noqa: BLE001
            check(False, "超时 → parse() 返回失败且无 url",
                  f"竟然抛了 {type(e).__name__}: {e}")

        with client_points_at(up.api_base, timeout_seconds=0.4):
            try:
                await fr.resolve("https://share.feijipan.com/s/gg95ZrAn")
                check(False, "超时 → resolve() 抛 FeijipanResolveError", "竟然返回了")
            except fr.FeijipanResolveError as e:
                check(True, "超时 → resolve() 抛 FeijipanResolveError", str(e))


async def test_upstream_error_responses():
    print("\n[6c] 上游异常响应")
    # 上游业务失败
    async with LocalUpstream(recommend_payload={"code": -1, "msg": "分享已失效"}) as up:
        r = await fr.FeijipanClient(api_base=up.api_base).parse(
            "https://share.feijipan.com/s/gg95ZrAn")
        check(r["code"] != 200 and r["url"] is None, "上游失败 → code!=200 且无 url", str(r))
        check("分享已失效" in (r["message"] or ""), "上游消息被如实转达", str(r))

    # 文件列表为空
    async with LocalUpstream(recommend_payload={"code": 200, "list": []}) as up:
        r = await fr.FeijipanClient(api_base=up.api_base).parse(
            "https://share.feijipan.com/s/gg95ZrAn")
        check(r["code"] == 404 and r["url"] is None, "空列表 → 404", str(r))

    # 响应不是 JSON（上游错误页 HTML）
    async with LocalUpstream(recommend_payload=RECOMMEND_OK,
                             raw_recommend=True) as up:
        r = await fr.FeijipanClient(api_base=up.api_base).parse(
            "https://share.feijipan.com/s/gg95ZrAn")
        check(r["code"] == 500 and r["url"] is None, "非 JSON → 500", str(r))

    # 文件夹
    folder_payload = {
        "code": 200,
        "list": [{"fileIds": "F", "userId": "U",
                  "fileList": [{"fileName": "dir", "fileType": 2}]}],
    }
    async with LocalUpstream(recommend_payload=folder_payload) as up:
        r = await fr.FeijipanClient(api_base=up.api_base).parse(
            "https://share.feijipan.com/s/gg95ZrAn")
        check(r["code"] == 400 and "文件夹" in (r["message"] or ""),
              "文件夹被拒绝", str(r))


# ----------------------------------------------------------------------
# 7 / 8 / 9 — 成功结构、不持久化、日志不泄漏
# ----------------------------------------------------------------------
class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())

    @property
    def text(self):
        return "\n".join(self.records)


async def test_success_structure_and_no_leak():
    print("\n[7] 成功结果结构")
    logger = logging.getLogger("DouyinLowLatencyViewer.license.feijipan")
    collector = _Collector()
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)

    before_files = _snapshot_dir(ROOT)
    try:
        async with LocalUpstream(recommend_payload=RECOMMEND_OK) as up:
            client = fr.FeijipanClient(api_base=up.api_base)
            result = await client.parse("https://share.feijipan.com/s/gg95ZrAn")

            check(result["code"] == 200, "code == 200", str(result))
            check(result["message"] == "解析成功", "message == 解析成功")
            check(result["url"] == SENTINEL_DIRECT, "url 为上游 Location")
            check(result["file_name"] == "LiveLensDesktop-1.1.1-stable-full.nupkg",
                  "file_name 正确", str(result.get("file_name")))
            check(result["file_size"] == 267541653, "file_size 正确", str(result.get("file_size")))
            check(set(result) == {"code", "url", "message", "file_name", "file_size",
                                  "selection"},
                  "字段集合与约定一致", str(sorted(result)))
            check(result["selection"] == "first_file_compat",
                  "未传 filename → 明确标记为兼容模式", str(result.get("selection")))

            # resolve() 是服务端文档化入口（签名固定，不接受 api_base），
            # 因此这里把模块里的 FeijipanClient 指向本地服务端来验证它。
            with client_points_at(up.api_base):
                resolved = await fr.resolve("https://share.feijipan.com/s/gg95ZrAn")
                check(resolved["code"] == 200 and resolved["url"] == SENTINEL_DIRECT,
                      "resolve() 成功返回同样的结构", str(resolved))
                check(set(resolved) == {"code", "url", "message", "file_name",
                                        "file_size", "selection"},
                      "resolve() 字段集合一致", str(sorted(resolved)))

                # 失败时 resolve() 抛异常，而不是把失败当成功返回
                try:
                    await fr.resolve("https://evil.com/s/abc")
                    check(False, "非法域名 → resolve() 抛异常", "竟然返回了")
                except fr.FeijipanResolveError as e:
                    check(True, "非法域名 → resolve() 抛异常", str(e))
                    check(getattr(e, "code", None) == 400, "异常带业务码 400",
                          str(getattr(e, "code", None)))

            print("\n[8] direct_url 不被持久化")
            after_files = _snapshot_dir(ROOT)
            check(before_files == after_files, "解析过程未新建/修改任何文件")
            check(not hasattr(client, "url") and not hasattr(client, "direct_url"),
                  "客户端实例不持有直链")
            for attr in ("_last_url", "_direct_url", "_cache", "_results", "last_result"):
                check(not hasattr(fr, attr) and not hasattr(client, attr),
                      f"模块/实例无 {attr} 之类的缓存位")

            print("\n[9] 日志不泄漏直链")
            text = collector.text
            check(text != "", "确实产生了日志（否则该断言无意义）")
            check(SENTINEL_DIRECT not in text, "日志不含完整直链")
            check("SENTINEL-DIRECT-TOKEN" not in text, "日志不含直链 token")
            check("cdn.invalid.example" not in text, "日志不含直链 host")

        print("\n[10] 失败路径的日志同样不泄漏直链")
        # 让上游返回一个「带直链的错误」场景：302 但缺 Location
        collector.records.clear()
        async with LocalUpstream(recommend_payload=RECOMMEND_OK,
                                 redirect_status=302,
                                 redirect_location="") as up:
            r = await fr.FeijipanClient(api_base=up.api_base).parse(
                "https://share.feijipan.com/s/gg95ZrAn")
            check(r["code"] == 500 and r["url"] is None, "缺 Location → 500", str(r))
            check(SENTINEL_DIRECT not in collector.text, "失败日志不含直链")
    finally:
        logger.removeHandler(collector)


def _snapshot_dir(root: pathlib.Path):
    """目录指纹：路径 → (大小, mtime)。用于证明解析过程没有写文件。"""
    out = {}
    for p in sorted(root.rglob("*")):
        try:
            if p.is_file():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return out


def test_module_has_no_persistence_calls():
    print("\n[8b] 模块源码不含持久化/direct_url 落盘")
    src = pathlib.Path(fr.__file__).read_text(encoding="utf-8")
    lowered = src.lower()
    for needle, why in (
        ("open(", "不得写文件"),
        ("json.dump", "不得落盘"),
        ("session.add", "不得写数据库"),
        ("commit(", "不得提交事务"),
        (".write(", "不得写文件"),
    ):
        check(needle not in lowered, f"源码不含 {needle}（{why}）")


# ----------------------------------------------------------------------
# Phase 1.5 — filename 精确选择
# ----------------------------------------------------------------------
async def test_filename_exact_match():
    print("\n[11] filename 精确匹配成功（目标在第二个分享项里）")
    capture = {}
    before = _snapshot_dir(ROOT)

    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI,
                             capture=capture) as up:
        client = fr.FeijipanClient(api_base=up.api_base)
        r = await client.parse("https://share.feijipan.com/s/gg95ZrAn",
                               filename=TARGET_NUPKG)

        check(r["code"] == 200, "命中 → code 200", str(r))
        check(r["file_name"] == TARGET_NUPKG, "返回的是命中文件", str(r.get("file_name")))
        check(r["file_size"] == 267541653, "返回命中文件的大小", str(r.get("file_size")))
        check(r["selection"] == "filename_exact",
              "selection 标记精确匹配", str(r.get("selection")))
        check(r["url"] == SENTINEL_DIRECT, "url 为上游 Location")

        # 命中的是第二个分享项 → 必须用那一项的 fileIds/userId 生成 downloadId
        expected = fr.AESUtils.encrypt_to_hex("NUPKG-FILE-ID|NUPKG-USER")
        got = (capture.get("redirect_query") or {}).get("downloadId")
        check(got == expected, "用的是命中文件所在分享项的 fileIds/userId",
              "downloadId 与命中项不符")
        check((capture.get("redirect_query") or {}).get("shareId") == "gg95ZrAn",
              "redirect 带上了 shareId")

        # direct_url 不持久化
        check(_snapshot_dir(ROOT) == before, "精确匹配路径未写任何文件")
        check(not hasattr(client, "url") and not hasattr(client, "direct_url"),
              "客户端实例不持有直链")

    # resolve() 也支持 filename
    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI) as up:
        with client_points_at(up.api_base):
            r = await fr.resolve("https://share.feijipan.com/s/gg95ZrAn",
                                 filename=TARGET_NUPKG)
            check(r["code"] == 200 and r["file_name"] == TARGET_NUPKG,
                  "resolve(filename=...) 命中", str(r.get("file_name")))


async def test_filename_not_found():
    print("\n[12] filename 不存在 → 404，绝不回退第一个文件")
    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI) as up:
        client = fr.FeijipanClient(api_base=up.api_base)
        missing = "LiveLensDesktop-9.9.9-stable-full.nupkg"
        r = await client.parse("https://share.feijipan.com/s/gg95ZrAn",
                               filename=missing)

        check(r["code"] == 404, "code == 404（FILE_NOT_FOUND）", str(r))
        check(r["url"] is None, "不返回 url", str(r))
        check(missing in (r["message"] or ""), "消息里点明是哪个文件", str(r))
        check(r.get("file_name") is None,
              "没有拿其它文件（尤其是第一个）顶上", str(r))
        check("selection" not in r, "失败结果不带 selection（没有选中任何文件）", str(r))

    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI) as up:
        with client_points_at(up.api_base):
            try:
                await fr.resolve("https://share.feijipan.com/s/gg95ZrAn",
                                 filename=missing)
                check(False, "resolve() 找不到时抛异常", "竟然返回了")
            except fr.FeijipanResolveError as e:
                check(True, "resolve() 找不到时抛异常", str(e))
                check(getattr(e, "code", None) == 404, "异常带 404", str(getattr(e, "code", None)))


async def test_filename_must_match_exactly():
    print("\n[13] 只有完全相等才算命中")
    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI) as up:
        client = fr.FeijipanClient(api_base=up.api_base)
        for bad in (
            "livelensdesktop-1.1.1-stable-full.nupkg",       # 大小写不同
            "LiveLensDesktop-1.1.1-stable-full",             # 少了扩展名
            "LiveLensDesktop-1.1.1-stable-full.nupkg.bak",   # 多了后缀
            " " + TARGET_NUPKG,                              # 前后空白
            TARGET_NUPKG + " ",
            "stable-full.nupkg",                             # 子串
        ):
            r = await client.parse("https://share.feijipan.com/s/gg95ZrAn",
                                   filename=bad)
            check(r["code"] == 404, f"非精确匹配被拒绝: {bad!r}", str(r))


async def test_empty_filename_rejected():
    print("\n[14] 空 filename 是调用方错误（不静默退回兼容模式）")
    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI) as up:
        client = fr.FeijipanClient(api_base=up.api_base)
        for bad in ("", "   "):
            try:
                await client.parse("https://share.feijipan.com/s/gg95ZrAn",
                                   filename=bad)
                check(False, f"空 filename {bad!r} 被拒绝", "竟然返回了")
            except fr.FeijipanResolveError as e:
                check(getattr(e, "code", None) == 400, f"空 filename {bad!r} → 400",
                      str(e))


async def test_compat_mode_unchanged():
    print("\n[15] 兼容模式（不传 filename）行为保持不变")
    async with LocalUpstream(recommend_payload=RECOMMEND_MULTI) as up:
        r = await fr.FeijipanClient(api_base=up.api_base).parse(
            "https://share.feijipan.com/s/gg95ZrAn")
        check(r["code"] == 200, "仍然成功", str(r))
        check(r["file_name"] == "DouyinLowLatencyViewer.zip",
              "仍取第一个分享项的第一个文件", str(r.get("file_name")))
        check(r["selection"] == "first_file_compat",
              "明确标记为兼容模式", str(r.get("selection")))


# ----------------------------------------------------------------------
def main():
    test_valid_urls_accepted()
    test_bad_hosts_rejected()
    test_bad_paths_rejected()
    test_invalid_url_inputs()
    test_aes_failure_is_explicit()
    test_aes_is_deterministic_for_same_input()
    test_module_has_no_persistence_calls()

    async def run_async():
        await test_network_error()
        await test_timeout()
        await test_upstream_error_responses()
        await test_success_structure_and_no_leak()
        # Phase 1.5
        await test_filename_exact_match()
        await test_filename_not_found()
        await test_filename_must_match_exactly()
        await test_empty_filename_rejected()
        await test_compat_mode_unchanged()

    asyncio.run(run_async())

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for name in _FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
