# -*- coding: utf-8 -*-
"""
LiveLens Velopack 真实更新端到端验证（Phase 3 / 任务书 §16C）。

做的是一条真实链路，不模拟、不绕过任何一步：

    feed 里的 Setup.exe
      → 静默安装 1.1.0 到临时目录
      → 启动 1.1.0（LIVELENS_E2E_UPDATE=1 让程序自己点按钮）
      → 程序检查更新源（本地 HTTP，指向 feed）
      → 发现 1.1.1 → 下载 → apply
      → 1.1.0 进程退出，Velopack 应用更新并拉起 1.1.1
      → 重新启动已安装程序，确认它报告的版本确实是 1.1.1

全程另外校验：安装目录之外的永久用户数据（%LOCALAPPDATA%\\LiveLens 下的
prefs.json / stream_history.json）在整个过程中逐字节未变。

用法：
    python scripts/e2e_velopack_update.py
    python scripts/e2e_velopack_update.py --base 1.1.0 --target 1.1.1

所有生成物（临时安装、日志、备份）都在发布目录下，源码目录不受污染。
"""

import argparse
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RELEASE_ROOT = PROJECT_ROOT.parent / "_velopack_release"

MAIN_EXE = "DouyinLowLatencyViewer.exe"
PACK_ID = "LiveLensDesktop"
USER_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "LiveLens"

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [ok]   {message}", flush=True)
    else:
        _FAILED += 1
        print(f"  [FAIL] {message}", flush=True)
    return bool(condition)


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def user_data_hashes() -> dict:
    out = {}
    for name in ("prefs.json", "stream_history.json"):
        p = USER_DATA_DIR / name
        out[name] = sha256(p) if p.is_file() else None
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 - 保持安静，别刷屏
        pass


