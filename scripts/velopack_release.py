# -*- coding: utf-8 -*-
"""
LiveLens 正式发布链：PyInstaller onedir → vpk pack → Velopack release (Phase 3)。

用法（在项目根目录执行）：

    python scripts/velopack_release.py                 # 构建 + 打包
    python scripts/velopack_release.py --skip-build    # 复用现有 onedir，只打包
    python scripts/velopack_release.py --notes notes.md

产物（默认落在**源码目录之外**）：

    <项目父目录>/_velopack_release/
    ├── feed/                                  ← 整个目录就是更新源
    │   ├── LiveLensDesktop-stable-Setup.exe       安装器（首次安装用）
    │   ├── LiveLensDesktop-<版本>-stable-full.nupkg   全量包（自动更新用，每版一个）
    │   ├── releases.stable.json                更新索引（清单，客户端取的就是它）
    │   ├── assets.stable.json / RELEASES-stable  Velopack 自家的 feed 元数据，原样保留
    ├── installers/                            带版本号的安装器归档（不进更新源）
    │   └── LiveLensDesktop-<版本>-stable-Setup.exe
    └── <版本>/dist, <版本>/build               PyInstaller 的中间产物（不上传）

    发布形态固定为「单文件 CDN」：每个版本只有 Setup.exe + 一个 Full.nupkg，
    不生成增量包、不生成便携包（理由见 pack()）。

    实际分发时：`releases.stable.json` 这类小文件放 livelen.icu，
    Setup.exe / .nupkg 这类大文件交给第三方托管（客户端只认
    utils/update_source.py 里那一条源地址，不感知后端怎么摆）。

    归档那一份是必需的：feed 里的安装器文件名不含版本，发新版会覆盖它，
    而上个版本的用户「当时拿到的是哪个安装器」需要能找回来。

    所有版本共用一个 feed/：更新源要求清单与 .nupkg 同目录，且增量包必须能看到
    上一版本的全量包才能算出来。

    把这个目录的内容**原样上传**到 utils/update_source.py 里配置的源目录
    （地址只在那里定义一次，本脚本不重复它），客户端即可自动更新。
    客户端不依赖任何中转网盘或临时 CDN。

为什么产物必须离开源码目录：
    Setup.exe / .nupkg / packages/ / 临时 release / 测试安装 都是大型生成物，
    堆进仓库既污染工作区又容易误提交。构建中间产物（PyInstaller 的 build 与
    dist）同理，因此这里显式指定 distpath / workpath，不用项目默认的
    ./build 与 ./dist。

版本号只有一个来源：
    构建版本取自 utils/version.py（规格见 douyin_viewer.spec 的同一条约定），
    `vpk pack -v` 与它必须一致，因此这里 exec 读取、不接受手写第二份。
    客户端运行时显示的版本优先取 Velopack 的当前安装版本。

签名：
    正式发布必须签名，但私钥/证书口令**不进仓库**：签名参数只从环境变量注入。
        VELOPACK_SIGN_PARAMS                 → --signParams  （signtool 参数串，
                                               形如 "/fd sha256 /a /f cert.pfx /p ..."）
        VELOPACK_AZURE_TRUSTED_SIGN_FILE     → --azureTrustedSignFile
    两个都没设时，脚本做的是**未签名**构建并在结束时显著告警 —— 不静默降级，
    也不自建第二套签名机制。
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------------
# 发布配置（唯一的第二份？不是 —— packId / channel 是构建期概念，
# 运行时只认 utils.update_source.py 里的源地址，两者不重复）
# ----------------------------------------------------------------------
#: Velopack 的包标识。**刻意不等于**用户数据目录名 "LiveLens"：
#: 用户数据在 %LOCALAPPDATA%\LiveLens\（Phase 0 决定，不得改动），
#: 而 Velopack 在 Windows 上默认把安装根目录/回退目录建成
#: %LOCALAPPDATA%\<packId>。若 packId 也叫 LiveLens，两者就会指向同一个目录，
#: 卸载或更新清理时可能波及永久用户数据。故取一个稳定、独立、语义不冲突的名字。
PACK_ID = "LiveLensDesktop"

#: 单一渠道。不做 beta/dev 多渠道。
CHANNEL = "stable"

#: 主程序文件名，与 douyin_viewer.spec 的 EXE(name=...) 一致，不得改名。
MAIN_EXE = "DouyinLowLatencyViewer.exe"

#: onedir 目录名，与 spec 的 COLLECT(name=...) 一致。
ONEDIR_NAME = "DouyinLowLatencyViewer"

PACK_TITLE = "LiveLens"
PACK_AUTHORS = "LiveLens"

#: 默认发布根目录：源码目录的**同级**目录，不污染工作区。
DEFAULT_RELEASE_ROOT = PROJECT_ROOT.parent / "_velopack_release"

#: 构建期从 PATH 里剔除的目录关键字。本机装了 ACLOS 版 FFmpeg 与 Tesseract，
#: 若留在 PATH 里，PyInstaller 会把开发者本机的运行库一并打进包。
PATH_STRIP_KEYWORDS = ("ACLOS", "Tesseract-OCR")


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[release] {msg}", flush=True)


def read_version() -> str:
    """版本号唯一来源：utils/version.py（exec 读取，不 import）。"""
    ns: dict = {}
    with open(PROJECT_ROOT / "utils" / "version.py", "r", encoding="utf-8") as f:
        exec(f.read(), ns)  # noqa: S102 - 见模块 docstring：spec 也是这么读的
    return ns["__version__"]


def clean_path_env() -> dict:
    """返回一份剔除了开发机运行库目录的环境变量。

    只影响本次构建的子进程，不改动调用者的环境。
    """
    env = os.environ.copy()
    parts = env.get("PATH", "").split(os.pathsep)
    kept = [p for p in parts if not any(k.lower() in p.lower() for k in PATH_STRIP_KEYWORDS)]
    if len(kept) != len(parts):
        log(f"PATH 已剔除 {len(parts) - len(kept)} 个开发机运行库目录（{', '.join(PATH_STRIP_KEYWORDS)}）")
    env["PATH"] = os.pathsep.join(kept)
    return env


def signing_args() -> list:
    """从环境变量构造签名参数。私钥/口令只经环境注入，不进仓库。"""
    args: list = []
    sign_params = (os.environ.get("VELOPACK_SIGN_PARAMS") or "").strip()
    azure_file = (os.environ.get("VELOPACK_AZURE_TRUSTED_SIGN_FILE") or "").strip()
    if sign_params:
        args += ["--signParams", sign_params]
        log("签名：使用 VELOPACK_SIGN_PARAMS（signtool）")
    if azure_file:
        args += ["--azureTrustedSignFile", azure_file]
        log("签名：使用 VELOPACK_AZURE_TRUSTED_SIGN_FILE（Azure Trusted Signing）")
    if not args:
        log("签名：未配置（VELOPACK_SIGN_PARAMS / VELOPACK_AZURE_TRUSTED_SIGN_FILE 均为空）")
    return args


# ----------------------------------------------------------------------
# 构建
# ----------------------------------------------------------------------
def run(cmd: list, env=None) -> None:
    log("执行: " + " ".join(str(c) for c in cmd))
    subprocess.check_call([str(c) for c in cmd], env=env)


def build_onedir(dist_dir: Path, work_dir: Path) -> Path:
    """PyInstaller onedir 构建。返回 onedir 目录。"""
    spec = PROJECT_ROOT / "douyin_viewer.spec"
    run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--distpath", dist_dir,
            "--workpath", work_dir,
            spec,
        ],
        env=clean_path_env(),
    )
    onedir = dist_dir / ONEDIR_NAME
    if not (onedir / MAIN_EXE).is_file():
        raise SystemExit(f"构建失败：{onedir / MAIN_EXE} 不存在")
    return onedir


def pack(onedir: Path, out_dir: Path, version: str, notes: Path | None) -> None:
    """vpk pack 生成 Setup.exe / 全量 nupkg / 更新源清单。

    刻意固定成「一个版本一个 Full 包」的发布形态，不接受默认值：
      --delta None   不生成增量包。增量包只有在前一版本仍存在于 feed 里时才有
                     意义，而我们的发布形态是「索引在 livelen.icu、大文件在
                     第三方托管」，多一个会变动的中间产物只会让上传与回滚更
                     复杂。全量包单文件即可覆盖所有旧版本用户。
      --noPortable   不生成 Portable.zip。便携包是给「解压即用」场景的，而
                     LiveLens 只走 Setup.exe 安装 + 自动更新这条路径，多打一个
                     250MB 的包纯属浪费带宽与存储。
                     注意 Setup.exe 不受影响：打包流程里它是从 **全量包** 与
                     打包目录生成的，与便携包无关（见 Velopack 的
                     PackageBuilder：CreateSetupPackage 的输入是 releasePath）。

    保留 vpk 自己写的 assets.stable.json / RELEASES-stable —— 那是 Velopack 的
    feed 元数据，不为了「目录好看」去裁剪它。
    """
    cmd = [
        "vpk", "pack",
        "--packId", PACK_ID,
        "--packVersion", version,
        "--packDir", onedir,
        "--outputDir", out_dir,
        "--channel", CHANNEL,
        "--mainExe", MAIN_EXE,
        "--packTitle", PACK_TITLE,
        "--packAuthors", PACK_AUTHORS,
        "--delta", "None",
        "--noPortable",
    ]
    if notes is not None:
        cmd += ["--releaseNotes", notes]
    cmd += signing_args()
    run(cmd)


def archive_installer(feed_dir: Path, release_root: Path, version: str) -> Path:
    """把本次的 Setup.exe 另存一份带版本号的副本。

    原因：feed 里的安装器文件名不含版本（`<packId>-<channel>-Setup.exe`），
    发新版本时会把它覆盖掉 —— 于是「上个版本的用户拿到的是哪个安装器」就再也
    找不回来了，而增量更新验证、回滚、客服定位问题都要用到它。
    副本放在 feed **之外**，不给更新源增加无谓的体积。
    """
    src = feed_dir / f"{PACK_ID}-{CHANNEL}-Setup.exe"
    dest_dir = release_root / "installers"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{PACK_ID}-{version}-{CHANNEL}-Setup.exe"
    shutil.copy2(src, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="LiveLens Velopack 发布")
    parser.add_argument("--version", help="覆盖版本号（默认取 utils/version.py）")
    parser.add_argument("--skip-build", action="store_true", help="复用现有 dist 里的 onedir，不重新构建")
    parser.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT),
                        help="发布根目录（默认在源码目录之外）")
    parser.add_argument("--notes", help="markdown 格式的更新说明文件")
    parser.add_argument("--dist-dir", help="PyInstaller 输出目录（默认 <发布目录>/dist）")
    parser.add_argument("--work-dir", help="PyInstaller 中间目录（默认 <发布目录>/build）")
    args = parser.parse_args()

    version = (args.version or read_version()).strip()
    release_root = Path(args.release_root)
    # 所有版本的包与更新源清单都放**同一个**目录：
    #   - 更新源（HttpSource）要求 releases.<channel>.json 与 .nupkg 同目录，
    #     各版本分目录会让客户端取不到包；
    #   - 增量包要靠「上一个版本的全量包就在旁边」才能算出来。
    feed_dir = release_root / "feed"
    build_root = release_root / version
    dist_dir = Path(args.dist_dir) if args.dist_dir else build_root / "dist"
    work_dir = Path(args.work_dir) if args.work_dir else build_root / "build"

    # 防呆：生成物不得落进源码目录
    if PROJECT_ROOT in release_root.resolve().parents or release_root.resolve() == PROJECT_ROOT:
        raise SystemExit(f"发布目录不能位于源码目录内：{release_root}")

    log(f"版本 {version} / packId {PACK_ID} / channel {CHANNEL}")
    log(f"更新源目录 {feed_dir}")
    feed_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_build:
        onedir = dist_dir / ONEDIR_NAME
        if not (onedir / MAIN_EXE).is_file():
            raise SystemExit(f"--skip-build 但找不到现有构建：{onedir / MAIN_EXE}")
        log(f"复用现有构建 {onedir}")
    else:
        onedir = build_onedir(dist_dir, work_dir)

    notes = Path(args.notes).resolve() if args.notes else None
    pack(onedir, feed_dir, version, notes)
    archived = archive_installer(feed_dir, release_root, version)
    log(f"安装器已归档：{archived}")

    log("完成。更新源目录内容：")
    for p in sorted(feed_dir.iterdir()):
        if p.is_file():
            log(f"    {p.name}  ({p.stat().st_size / (1024 * 1024):.1f} MB)")
    if not signing_args():
        log("!! 本次为未签名构建：正式发布前必须配置签名环境变量后重新打包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
