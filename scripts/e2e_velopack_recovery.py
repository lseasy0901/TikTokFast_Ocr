# -*- coding: utf-8 -*-
"""
Phase 4B 启动失败自动恢复 —— 真实端到端验证。

验证的是「新版装得上但起不来」这一种失败，全链路真实：

    安装 A(1.1.0)
      → A 启动 → 发现 B(1.1.1) → 下载 → apply（真实走 UpdateDialog/自动驾驶）
      → Velopack 拉起 B
      → B 是**故意做坏的**版本（打包前删掉了 _internal/PySide6）：
        进程能起来、能读状态、能把 applying_new 推进成 booting_new，
        但 import PySide6 失败 → 没进事件循环就退出 → 永远写不出 healthy
      → 再次启动 B：读到 booting_new 且当前版本仍是 B → 触发一次回滚
      → 从更新源取 A 的 Full asset → Velopack 定向 downgrade → 重启 A
      → A 通过健康窗口 → state=healthy{to:A, blocked:B, attempts:1}
      → B 被封锁，不再被自动更新回来
      → 源上出现更高版本 C 时，封锁才解除

隔离（硬约束）：
    全程用**独立**的 LOCALAPPDATA / APPDATA（临时目录），因此 prefs.json /
    stream_history.json / update_state.json 全部落在临时目录里，
    真实 %LOCALAPPDATA%\\LiveLens 只被读取哈希做「未被改动」的证明。

用法：
    python scripts/e2e_velopack_recovery.py
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_velopack_update import (  # noqa: E402 - 复用既有 E2E 的脚手架
    MAIN_EXE, PACK_ID, app_pids, installed_version, read_text, serve, terminate_own, wait_for,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: 4B 专用的发布根：feed 与归档安装器都在这里。
#: 必须与 --root 指向同一处 —— 若从别处的归档安装器装 A（例如拿 Phase 3 的
#: 1.1.0 安装器来装），装上去的就是**没有本阶段代码**的旧构建，后面的状态机
#: 断言会全部失败，而且 feed 里的增量包会以错误的基线去补丁，产出一个
#: 半新半旧的安装目录。这个坑已经踩过一次，所以下面第 2 步显式断言 A 的启动
#: 日志里必须有状态机输出。
DEFAULT_ROOT = PROJECT_ROOT.parent / "_velopack_4b"
DEFAULT_RELEASE_ROOT = PROJECT_ROOT.parent / "_velopack_4b"

BASE = "1.1.0"      # A：好版本
BROKEN = "1.1.1"    # B：装得上但起不来
NEWER = "1.1.2"     # C：更高版本，用来验证封锁解除

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


def sha256(path: Path):
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def real_user_data() -> dict:
    """真实用户数据（只读，用来证明全程未被触碰）。"""
    base = Path(os.environ.get("REAL_LOCALAPPDATA") or os.environ.get("LOCALAPPDATA", ".")) / "LiveLens"
    return {
        name: sha256(base / name)
        for name in ("prefs.json", "stream_history.json", "update_state.json")
    }


def read_app_state(data_root: Path) -> dict:
    path = data_root / "LiveLens" / "update_state.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_state(data_root: Path, predicate, timeout: float):
    return wait_for(lambda: (lambda s: s if predicate(s) else None)(read_app_state(data_root)),
                    timeout=timeout)


def launch(app_exe: Path, env: dict, log: Path):
    fh = open(log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(app_exe)], env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=str(app_exe.parent)
    )
    return proc, fh


def run(root: Path, release_root: Path) -> int:
    feed = root / "feed"
    work = root / "e2e"
    install_dir = work / "install"
    logs = work / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    # 隔离的数据根：app 会在这个目录下建 LiveLens/
    data_root = Path(tempfile.mkdtemp(prefix="dlv-p4b-iso-"))
    (data_root / "LiveLens").mkdir(parents=True, exist_ok=True)

    step("0. 前置检查")
    for v in (BASE, BROKEN):
        pkgs = sorted(feed.glob(f"{PACK_ID}-{v}-*-full.nupkg"))
        check(bool(pkgs), f"feed 里有 {v} 全量包")
    check((feed / "releases.stable.json").is_file(), "feed 有清单")
    setup_exe = release_root / "installers" / f"{PACK_ID}-{BASE}-stable-Setup.exe"
    check(setup_exe.is_file(), f"A 的归档安装器存在（{setup_exe.name}）")
    if not setup_exe.is_file():
        return 1

    real_before = real_user_data()
    print(f"  [note] 真实用户数据指纹：{real_before}")
    print(f"  [note] 隔离数据根：{data_root}")

    step(f"1. 安装 A={BASE}")
    print(f"  [note] 安装器 {setup_exe}")
    if install_dir.exists():
        shutil.rmtree(install_dir, ignore_errors=True)
    rc = subprocess.call([str(setup_exe), "--silent", "--installto", str(install_dir),
                          "--log", str(logs / "setup_a.log")])
    check(rc == 0, f"Setup.exe 返回 {rc}")
    ver, _ = installed_version(install_dir)
    check(ver == BASE, f"安装版本 {ver}（期望 {BASE}）")
    app_exe = install_dir / "current" / MAIN_EXE

    # 本地更新源
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = serve(feed, port)
    url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(data_root)
    env["APPDATA"] = str(data_root)
    env["LIVELENS_UPDATE_URL"] = url
    env["QT_QPA_PLATFORM"] = "windows"
    print(f"  [note] 本地更新源 {url}")

    step(f"2. 启动 A，自动驾驶跑到 apply（{BASE} → {BROKEN}）")
    env_run = dict(env, LIVELENS_E2E_UPDATE="1")
    a_log = logs / "a_update.log"
    proc, fh = launch(app_exe, env_run, a_log)
    try:
        # 先证明装上去的 A 确实是带 Phase 4B 代码的构建：它的启动日志里必须有
        # 状态机输出。否则整轮 E2E 都在验证一个不含被测代码的版本。
        check(wait_for(lambda: "更新状态" in read_text(a_log), timeout=90),
              "A 的启动日志里有状态机输出（装的是 4B 构建，不是旧构建）")
        check("发现新版本" in read_text(a_log) or
              wait_for(lambda: "发现新版本" in read_text(a_log), timeout=120),
              f"A 发现 {BROKEN}")
        check(wait_for(lambda: "E2E: 点击「立即重启并更新」" in read_text(a_log), timeout=900),
              "下载完成并点下「立即重启并更新」")
        proc.wait(timeout=120)
        check(True, "A 退出，交给 Velopack 应用更新")
    finally:
        fh.close()
        if proc.poll() is None:
            terminate_own(proc.pid, app_exe)

    step(f"3. B={BROKEN} 装上了，但启动即失败")
    wait_for(lambda: installed_version(install_dir)[0] == BROKEN, timeout=180)
    ver, _ = installed_version(install_dir)
    check(ver == BROKEN, f"安装版本变为 {ver}（期望 {BROKEN}）")

    # B 由 Velopack 拉起：它会在退出前把 applying_new 推进成 booting_new
    st = wait_state(data_root, lambda s: s if s.get("state") == "booting_new" else None, timeout=120)
    check(st is not None, f"B 把状态推进成了 booting_new（{st}）")
    if st:
        check(st.get("from") == BASE and st.get("to") == BROKEN,
              "booting_new 记录 from=A / to=B")
        check(st.get("attempts") == 0 and st.get("blocked_version") is None,
              "booting_new 的 attempts=0、尚无封锁")

    # B 起不来：进程很快退出，且状态**没有**变成 healthy
    wait_for(lambda: not app_pids(app_exe), timeout=120)
    time.sleep(3)
    check(read_app_state(data_root).get("state") == "booting_new",
          "B 未通过健康窗口，状态保持 booting_new（没有 healthy 标记）")

    step("4. 再次启动 B → 检测到启动失败 → 自动回滚到 A")
    b_log = logs / "b_recovery.log"
    proc, fh = launch(app_exe, env, b_log)
    try:
        check(wait_for(lambda: "准备回滚" in read_text(b_log), timeout=120),
              "B 判定上次启动失败并准备回滚")
        check(read_app_state(data_root).get("state") == "recovery",
              "状态推进到 recovery")
        check(wait_for(lambda: "开始回滚到" in read_text(b_log), timeout=180),
              "开始定向回滚（从更新源取 A 的 Full asset）")
        proc.wait(timeout=600)
        print("  [note] B 进程已退出（Velopack 接管应用回滚）", flush=True)
    finally:
        fh.close()
        if proc.poll() is None:
            terminate_own(proc.pid, app_exe)

    step("5. 回滚结果")
    rolled = wait_for(lambda: installed_version(install_dir)[0] == BASE, timeout=300)
    ver, _ = installed_version(install_dir)
    check(ver == BASE, f"安装版本回滚到 {ver}（期望 {BASE}）")
    check(bool(rolled), "Velopack 实际完成了 downgrade")

    restarted = wait_for(lambda: app_pids(app_exe), timeout=180)
    check(bool(restarted), f"回滚后的 {BASE} 被 Velopack 自动拉起（pid={restarted}）")

    # A 要过健康窗口（HEALTH_WINDOW_MS=12s）才会写成 healthy
    st = wait_state(data_root, lambda s: s if s.get("state") == "healthy" else None, timeout=180)
    check(st is not None, "回滚后的 A 通过健康窗口 → healthy")
    if st:
        check(st.get("to") == BASE, "healthy 记录当前版本为 A")
        check(st.get("blocked_version") == BROKEN, f"B={BROKEN} 被标记 blocked")
        check(st.get("attempts") == 1, "attempts == 1（回滚次数已用尽）")

    for pid in list(restarted or []):
        terminate_own(pid, app_exe)
    wait_for(lambda: not app_pids(app_exe), timeout=60)

    step(f"6. B 不会被自动更新回来（封锁生效）")
    a_log2 = logs / "a_blocked.log"
    proc, fh = launch(app_exe, env, a_log2)     # 不开自动驾驶，只看检查结果
    try:
        check(wait_for(lambda: "已被封锁，跳过本次更新" in read_text(a_log2), timeout=120),
              "A 的更新检查跳过了被封锁的 B")
        check("发现新版本" not in read_text(a_log2), "没有再次提示更新 B（不循环）")
    finally:
        fh.close()
        terminate_own(proc.pid, app_exe)
    check(wait_state(data_root, lambda s: s if s.get("blocked_version") == BROKEN else None,
                     timeout=30) is not None, "封锁状态保持不变")

    step(f"7. 出现更高版本 {NEWER} → 解除封锁")
    make_newer = root / "1.1.0" / "dist" / "DouyinLowLatencyViewer"
    rc = subprocess.call([
        "vpk", "pack", "--packId", PACK_ID, "--packVersion", NEWER,
        "--packDir", str(make_newer), "--outputDir", str(feed), "--channel", "stable",
        "--mainExe", MAIN_EXE, "--packTitle", "LiveLens", "--packAuthors", "LiveLens",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check(rc == 0, f"打包出更高版本 {NEWER} 到 feed")

    a_log3 = logs / "a_unblocked.log"
    proc, fh = launch(app_exe, env, a_log3)
    try:
        check(wait_for(lambda: "解除版本" in read_text(a_log3), timeout=120),
              "发现更高版本后解除对 B 的封锁")
        check(wait_for(lambda: f"发现新版本 {NEWER}" in read_text(a_log3), timeout=120),
              f"并允许更新到 {NEWER}")
    finally:
        fh.close()
        terminate_own(proc.pid, app_exe)
    st = read_app_state(data_root)
    check(st.get("blocked_version") in (None, ""), f"状态里封锁已清除（{st.get('blocked_version')}）")

    step("8. 隔离与用户数据")
    iso_files = sorted(p.name for p in (data_root / "LiveLens").iterdir())
    check("update_state.json" in iso_files, f"状态文件写在隔离目录里：{iso_files}")
    real_after = real_user_data()
    check(real_after == real_before, "真实 %LOCALAPPDATA%\\LiveLens 逐字节未变")

    step("9. 清理临时安装")
    uninstaller = install_dir / "Update.exe"
    if uninstaller.is_file():
        subprocess.call([str(uninstaller), "--silent", "uninstall"])
        check(bool(wait_for(lambda: not (install_dir / "current").exists(), timeout=90)),
              "临时安装已卸载")
    check((data_root / "LiveLens" / "update_state.json").is_file(),
          "卸载后隔离数据（含状态文件）仍在 —— 与真实用户数据同理，更新/卸载不动它")

    if httpd is not None:
        httpd.shutdown()

    print(f"\nPASS {_PASSED} / FAIL {_FAILED}")
    return 1 if _FAILED else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LiveLens 启动失败自动恢复 E2E")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT))
    args = ap.parse_args()

    # 真实 LOCALAPPDATA 会被隔离 env 覆盖，先记下来供比对
    os.environ.setdefault("REAL_LOCALAPPDATA", os.environ.get("LOCALAPPDATA", ""))
    return run(Path(args.root), Path(args.release_root))


if __name__ == "__main__":
    sys.exit(main())
