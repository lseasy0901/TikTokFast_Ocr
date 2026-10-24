#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/publish_update.bat 的回归测试。

策略
----
* 起一个**本地 HTTP 端点**冒充 /api/v1/updates/releases，记录收到的请求
  （路径 / 头 / 原始 body）。因此「JSON 到底怎么构造的、哈希对不对」都是
  断言出来的事实，不是假设。
* 不联网、不碰生产。**绝不真的发布正式版本。**
* 用 ``DLV_LICENSE_SERVER_URL``（项目既有的服务器覆盖变量）把 BAT 指向本地端点，
  不为此新造第二个配置项。
* BAT 的每个提示都能用命令行选项替代（这是它自带的接口），因此测试走非交互路径。
  只有在**必须**验证交互行为（取消）时才用空 stdin。

备注（实测，不是推测）：
  cmd 的 ``set /p`` 在 stdin 被重定向时会把**整段输入**读进第一个变量
  （``AAA\\nBBB\\nCCC`` 全进了 A），因此多提示流程无法用管道驱动 —— 这也是
  BAT 必须有命令行选项的原因。
"""

import hashlib
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parent
BAT = ROOT / "scripts" / "publish_update.bat"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        pass

_PASS = []
_FAIL = []
RECEIVED = []
SENTINEL_BODY = {"id": 1, "version": "1.1.1", "channel": "stable"}
SHARE = "https://share.feijipan.com/s/gg95ZrAn"


def check(condition, message, detail=""):
    (_PASS if condition else _FAIL).append(message)
    suffix = f"  -- {detail}" if (detail and not condition) else ""
    print(f"{'PASS' if condition else 'FAIL'}  {message}{suffix}")


# ----------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        RECEIVED.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "raw": raw,
        })
        body = json.dumps(SENTINEL_BODY).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                        # noqa: A003
        pass


def _run(args, env_extra=None, stdin=b""):
    env = os.environ.copy()
    env.pop("LIVELENS_ADMIN_KEY", None)      # 由用例自己决定是否提供
    env.update(env_extra or {})
    proc = subprocess.run(
        ["cmd.exe", "/c", str(BAT), *args],
        input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, cwd=str(ROOT), timeout=180,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _args(pkg, version="1.1.1", share=SHARE, notes=None, yes=True, extra=()):
    out = ["--version", version, "--nupkg", str(pkg), "--share", share]
    if notes is not None:
        out += ["--notes", notes]
    if yes:
        out.append("--yes")
    return out + list(extra)


# ----------------------------------------------------------------------
def main() -> int:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
    print(f"local endpoint: {base}\n")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dlv-pubtest-"))
    good = tmp / "LiveLensDesktop-1.1.1-stable-full.nupkg"
    good.write_bytes(b"LiveLens fake package payload\n" * 100)
    data = good.read_bytes()
    expected_sha1 = hashlib.sha1(data).hexdigest().upper()
    expected_sha256 = hashlib.sha256(data).hexdigest().upper()
    expected_size = len(data)
    API_ENV = {"DLV_LICENSE_SERVER_URL": base, "LIVELENS_ADMIN_KEY": "test-admin-key"}

    try:
        print("[1] file does not exist")
        code, out = _run(_args(tmp / "nope.nupkg"), API_ENV)
        check(code == 1, "exit code 1", str(code))
        check("[FAIL]" in out, "clear failure marker", out[-200:])
        check("nope.nupkg" in out, "message names the file", out[-200:])
        check(not RECEIVED, "no request was sent", str(len(RECEIVED)))

        print("\n[2] not a .nupkg")
        bad = tmp / "LiveLensDesktop-1.1.1-stable-full.zip"
        bad.write_bytes(b"x")
        code, out = _run(_args(bad), API_ENV)
        check(code == 1, "exit code 1", str(code))
        check(".nupkg" in out, "message mentions the extension", out[-200:])
        check(not RECEIVED, "no request was sent", str(len(RECEIVED)))

        print("\n[3] version / filename mismatch")
        code, out = _run(_args(good, version="1.2.0"), API_ENV)
        check(code == 1, "exit code 1", str(code))
        check("mismatch" in out, "message says mismatch", out[-220:])
        check("LiveLensDesktop-1.2.0-stable-full.nupkg" in out,
              "message shows the expected name", out[-220:])
        code, out = _run(_args(good, version="1.1"), API_ENV)
        check(code == 1, "malformed version also rejected", str(code))
        code, out = _run(_args(good, version="1.1.1.1"), API_ENV)
        check(code == 1, "four-part version also rejected", str(code))
        check(not RECEIVED, "no request was sent", str(len(RECEIVED)))

        print("\n[4] illegal share URL")
        for bad_share in ("https://evil.com/s/abc",
                          "http://share.feijipan.com/s/abc",
                          "https://share.feijipan.com/u/abc",
                          "https://share.feijipan.com.evil.com/s/abc",
                          "ftp://share.feijipan.com/s/abc",
                          "https://share.feijipan.com/s/"):
            code, out = _run(_args(good, share=bad_share), API_ENV)
            check(code == 2, f"rejected {bad_share[:44]} -> exit 2", str(code))
        check(not RECEIVED, "no request was sent", str(len(RECEIVED)))

        print("\n[5] missing admin key")
        code, out = _run(_args(good, extra=["--no-key-prompt"]),
                         {"DLV_LICENSE_SERVER_URL": base})
        check(code == 3, "exit code 3", str(code))
        check("LIVELENS_ADMIN_KEY" in out, "points at that env var", out[-260:])
        check(not RECEIVED, "no request was sent", str(len(RECEIVED)))

        print("\n[6] user cancels (no --yes, empty stdin)")
        code, out = _run([a for a in _args(good) if a != "--yes"], API_ENV)
        check(code == 4, "exit code 4", str(code))
        check("cancel" in out.lower(), "says it was cancelled", out[-220:])
        check(not RECEIVED, "nothing was published", str(len(RECEIVED)))

        print("\n[7][8][9][10] happy path")
        notes = "LiveLens 1.1.1 release notes"
        code, out = _run(_args(good, notes=notes), API_ENV)
        check(code == 0, "exit code 0", f"{code} :: {out[-400:]}")
        check("release published" in out, "prints the success marker", out[-200:])
        for marker in ("package exists", "filename verified", "SHA1 calculated",
                       "SHA256 calculated", "size calculated", "share URL validated"):
            check(marker in out, f"[OK] {marker}", out[-300:])
        check(len(RECEIVED) == 1, "exactly one request", str(len(RECEIVED)))

        if RECEIVED:
            req = RECEIVED[0]
            check(req["path"] == "/api/v1/updates/releases",
                  "posted to /api/v1/updates/releases", req["path"])
            check(req["headers"].get("x-api-key") == "test-admin-key",
                  "API key travels in X-API-Key", str(sorted(req["headers"]))[:80])
            check(b"test-admin-key" not in req["raw"].lower(),
                  "API key is NOT in the request body")
            body = json.loads(req["raw"].decode("utf-8"))
            check(body.get("version") == "1.1.1", "version", str(body.get("version")))
            check(body.get("channel") == "stable", "channel", str(body.get("channel")))
            check(body.get("package_filename") == good.name,
                  "package_filename auto-derived", str(body.get("package_filename")))
            check(body.get("package_share_url") == SHARE, "share_url")
            check(body.get("package_sha1") == expected_sha1,
                  "SHA1 matches the local file", str(body.get("package_sha1")))
            check(body.get("package_sha256") == expected_sha256,
                  "SHA256 matches the local file", str(body.get("package_sha256")))
            check(body.get("package_size") == expected_size,
                  "size matches the local file", str(body.get("package_size")))
            check(body.get("release_notes") == notes, "release_notes")
            check(set(body) == {"version", "channel", "package_filename",
                                "package_share_url", "release_notes",
                                "package_sha1", "package_sha256", "package_size"},
                  "JSON field set matches the API contract", str(sorted(body)))

        print("\n[11] invalid option")
        code, out = _run(["--bogus", "x"], API_ENV)
        check(code == 9, "exit code 9", str(code))

        print("\n[12] Chinese notes survive via --notes")
        RECEIVED.clear()
        zh = "修复直播延迟 中文说明"
        code, out = _run(_args(good, notes=zh), API_ENV)
        check(code == 0, "exit code 0", f"{code} :: {out[-300:]}")
        if RECEIVED:
            got = json.loads(RECEIVED[0]["raw"].decode("utf-8")).get("release_notes")
            check(got == zh, "Chinese notes round-trip intact", f"got {got!r}")

        print("\n[12b] multi-line / quotes / percent notes via --notes-file")
        RECEIVED.clear()
        # 末尾故意多一个换行：BAT 会 TrimEnd（否则每份说明都会多一个空行结尾）。
        tricky = '修复直播延迟 & "房间码" 识别\n第二行：中文 100% 生效\n第三行 end'
        nf = tmp / "notes.md"
        # newline="" 不做换行转换：文件里就是 LF，BAT 必须原样保留。
        nf.write_text(tricky + "\n", encoding="utf-8", newline="")
        code, out = _run(_args(good, extra=["--notes-file", str(nf)]), API_ENV)
        check(code == 0, "exit code 0", f"{code} :: {out[-300:]}")
        if RECEIVED:
            got = json.loads(RECEIVED[0]["raw"].decode("utf-8")).get("release_notes")
            check(got == tricky,
                  "multi-line + quotes + percent round-trip intact", f"got {got!r}")

        print("\n[13] temporary files cleaned up")
        temps = list(pathlib.Path(os.environ.get("TEMP", ".")).glob("livelens_pub_*"))
        check(not temps, "no livelens_pub_* left behind", str([t.name for t in temps]))

        print("\n[14] nothing was pointed at production")
        check(all(r["path"].startswith("/api/v1/updates/releases") for r in RECEIVED),
              "every request used the local endpoint path",
              str([r["path"] for r in RECEIVED]))

    finally:
        httpd.shutdown()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for name in _FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
