#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布打包策略 —— 验证脚本

锁住「单文件 CDN」发布形态，防止以后被无声改回去：

    vpk pack 必须带  --delta None     （每版只发一个 Full 包，不发增量包）
                     --noPortable     （不发便携包）
    且必须保持        packId / channel / mainExe / 安装器归档 不变

只做纯逻辑与最小文件操作，不调用 vpk、不构建、不联网。

直接 ``python test_release_packaging.py`` 运行。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import velopack_release as rel  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {message}", flush=True)
    else:
        _FAILED += 1
        print(f"    [FAIL] {message}", flush=True)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


def capture_pack_command(notes=None):
    """拦住 subprocess，只把 vpk 命令行取回来。"""
    captured = {}
    real_run = rel.run
    rel.run = lambda cmd, env=None: captured.setdefault("cmd", [str(c) for c in cmd])
    try:
        rel.pack(Path("packdir"), Path("outdir"), "1.1.0", notes)
    finally:
        rel.run = real_run
    return captured["cmd"]


# ======================================================================
# A. vpk pack 参数固定
# ======================================================================
def test_pack_flags() -> None:
    section("A. vpk pack 参数")
    cmd = capture_pack_command()

    check(cmd[:2] == ["vpk", "pack"], "调用的是 vpk pack")
    check("--delta" in cmd and cmd[cmd.index("--delta") + 1] == "None",
          "--delta None（不生成增量包，不依赖交互/默认值）")
    check("--noPortable" in cmd, "--noPortable（不生成便携包）")

    def value_of(flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    check(value_of("--packId") == "LiveLensDesktop", "packId 保持 LiveLensDesktop")
    check(value_of("--channel") == "stable", "channel 保持 stable")
    check(value_of("--mainExe") == "DouyinLowLatencyViewer.exe",
          "mainExe 保持 DouyinLowLatencyViewer.exe")
    check(value_of("--packVersion") == "1.1.0", "版本号来自参数（由 utils/version.py 提供）")
    check(value_of("--outputDir") == "outdir", "输出目录由参数决定")
    check("--instLocation" not in cmd and "--shortcuts" not in cmd,
          "没有额外的安装位置/快捷方式参数（保持既有安装行为）")

    with_notes = capture_pack_command(Path("notes.md"))
    check("--releaseNotes" in with_notes, "给了说明文件时才传 --releaseNotes")

    # 签名：没有环境变量时不带参数（未签名状态保持不变）
    os.environ.pop("VELOPACK_SIGN_PARAMS", None)
    os.environ.pop("VELOPACK_AZURE_TRUSTED_SIGN_FILE", None)
    cmd2 = capture_pack_command()
    check("--signParams" not in cmd2 and "--azureTrustedSignFile" not in cmd2,
          "未配置签名环境变量时不注入签名参数")


# ======================================================================
# B. 归档只收安装器
# ======================================================================
def test_archive_only_installer() -> None:
    section("B. 归档范围")

    root = Path(tempfile.mkdtemp(prefix="dlv-pkg-"))
    try:
        feed = root / "feed"
        feed.mkdir()
        (feed / f"{rel.PACK_ID}-{rel.CHANNEL}-Setup.exe").write_bytes(b"setup")
        # 历史产物（例如旧 feed 里遗留的），不该被归档带走
        (feed / f"{rel.PACK_ID}-1.1.0-{rel.CHANNEL}-full.nupkg").write_bytes(b"full")
        (feed / f"{rel.PACK_ID}-1.1.0-{rel.CHANNEL}-delta.nupkg").write_bytes(b"delta")
        (feed / f"{rel.PACK_ID}-{rel.CHANNEL}-Portable.zip").write_bytes(b"portable")

        dest = rel.archive_installer(feed, root, "1.1.0")
        archived = sorted(p.name for p in (root / "installers").iterdir())

        check(dest.name == f"{rel.PACK_ID}-1.1.0-{rel.CHANNEL}-Setup.exe",
              "归档文件名带版本号")
        check(archived == [f"{rel.PACK_ID}-1.1.0-{rel.CHANNEL}-Setup.exe"],
              f"归档目录里只有安装器：{archived}")
        check(not any("delta" in n or "Portable" in n for n in archived),
              "不归档增量包 / 便携包")
        check((root / "installers") not in list(feed.iterdir()),
              "归档不写进 feed（不给更新源增体积）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ======================================================================
# C. 发布目录不落进源码树
# ======================================================================
def test_release_root_outside_source() -> None:
    section("C. 产物位置与身份")

    default_root = rel.DEFAULT_RELEASE_ROOT.resolve()
    check(PROJECT_ROOT.resolve() not in default_root.parents
          and default_root != PROJECT_ROOT.resolve(),
          f"默认发布根在源码目录之外：{default_root}")
    check(rel.PACK_ID != "LiveLens",
          "packId 不与用户数据目录名（LiveLens）相同，避免 %LOCALAPPDATA% 冲突")
    check(rel.CHANNEL == "stable", "单一渠道 stable")


def main() -> int:
    print("=" * 68)
    print("发布打包策略 —— 验证")
    print("=" * 68)
    test_pack_flags()
    test_archive_only_installer()
    test_release_root_outside_source()
    print("\n" + "=" * 68)
    print(f"PASS {_PASSED} / FAIL {_FAILED}")
    print("=" * 68)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
