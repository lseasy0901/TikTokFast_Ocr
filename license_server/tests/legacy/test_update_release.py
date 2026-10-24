#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2：Release Registry + Velopack 更新分发 的回归测试。

约定与 license_server 下其它 test_*.py 一致：扁平脚本、check() 自行统计。

隔离
----
* ``DATABASE_URL`` / ``ADMIN_API_KEY`` 在 import 任何 app 模块**之前**写进环境变量，
  因此用的是一个临时 sqlite 文件，**不碰**仓库里的 license_server.db，也不用真实
  admin key。
* Feijipan resolver 被替换成假的（``services.feijipan_resolver.resolve``）：本测试
  不联网，也不依赖真实分享链接。真实解析另有独立验证。
* 假 resolver 返回的直链是**自造哨兵**，把「有没有泄漏/落库」变成可断言的事实。
"""

import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))

# ---- 必须在 import app 模块之前设置 -------------------------------------
_TMP = tempfile.mkdtemp(prefix="dlv-updrel-")
_DB_PATH = pathlib.Path(_TMP) / "test_update_release.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
_TEST_ADMIN_KEY = "phase-2-test-admin-key"
os.environ["ADMIN_API_KEY"] = _TEST_ADMIN_KEY

import asyncio  # noqa: E402

import httpx  # noqa: E402

import config  # noqa: E402
import main as server_main  # noqa: E402  （本文件自己的入口也叫 main，避免名字撞车）
import models  # noqa: E402
from services import feijipan_resolver as fr  # noqa: E402
from services import release_service  # noqa: E402


class _SyncClient:
    """同步测试客户端。

    不用 ``fastapi.testclient.TestClient``：它会向 ``httpx.Client`` 传 ``app=``，
    而本机 httpx 0.28 已移除该参数，版本组合不兼容。这里直接走 ASGITransport，
    请求行为等价（含 4xx/5xx 与未跟随的 307），且不依赖那个版本组合。
    """

    def __init__(self, app):
        self._app = app

    def _run(self, method, url, **kw):
        async def go():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                follow_redirects=False,          # 必须看到原始 307
            ) as c:
                return await c.request(method, url, **kw)
        return asyncio.run(go())

    def get(self, url, **kw):
        return self._run("GET", url, **kw)

    def post(self, url, **kw):
        return self._run("POST", url, **kw)

_PASS = []
_FAIL = []

SENTINEL_DIRECT = "https://cdn.invalid.example/download/SENTINEL-DIRECT-TOKEN-7c1f"

SHARE_URL = "https://share.feijipan.com/s/gg95ZrAn"
VERSION = "1.1.1"
FILENAME = "LiveLensDesktop-1.1.1-stable-full.nupkg"
SHA1 = "9D65794143F159D3397C6BB16326C7240B5EB79E"
SHA256 = "49E0B71F3737C38C49E582723BCF46A0569CBCEB200F1A7BE4A6F10564AD3EB3"
SIZE = 267541653
NOTES = "## LiveLens 1.1.1\n\n- Phase 2：动态更新分发"

#: 假分享里「存在」的文件。
FAKE_FILES = {FILENAME: {"size": SIZE}}
# [17] 尾斜杠回归用的第二个文件（不同版本，避免撞 (channel, version) 唯一约束）
FAKE_FILES["LiveLensDesktop-9.8.7-stable-delta.nupkg"] = {"size": SIZE}


def check(condition, message, detail=""):
    (_PASS if condition else _FAIL).append(message)
    suffix = f"  -- {detail}" if (detail and not condition) else ""
    print(f"{'PASS' if condition else 'FAIL'}  {message}{suffix}")


# ----------------------------------------------------------------------
# 假 resolver
# ----------------------------------------------------------------------
_REAL_RESOLVE = fr.resolve


async def _fake_resolve(share_url, share_password="", filename=None):
    """只认 FAKE_FILES；找不到就按 resolver 的真实语义抛 404。

    share_url 先 rstrip("/")：与真实 parse() 的 ``/s/([^/?]+)`` 提取语义一致
    （尾斜杠不改变分享身份，v2.1.0 排障修复的回归覆盖点）。
    """
    if filename is None:
        raise fr.FeijipanResolveError("测试必须传 filename", code=500)
    if share_url.rstrip("/") not in (SHARE_URL,):
        raise fr.FeijipanResolveError("分享中未找到文件: " + filename, code=404)
    if filename not in FAKE_FILES:
        raise fr.FeijipanResolveError("分享中未找到文件: " + filename, code=404)
    return {
        "code": 200,
        "url": SENTINEL_DIRECT,          # 自造哨兵，绝不是真实直链
        "message": "解析成功",
        "file_name": filename,
        "file_size": FAKE_FILES[filename]["size"],
        "selection": "filename_exact",
    }


def _payload(**over):
    body = {
        "version": VERSION,
        "channel": "stable",
        "package_filename": FILENAME,
        "package_share_url": SHARE_URL,
        "release_notes": NOTES,
        "package_sha1": SHA1,
        "package_sha256": SHA256,
        "package_size": SIZE,
    }
    body.update(over)
    return body


ADMIN = {"X-API-Key": _TEST_ADMIN_KEY}


# ----------------------------------------------------------------------
def main() -> int:
    fr.resolve = _fake_resolve           # 全局替换（release_service / main 都引同一模块）
    client = _SyncClient(server_main.app)

    print("\n[2] admin API 无 key / 错 key")
    r = client.post("/api/v1/updates/releases", json=_payload())
    check(r.status_code in (401, 403), "无 key → 401/403", str(r.status_code))
    r = client.post("/api/v1/updates/releases", json=_payload(),
                    headers={"X-API-Key": "wrong-key"})
    check(r.status_code in (401, 403), "错 key → 401/403", str(r.status_code))
    check(_row_count() == 0, "未授权请求没有写入任何 release", str(_row_count()))

    print("\n[4] 非法 package_share_url 被拒绝")
    for bad in ("https://evil.com/s/abc",
                "http://share.feijipan.com/s/abc",
                "https://share.feijipan.com/u/abc",
                "https://share.feijipan.com.evil.com/s/abc",
                "not-a-url"):
        r = client.post("/api/v1/updates/releases",
                        json=_payload(package_share_url=bad), headers=ADMIN)
        check(r.status_code == 400, f"拒绝 {bad!r} → 400", str(r.status_code))
    check(_row_count() == 0, "非法 share_url 未写入 release", str(_row_count()))

    print("\n[4b] 其它字段校验")
    # 太短 → 被 pydantic 的 min_length 拦下（422）；形状对但段数不对 → 服务层 400。
    r = client.post("/api/v1/updates/releases", json=_payload(version="1.1"),
                    headers=ADMIN)
    check(r.status_code in (400, 422), "过短 version → 400/422", str(r.status_code))
    r = client.post("/api/v1/updates/releases", json=_payload(version="1.1.1.1"),
                    headers=ADMIN)
    check(r.status_code == 400, "四段式 version → 400", str(r.status_code))
    r = client.post("/api/v1/updates/releases", json=_payload(version="1.1.x"),
                    headers=ADMIN)
    check(r.status_code == 400, "非数字段 version → 400", str(r.status_code))
    r = client.post("/api/v1/updates/releases", json=_payload(channel="beta"),
                    headers=ADMIN)
    check(r.status_code == 400, "未支持 channel → 400", str(r.status_code))
    for bad_fn in ("../../etc/passwd.nupkg", "a/b.nupkg", "x.zip", "x.nupkg.exe"):
        r = client.post("/api/v1/updates/releases",
                        json=_payload(package_filename=bad_fn), headers=ADMIN)
        check(r.status_code in (400, 422), f"拒绝 package_filename {bad_fn!r}",
              str(r.status_code))
    r = client.post("/api/v1/updates/releases", json=_payload(package_sha1="abc"),
                    headers=ADMIN)
    check(r.status_code in (400, 422), "sha1 位数不对 → 拒绝", str(r.status_code))

    print("\n[5] 分享中不存在 package_filename → 发布失败，不写库")
    r = client.post("/api/v1/updates/releases",
                    json=_payload(package_filename="LiveLensDesktop-9.9.9-stable-full.nupkg"),
                    headers=ADMIN)
    check(r.status_code == 400, "文件不存在 → 400", f"{r.status_code} {r.text[:120]}")
    check(_row_count() == 0, "失败的发布没有留下记录", str(_row_count()))

    print("\n[5b] 申报大小与分享实际大小不一致 → 拒绝")
    r = client.post("/api/v1/updates/releases", json=_payload(package_size=123),
                    headers=ADMIN)
    check(r.status_code == 400, "大小不符 → 400", str(r.status_code))
    check(_row_count() == 0, "大小不符未写库", str(_row_count()))

    print("\n[1][3] 创建 stable release（有效 key）")
    r = client.post("/api/v1/updates/releases", json=_payload(), headers=ADMIN)
    check(r.status_code == 200, "发布成功", f"{r.status_code} {r.text[:160]}")
    body = r.json() if r.status_code == 200 else {}
    check(body.get("version") == VERSION, "返回 version", str(body.get("version")))
    check(body.get("package_size") == SIZE, "返回 package_size", str(body.get("package_size")))
    check(body.get("package_share_url") == SHARE_URL, "返回永久 share_url")
    check("direct_url" not in json.dumps(body).lower(), "响应里没有 direct_url")
    check(_row_count() == 1, "库里有且仅有 1 条 release", str(_row_count()))

    print("\n[1b] 同渠道同版本不允许重复")
    r = client.post("/api/v1/updates/releases", json=_payload(), headers=ADMIN)
    check(r.status_code == 409, "重复发布 → 409", str(r.status_code))
    check(_row_count() == 1, "重复发布没有新增记录", str(_row_count()))

    print("\n[6][13] 库里只有永久 share_url，没有 direct_url")
    row = _row()
    check(row is not None, "能查到 release")
    if row is not None:
        check(row.package_share_url == SHARE_URL, "存的是永久分享链接")
        cols = {c.name for c in models.UpdateRelease.__table__.columns}
        check("package_share_url" in cols, "有 package_share_url 列")
        leaked = [c for c in cols if "direct" in c or "tmp" in c or "token" in c]
        check(not leaked, "没有 direct_url / token 之类的列", str(leaked))
        check(not any("installer" in c for c in cols),
              "没有 installer_url / installer_filename 列", str(sorted(cols)))
    # 直接扫数据库文件本身
    check(SENTINEL_DIRECT.encode() not in _DB_PATH.read_bytes(),
          "数据库文件里不含临时直链")

    print("\n[7][8] feed 反映新版本 + notes 进入客户端读取的位置")
    r = client.get("/updates/stable/releases.stable.json")
    check(r.status_code == 200, "releases.stable.json → 200", str(r.status_code))
    manifest = r.json()
    asset = (manifest.get("Assets") or [{}])[0]
    check(asset.get("Version") == VERSION, "Version 正确", str(asset.get("Version")))
    check(asset.get("PackageId") == "LiveLensDesktop", "PackageId 正确", str(asset.get("PackageId")))
    check(asset.get("Type") == "Full", "Type=Full", str(asset.get("Type")))
    check(asset.get("FileName") == FILENAME,
          "FileName 与 package_filename 完全一致", str(asset.get("FileName")))
    check(asset.get("SHA1") == SHA1.upper(), "SHA1 大写透传", str(asset.get("SHA1")))
    check(asset.get("SHA256") == SHA256.upper(), "SHA256 大写透传", str(asset.get("SHA256")))
    check(asset.get("Size") == SIZE, "Size 正确", str(asset.get("Size")))
    # update_dialog.py 读的就是 NotesMarkdown
    check(asset.get("NotesMarkdown") == NOTES,
          "release_notes 进入 Assets[].NotesMarkdown", str(asset.get("NotesMarkdown"))[:60])

    r = client.get("/updates/stable/assets.stable.json")
    check(r.status_code == 200, "assets.stable.json → 200", str(r.status_code))
    assets = r.json()
    check(assets[0]["Type"] == "Installer" and assets[1]["Type"] == "Full",
          "资产索引含 Installer + Full", json.dumps(assets, ensure_ascii=False)[:120])
    check(assets[1]["RelativeFileName"] == FILENAME, "Full 资产指向 nupkg")

    r = client.get("/updates/stable/RELEASES-stable")
    check(r.status_code == 200, "RELEASES-stable → 200", str(r.status_code))
    raw = r.content
    check(raw.startswith(b"\xef\xbb\xbf"), "带 UTF-8 BOM")
    check(raw.decode("utf-8-sig") == f"{SHA1.upper()} {FILENAME} {SIZE}",
          "旧版格式内容正确", raw.decode("utf-8-sig")[:80])

    print("\n[9] package URL 找不到 → 404")
    r = client.get("/updates/stable/LiveLensDesktop-9.9.9-stable-full.nupkg")
    check(r.status_code == 404, "未发布的包 → 404", str(r.status_code))
    r = client.get("/updates/stable/whatever.txt")
    check(r.status_code == 404, "未知文件 → 404", str(r.status_code))
    r = client.get("/updates/beta/releases.beta.json")
    check(r.status_code == 404, "未知渠道 → 404", str(r.status_code))

    print("\n[10][11][12][16] package URL 命中 → 307 + 临时直链，不代理内容")
    log_records = _capture_logs()
    r = client.get(f"/updates/stable/{FILENAME}")
    check(r.status_code == 307, "命中 → 307", str(r.status_code))
    check(r.headers.get("Location") == SENTINEL_DIRECT,
          "Location 是 resolver 返回的临时直链", str(r.headers.get("Location"))[:50])
    check(r.headers.get("Cache-Control") == "no-store",
          "临时直链不缓存", str(r.headers.get("Cache-Control")))
    check(not r.content, "响应体为空（没有代理 .nupkg 内容）", str(len(r.content)))
    check(r.headers.get("content-length") in (None, "0"),
          "没有 Content-Length（未读取包体）", str(r.headers.get("content-length")))

    print("\n[14] direct_url 不进入日志")
    text = "\n".join(log_records[0])
    check(text != "", "确实产生了日志（否则该断言无意义）")
    check(SENTINEL_DIRECT not in text, "日志不含完整临时直链")
    check("SENTINEL-DIRECT-TOKEN" not in text, "日志不含直链 token")
    check("cdn.invalid.example" not in text, "日志不含直链 host")

    print("\n[15] resolver 失败 → 502，且不泄漏")
    log_records[0].clear()
    r = client.get("/updates/stable/" + FILENAME)
    check(r.status_code == 307, "（正常路径仍可用）", str(r.status_code))

    # 让 resolver 对「已发布的那个文件」报错
    async def _failing(share_url, share_password="", filename=None):
        raise fr.FeijipanResolveError("访问小飞机接口超时", code=500)

    fr.resolve = _failing
    try:
        r = client.get(f"/updates/stable/{FILENAME}")
        check(r.status_code == 502, "resolver 失败 → 502", str(r.status_code))
        check(SENTINEL_DIRECT not in r.text, "502 响应体不含临时直链", r.text[:80])
        check(SENTINEL_DIRECT not in "\n".join(log_records[0]),
              "失败日志不含临时直链")
    finally:
        fr.resolve = _fake_resolve

    print("\n[16b] 服务端不下载/读取 .nupkg")
    # 唯一一次「读包」的路径是 resolver；本测试中 resolver 是假的。
    # 若服务端偷偷代理内容，响应就会是 200 + 巨大 body。这里再次确认为 307 空体。
    r = client.get(f"/updates/stable/{FILENAME}")
    check(r.status_code == 307 and not r.content,
          "仍是 307 且无 body（未读取整包）", f"{r.status_code}/{len(r.content)}")

    print("\n[17] share URL 尾部斜杠容忍（v2.1.0 排障修复回归）")
    # 单元层：validate_share_url 本体——尾斜杠放行、多段路径仍拒绝
    try:
        key = fr.validate_share_url(SHARE_URL + "/")
        check(bool(key), "尾斜杠链接通过校验并提取到 key")
    except fr.FeijipanResolveError as e:
        check(False, "尾斜杠链接通过校验", str(e))
    for bad in (SHARE_URL + "/extra/", SHARE_URL.replace("/s/", "/s/a/")):
        try:
            fr.validate_share_url(bad)
            check(False, "多段路径被拒绝: " + bad)
        except fr.FeijipanResolveError:
            check(True, "多段路径被拒绝")
    # API 层：带尾斜杠的完整发布流程（release_service → 真实 validate_share_url）
    r = client.post(
        "/api/v1/updates/releases",
        json=_payload(version="9.8.7",
                      package_filename="LiveLensDesktop-9.8.7-stable-delta.nupkg",
                      package_share_url=SHARE_URL + "/"),
        headers=ADMIN,
    )
    check(r.status_code == 200, "尾斜杠 URL 发布 → 200", str(r.status_code))
    if r.status_code == 200:
        check(_row_count() == 2, "尾斜杠发布的记录已入库", str(_row_count()))

    # 发布失败时服务端必须留下含原因的 error 日志（v2.1.0 排障修复回归）
    log_records[0].clear()
    r = client.post("/api/v1/updates/releases",
                    json=_payload(version="9.8.7",
                                  package_filename="LiveLensDesktop-9.8.7-stable-delta.nupkg"),
                    headers=ADMIN)
    check(r.status_code == 409, "同版本重复发布 → 409", str(r.status_code))
    check(any("release publish failed" in t for t in log_records[0]),
          "发布失败日志含 'release publish failed'")
    check(any("409" in t or "已存在版本" in t for t in log_records[0]),
          "日志携带失败原因")

    print("\n[18] 飞盘 fileSize=KB 取整的交叉校验（v2.1.0 排障修复回归）")
    # 实测：飞盘 fileSize 是字节数 // 1024。申报真实字节、分享报告 KB，
    # 必须判为一致且落库**真实字节**（feed Size 需要真实字节）。
    KB_REPORTED = 14752
    REAL_BYTES = KB_REPORTED * 1024 + 369          # 15,106,417 的真实案例形态
    kb_name = "LiveLensDesktop-9.8.6-stable-delta.nupkg"
    FAKE_FILES[kb_name] = {"size": KB_REPORTED}

    async def _kb_resolve(share_url, share_password="", filename=None):
        if filename == kb_name:
            return {"code": 200, "url": SENTINEL_DIRECT, "message": "解析成功",
                    "file_name": kb_name, "file_size": KB_REPORTED,
                    "selection": "filename_exact"}
        return await _fake_resolve(share_url, share_password, filename)

    fr.resolve = _kb_resolve
    try:
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.8.6",
                                      package_filename=kb_name,
                                      package_size=REAL_BYTES),
                        headers=ADMIN)
        check(r.status_code == 200, "KB 取整口径 → 200（不再误判不一致）",
              str(r.status_code) + r.text[:80])
        from sqlalchemy.orm import Session
        import database
        with Session(database.engine) as s:
            row = (s.query(models.UpdateRelease)
                   .filter(models.UpdateRelease.version == "9.8.6").first())
        check(row.package_size == REAL_BYTES,
              "落库的是申报的真实字节", str(row and row.package_size))
        # 字节级声明也仍然精确匹配
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.8.5",
                                      package_filename=kb_name,
                                      package_size=KB_REPORTED),
                        headers=ADMIN)
        check(r.status_code == 200, "精确字节口径 → 200", str(r.status_code))
        # 真不一致（既非字节也非 KB 口径）仍然拒绝
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.8.4",
                                      package_filename=kb_name,
                                      package_size=KB_REPORTED + 500),
                        headers=ADMIN)
        check(r.status_code == 400, "大小真正不一致 → 400", str(r.status_code))
    finally:
        fr.resolve = _fake_resolve

    print("\n[19] 对象存储（OSS）后端：发布 HEAD 复核 + 下载 307（v2.1.0）")
    oss_name = "LiveLensDesktop-9.9.0-stable-delta.nupkg"
    oss_url = "https://livlensupdate.oss-cn-beijing.aliyuncs.com/stable/" + oss_name
    OSS_HEADERS = {"Content-Length": str(SIZE)}

    # 模拟 HEAD：可控的响应/异常；sizes 按 URL 精确返回字节数
    class _FakeHeadResp:
        def __init__(self, status, headers):
            self.status = status
            self.headers = headers

    head_state = {"status": 200, "raise": None, "calls": 0, "sizes": {},
                  "last_url": None}

    class _FakeHeadCtx:
        def __init__(self, url, **kw):
            self.url = url
            head_state["calls"] += 1
            head_state["last_url"] = url
            if head_state["raise"]:
                raise head_state["raise"]

        async def __aenter__(self):
            size = head_state["sizes"].get(self.url, SIZE)
            return _FakeHeadResp(head_state["status"],
                                 {"Content-Length": str(size)})

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def head(self, url, **kw):
            return _FakeHeadCtx(url, **kw)

    import aiohttp
    real_client_session = aiohttp.ClientSession
    aiohttp.ClientSession = _FakeSession
    try:
        # 19a) 正常：HEAD 200 + 字节一致 → 200 入库；旧飞盘记录数 +1 不受影响
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.9.0", package_filename=oss_name,
                                      package_share_url=oss_url),
                        headers=ADMIN)
        check(r.status_code == 200, "OSS 发布 → 200", str(r.status_code) + r.text[:80])
        check(head_state["calls"] >= 1, "发布前做了 HEAD 复核")
        check(head_state["last_url"] == oss_url, "HEAD 的就是申报的 OSS 直链")
        from sqlalchemy.orm import Session
        import database
        with Session(database.engine) as s:
            row = (s.query(models.UpdateRelease)
                   .filter(models.UpdateRelease.version == "9.9.0").first())
        check(row is not None and row.package_share_url == oss_url,
              "OSS 直链作为永久 URL 入库")

        # 19b) 下载 → 307 到 OSS 直链
        r = client.get(f"/updates/stable/{oss_name}")
        check(r.status_code == 307, "OSS 包下载 → 307", str(r.status_code))
        check(r.headers.get("Location") == oss_url, "307 到 OSS 直链")
        check(r.headers.get("Cache-Control") == "public, max-age=300",
              "OSS 直链允许短缓存", str(r.headers.get("Cache-Control")))

        # 19c) HEAD 404 → 400，不入库
        head_state["status"] = 404
        before = _row_count()
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.9.1", package_filename=oss_name,
                                      package_share_url=oss_url),
                        headers=ADMIN)
        check(r.status_code == 400, "OSS 对象不存在 → 400", str(r.status_code))
        check(_row_count() == before, "失败发布不入库")

        # 19d) HEAD 网络异常 → 502
        head_state["status"] = 200
        head_state["raise"] = OSError("net down")
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.9.1", package_filename=oss_name,
                                      package_share_url=oss_url),
                        headers=ADMIN)
        check(r.status_code == 502, "OSS 不可达 → 502", str(r.status_code))

        # 19e) 大小不符 → 400
        head_state["raise"] = None
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.9.1", package_filename=oss_name,
                                      package_share_url=oss_url,
                                      package_size=SIZE + 1),
                        headers=ADMIN)
        check(r.status_code == 400, "OSS 大小不符 → 400", str(r.status_code))

        # 19f) host 白名单：非 aliyuncs 的对象 URL 被当作飞盘路径处理（→ 走
        # resolver → 404 400），且不会对它发 HEAD
        head_state["calls"] = 0
        r = client.post("/api/v1/updates/releases",
                        json=_payload(version="9.9.2", package_filename=oss_name,
                                      package_share_url="https://evil.example.com/x.nupkg"),
                        headers=ADMIN)
        check(r.status_code == 400, "非白名单 host → 走飞盘路径拒绝", str(r.status_code))
        check(head_state["calls"] == 0, "非白名单 host 不发 HEAD")
    finally:
        aiohttp.ClientSession = real_client_session

    print("\n[20] Full + Delta 双条目（v2.1.0 增量分发回归）")
    full_name = "LiveLensDesktop-9.9.3-stable-full.nupkg"
    dlt_name = "LiveLensDesktop-9.9.3-stable-delta.nupkg"
    full_url = ("https://livlensupdate.oss-cn-beijing.aliyuncs.com/stable/"
                + full_name)
    dlt_url = ("https://livlensupdate.oss-cn-beijing.aliyuncs.com/stable/"
               + dlt_name)
    head_state["sizes"] = {full_url: 2985, dlt_url: 1500}
    head_state["status"] = 200
    head_state["raise"] = None
    import aiohttp
    real_session_20 = aiohttp.ClientSession
    aiohttp.ClientSession = _FakeSession
    r = client.post(
        "/api/v1/updates/releases",
        json=_payload(version="9.9.3", package_filename=full_name,
                      package_share_url=full_url, package_size=2985,
                      delta_filename=dlt_name, delta_share_url=dlt_url,
                      delta_sha1=SHA1, delta_sha256=SHA256, delta_size=1500),
        headers=ADMIN)
    check(r.status_code == 200, "Full+Delta 同一次发布 → 200",
          str(r.status_code) + r.text[:100])

    r = client.get("/updates/stable/releases.stable.json")
    assets = r.json()["Assets"]
    kinds = {(a["Version"], a["Type"]) for a in assets}
    check(("9.9.3", "Full") in kinds and ("9.9.3", "Delta") in kinds,
          "清单同时登记 Full 与 Delta", str(kinds))
    d_asset = next(a for a in assets
                   if a["Version"] == "9.9.3" and a["Type"] == "Delta")
    check(d_asset["FileName"] == dlt_name and d_asset["Size"] == 1500,
          "Delta 条目文件名/大小正确", str(d_asset)[:120])

    r = client.get(f"/updates/stable/{dlt_name}")
    check(r.status_code == 307 and r.headers.get("Location") == dlt_url,
          "delta 包下载 → 307 到 delta 直链", str(r.status_code))
    r = client.get(f"/updates/stable/{full_name}")
    check(r.status_code == 307 and r.headers.get("Location") == full_url,
          "full 包下载 → 307 到 full 直链", str(r.status_code))

    # delta 字段不完整 → 400
    r = client.post(
        "/api/v1/updates/releases",
        json=_payload(version="9.9.2", package_filename=oss_name,
                      package_share_url=oss_url,
                      delta_filename=dlt_name, delta_share_url=dlt_url),
        headers=ADMIN)
    check(r.status_code == 400, "delta 字段不完整 → 400", str(r.status_code))
    aiohttp.ClientSession = real_session_20   # 恢复真实会话（[12b] 不发 HEAD）

    print("\n[12b] 兼容性：feed 字段集合与既有 vpk 产出对齐")
    keys = set(asset.keys())
    check(keys <= {"PackageId", "Version", "Type", "FileName", "SHA1", "SHA256",
                   "Size", "NotesMarkdown", "NotesHTML"},
          "清单字段不超出已知 schema", str(sorted(keys)))
    check({"PackageId", "Version", "Type", "FileName", "SHA1", "SHA256", "Size"} <= keys,
          "客户端必需字段齐全", str(sorted(keys)))

    fr.resolve = _REAL_RESOLVE
    shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for name in _FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


# ----------------------------------------------------------------------
def _row():
    from sqlalchemy.orm import Session
    import database
    with Session(database.engine) as s:
        return s.query(models.UpdateRelease).first()


def _row_count() -> int:
    from sqlalchemy.orm import Session
    import database
    with Session(database.engine) as s:
        return s.query(models.UpdateRelease).count()


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _capture_logs():
    collector = _Collector()
    root = logging.getLogger()
    root.addHandler(collector)
    root.setLevel(logging.INFO)
    return (collector.records, root, collector)


if __name__ == "__main__":
    sys.exit(main())