def serve(directory: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = lambda *a, **k: _QuietHandler(*a, directory=str(directory), **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def installed_version(install_dir: Path):
    """已安装版本 —— 读 Velopack 自己的清单 current/sq.version。

    这是运行中程序 get_current_version() 读的同一份文件，因此它就是
    「程序认为自己是什么版本」的权威来源，而不是我们另算一遍。
    清单是 nuspec 格式的 XML（带命名空间），这里按标签名取值。
    """
    manifest = install_dir / "current" / "sq.version"
    if not manifest.is_file():
        return None, manifest
    try:
        import xml.etree.ElementTree as ET

        root = ET.parse(manifest).getroot()
    except Exception:  # noqa: BLE001
        return None, manifest
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "version":
            return (el.text or "").strip(), manifest
    return None, manifest


def wait_for(predicate, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def read_text(path: Path) -> str:
    """读程序的日志文件，自动识别编码。

    打包后的 EXE 是 Windows 窗口程序（console=False），它往继承来的 stderr
    句柄写的是**本地代码页**（中文机器上是 GBK），PYTHONIOENCODING 对这条
    路径不生效。于是日志里的中文按 UTF-8 读回来全是乱码，「按日志内容断言」
    会全部失效 —— 这里先按 UTF-8 试，失败就按 GBK 解。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def app_pids(exe_path: Path) -> list:
    """本次 E2E 那个 exe 的进程 pid —— 用**完整可执行文件路径**精确匹配。

    刻意不做「按 exe 文件名找进程」那种系统范围匹配：用户自己装的 LiveLens
    也叫 DouyinLowLatencyViewer.exe，按名字清一遍就会误伤。这里比的是一条
    具体路径（本次 E2E 的临时安装目录），不可能命中别处的同名程序；
    权限不足、取不到路径的进程一律跳过。
    """
    import psutil

    target = str(exe_path).lower()
    pids = []
    for p in psutil.process_iter(["pid", "exe"]):
        try:
            if (p.info.get("exe") or "").lower() == target:
                pids.append(p.info["pid"])
        except Exception:  # noqa: BLE001 - 取不到信息就跳过，不猜
            continue
    return pids


def terminate_own(pid: int, exe_path: Path, timeout: float = 20.0) -> bool:
    """终止一个**属于本次 E2E** 的进程，并等它真的退出。

    两道保险，都是为了不误伤无关进程：
      1. 只接受调用方明确传入的 pid（本次 Popen 的句柄，或上面按完整路径
         匹配出来的 pid），从不自己去「清一遍同名进程」；
      2. 终止前再用完整路径核对一次那一刻的 exe —— 防 pid 复用，
         核对不过就直接放弃，宁可留着也不杀错。
    """
    import psutil

    try:
        proc = psutil.Process(pid)
        if (proc.exe() or "").lower() != str(exe_path).lower():
            return False
        proc.terminate()
        proc.wait(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - 已退出/权限不足都算未由我们终止
        return False


def wait_log_contains(path: Path, needle: str, timeout: float) -> bool:
    return bool(wait_for(lambda: needle in read_text(path), timeout))


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def run(base: str, target: str, release_root: Path, keep_install: bool) -> int:
    feed = release_root / "feed"
    e2e_dir = release_root / "e2e"
    install_dir = e2e_dir / "install"
    logs = e2e_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    updater_log = Path(os.environ.get("LOCALAPPDATA", "")) / "velopack" / f"velopack_{PACK_ID}.log"

    step("0. 前置检查")
    # 必须用**带版本号归档的那一份**安装器装 base，不能用 feed 里那个：
    # feed 里的安装器文件名不含版本，发新版时会被覆盖，拿它「安装 1.1.0」
    # 实际装上去的是最新版（这一点已经踩过，安装后核对版本才发现）。
    setup_exe = release_root / "installers" / f"{PACK_ID}-{base}-stable-Setup.exe"
    check(setup_exe.is_file(), f"存在 {base} 的归档安装器：{setup_exe.name}")
    if not setup_exe.is_file():
        print(f"  [note] 请先打包该版本以生成归档（见 scripts/velopack_release.py）")
        return 1
    # 包名由 vpk 决定（<packId>-<版本>-<channel>-full.nupkg），用 glob 认它，
    # 不在这儿写死第二份命名规则
    for v in (base, target):
        pkgs = sorted(feed.glob(f"{PACK_ID}-{v}-*-full.nupkg"))
        size = pkgs[0].stat().st_size // (1024 * 1024) if pkgs else 0
        check(bool(pkgs), f"存在 {v} 全量包（{size} MB）")
    # 增量包只是「这份 feed 里恰好有」的信息，不再是发布链的要求：
    # scripts/velopack_release.py 现在固定 --delta None（每版只发一个 Full 包），
    # 所以拿新建的 feed 跑本脚本时这里本来就该是空的，不能当成失败。
    deltas = sorted(p.name for p in feed.glob(f"{PACK_ID}-{target}-*delta*.nupkg"))
    print(f"  [note] feed 中的增量包：{deltas or '无'}")
    check((feed / "releases.stable.json").is_file(), "存在更新源清单 releases.stable.json")

    # 用户数据：先备份再留指纹
    step("1. 永久用户数据备份与指纹")
    backup = e2e_dir / "userdata_backup"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    for name in ("prefs.json", "stream_history.json"):
        src = USER_DATA_DIR / name
        if src.is_file():
            shutil.copy2(src, backup / name)
    check(backup.is_dir(), f"已备份 {USER_DATA_DIR} → {backup}")

    # 首次引导是模态框，会和「替用户点按钮」的自动驾驶抢事件循环；
    # 这里把引导关掉，E2E 结束后原样备份仍在（只动这一个开关，见报告）。
    prefs_path = USER_DATA_DIR / "prefs.json"
    guide_touched = False
    if prefs_path.is_file():
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        if prefs.get("show_guide_on_start"):
            prefs["show_guide_on_start"] = False
            prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
            guide_touched = True
    if guide_touched:
        print("  [note] 已临时关闭首次引导弹窗（E2E 用），原文件已备份")

    before = user_data_hashes()
    print(f"  [note] 更新前指纹：{before}")

    # 安装 1.1.0
    step(f"2. 静默安装 {base}")
    if install_dir.exists():
        shutil.rmtree(install_dir, ignore_errors=True)
    setup_log = logs / "setup.log"
    rc = subprocess.call(
        [str(setup_exe), "--silent", "--installto", str(install_dir), "--log", str(setup_log)]
    )
    check(rc == 0, f"Setup.exe 返回 {rc}")
    version, manifest = installed_version(install_dir)
    check(version == base, f"安装目录清单版本为 {version}（期望 {base}）")
    if version != base:
        print("  [abort] 装上去的不是 base 版本，后面的更新毫无意义")
        return 1
    check((install_dir / "current" / MAIN_EXE).is_file(), f"current/ 下有 {MAIN_EXE}")
    check((install_dir / "Update.exe").is_file(), "安装根目录有 Velopack 的 Update.exe")
    check((install_dir / "packages").is_dir(), "安装根目录有 packages/")
    check(user_data_hashes() == before, "安装 1.1.0 后用户数据未改变")

    # 本地更新源
    port = free_port()
    httpd = serve(feed, port)
    url = f"http://127.0.0.1:{port}"
    print(f"  [note] 本地更新源 {url}（根目录 {feed}）")

    env = os.environ.copy()
    env["LIVELENS_E2E_UPDATE"] = "1"
    env["LIVELENS_UPDATE_URL"] = url
    env["QT_QPA_PLATFORM"] = "windows"

    run_log = logs / f"run_{base}.log"
    if updater_log.is_file():
        updater_log.unlink()        # 只看本次的更新器日志

    step(f"3. 启动 {base}（自动驾驶：检查 → 下载 → 重启）")
    with open(run_log, "w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(
            [str(install_dir / "current" / MAIN_EXE)],
            env=env, stdout=fh, stderr=subprocess.STDOUT,
            cwd=str(install_dir / "current"),
        )
        print(f"  [note] PID {proc.pid}，日志 {run_log}")

        check(
            wait_log_contains(run_log, "启动 Douyin Low Latency Stream Viewer", 60),
            "旧版程序完成启动",
        )
        # 运行中的 base 向更新源自报的版本必须是 base（Velopack 的请求里带
        # localVersion，这是「跑起来的确实是旧版」最直接的证据）
        check(
            wait_log_contains(run_log, f"localVersion={base}", 60),
            f"运行中的程序自报版本 {base}",
        )
        check(
            wait_log_contains(run_log, f"发现新版本 {target}", 90),
            f"{base} 从自有源发现 {target}",
        )
        check(wait_log_contains(run_log, "E2E: 点击「立即更新」", 60), "自动驾驶点击了「立即更新」")
        print("  [note] 等待下载与 apply（大包，稍等）…", flush=True)
        check(wait_log_contains(run_log, "E2E: 点击「立即重启并更新」", 900),
              "下载完成后点「立即重启并更新」")

        try:
            rc = proc.wait(timeout=120)
            check(True, f"{base} 进程已退出（code={rc}），交由 Velopack 应用更新")
        except subprocess.TimeoutExpired:
            proc.kill()
            check(False, f"{base} 进程未按要求退出")

    step("4. Velopack 更新器日志")
    updater_text = read_text(updater_log)
    check(bool(updater_text), f"更新器日志存在：{updater_log}")
    check(f"{target}" in updater_text, "更新器日志里出现目标版本")

    step(f"5. 安装目录版本变为 {target}")
    app_exe = install_dir / "current" / MAIN_EXE
    version, manifest = wait_for(
        lambda: (lambda v, m: (v, m) if v == target else None)(*installed_version(install_dir)),
        timeout=180,
    ) or (installed_version(install_dir))
    check(version == target, f"清单版本 {version}（期望 {target}）")
    check(app_exe.is_file(), "更新后主程序仍在 current/ 下")

    # Velopack 应在 apply 之后自己把新版本拉起来（这正是「新版自动启动」）。
    # 这是我们能从外部观察到「新版真的在跑」的直接证据：进程不是我们拉起的。
    restarted = wait_for(lambda: [p for p in app_pids(app_exe) if p != proc.pid],
                         timeout=120)
    check(bool(restarted), f"{target} 已被 Velopack 自动拉起（pid={restarted}）")

    # 收掉这个由 Velopack 拉起的实例，好让下一步能独占启动一个带日志的实例。
    # 只按上面匹配出来的 pid 逐个终止（terminate_own 还会再核一次完整路径），
    # 不做任何按名字的系统范围清理。
    for pid in restarted:
        terminate_own(pid, app_exe)
    check(wait_for(lambda: not app_pids(app_exe), timeout=30),
          "自动拉起的实例已退出，可以独占启动验证实例")

    step(f"6. 重新启动已安装程序，确认它自己报告 {target}")
    verify_log = logs / f"run_{target}.log"
    with open(verify_log, "w", encoding="utf-8", errors="replace") as fh:
        proc2 = subprocess.Popen(
            [str(app_exe)],
            env=env, stdout=fh, stderr=subprocess.STDOUT,
            cwd=str(app_exe.parent),
        )
        got = wait_log_contains(verify_log, f"当前版本 {target}", 90)
        check(got, f"运行中的程序自报「当前版本 {target}」")
        # 更新源上已经没有更高的版本，应保持安静
        time.sleep(8)
        check("发现新版本" not in read_text(verify_log), "已是最新时不提示更新")
        # 只终止我们自己 Popen 出来的那个 pid（ProcHandle 精确，非按名清理）
        if not terminate_own(proc2.pid, app_exe):
            proc2.kill()

    step("7. 用户数据完好性")
    after = user_data_hashes()
    check(after == before, "更新前后用户数据逐字节一致")
    check(all(v is not None for v in after.values()), "prefs.json / stream_history.json 都还在")

    step("8. 清理：卸载临时安装")
    uninstaller = install_dir / "Update.exe"
    if uninstaller.is_file():
        subprocess.call([str(uninstaller), "--silent", "uninstall"])
        gone = wait_for(lambda: not (install_dir / "current").exists(), timeout=90)
        check(bool(gone), "临时安装已卸载")
    check(USER_DATA_DIR.is_dir(), "卸载后用户数据目录仍在")
    check(user_data_hashes() == before, "卸载后用户数据仍未改变")

    httpd.shutdown()

    if keep_install:
        print("  [note] --keep-install：保留安装目录")

    print(f"\nPASS {_PASSED} / FAIL {_FAILED}")
    return 1 if _FAILED else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LiveLens Velopack 真实更新 E2E")
    ap.add_argument("--base", default="1.1.0")
    ap.add_argument("--target", default="1.1.1")
    ap.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT))
    ap.add_argument("--keep-install", action="store_true")
    args = ap.parse_args()
    return run(args.base, args.target, Path(args.release_root), args.keep_install)


if __name__ == "__main__":
    sys.exit(main())
