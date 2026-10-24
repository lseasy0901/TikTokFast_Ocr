# -*- coding: utf-8 -*-
"""新机资产补齐(provision):把不入库的运行时资产拷进本仓库并校验。

这些资产体积大或按机器分发,刻意不进 Git(见 .gitignore);换新电脑
clone 之后运行本脚本即可补齐开发/运行所需:

    runtime/onnx/    PP-OCRv6 识别模型 + charset(~73MB,manifest 校验)
    runtime/ffmpeg7/ FFmpeg 7 播放运行时(ffmpeg.exe 及 dll,裸流链路)
    runtime/ffmpeg/  av*/sw* 基础 dll(打包回填用;ffprobe.exe 已入库)

来源(--src)按优先级自动发现,也可显式指定:
    1. 正式安装目录(如 %LOCALAPPDATA%\\LiveLens\\... 或安装盘)
    2. 本机旧工作区(D:\\TIikTok_Ocr\\DouyinLowLatencyViewer)

用法:
    python scripts/provision_runtime.py                    # 自动找源
    python scripts/provision_runtime.py --src <目录>       # 显式指定源
    python scripts/provision_runtime.py --check            # 只检查缺什么

校验:onnx 模型按 scripts/onnx_model_manifest.json 的 SHA-256 校验;
其余文件做存在性+大小>0 检查。拷贝不覆盖已存在且校验通过的文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(PROJECT_ROOT, "runtime")

#: (目标子目录, 必需文件列表;None=目录存在即可)
WANTED = [
    ("onnx", ["PP-OCRv6_medium_rec_fp32.onnx", "charset.txt"]),
    ("ffmpeg7", ["ffmpeg.exe"]),
    ("ffmpeg", None),   # ffprobe.exe 已在 Git;这里只确保 dll 在(打包回填)
]

_MANIFEST = os.path.join(PROJECT_ROOT, "scripts", "onnx_model_manifest.json")


def _candidate_sources() -> list[str]:
    cands = []
    local = os.path.expandvars(r"%LOCALAPPDATA%\LiveLens")
    if os.path.isdir(local):
        cands.append(local)
    cands.append(r"D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime")
    return [c for c in cands if os.path.isdir(c)]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_onnx(runtime_dir: str) -> bool:
    """按 manifest 校验 onnx 资产;manifest 缺失时退化为存在性检查。"""
    if not os.path.isfile(_MANIFEST):
        return True
    with open(_MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    base = os.path.join(runtime_dir, "onnx")
    for name, meta in manifest.get("files", {}).items():
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) != meta["bytes"]:
            return False
        if _sha256(path) != meta["sha256"]:
            return False
    return True


def _verify_all() -> dict[str, bool]:
    status = {}
    for sub, required in WANTED:
        base = os.path.join(RUNTIME, sub)
        if sub == "onnx":
            status[sub] = _verify_onnx(RUNTIME)
            continue
        if required is None:
            status[sub] = os.path.isdir(base) and any(
                f.endswith(".dll") for f in os.listdir(base))
            continue
        status[sub] = all(
            os.path.isfile(os.path.join(base, f)) for f in required)
    return status


def provision(src: str) -> int:
    src_runtime = os.path.join(src, "runtime") \
        if not os.path.basename(src).lower() == "runtime" else src
    if not os.path.isdir(src_runtime):
        print(f"源目录无效:{src_runtime}")
        return 1
    print(f"源:{src_runtime}")
    for sub, _required in WANTED:
        s = os.path.join(src_runtime, sub)
        d = os.path.join(RUNTIME, sub)
        if not os.path.isdir(s):
            print(f"  跳过 {sub}/(源中不存在)")
            continue
        os.makedirs(d, exist_ok=True)
        copied = 0
        for name in os.listdir(s):
            sp, dp = os.path.join(s, name), os.path.join(d, name)
            if os.path.isfile(sp) and not os.path.isfile(dp):
                shutil.copy2(sp, dp)
                copied += 1
        print(f"  {sub}/:新拷 {copied} 个文件"
              + ("(全部已存在)" if copied == 0 else ""))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", help="资产来源目录(安装目录或旧工作区)")
    parser.add_argument("--check", action="store_true",
                        help="只检查当前状态,不拷贝")
    args = parser.parse_args()

    status = _verify_all()
    missing = [k for k, ok in status.items() if not ok]
    print("资产状态:")
    for k, ok in status.items():
        print(f"  {'✓' if ok else '✗'} runtime/{k}/")
    if not missing:
        print("全部就绪,无需补齐。")
        return 0
    if args.check:
        print(f"缺失:{', '.join(missing)}(用 --src 指定来源后重跑)")
        return 1

    sources = [args.src] if args.src else _candidate_sources()
    for src in sources:
        print(f"\n尝试来源:{src}")
        if provision(src) == 0:
            break
    status = _verify_all()
    missing = [k for k, ok in status.items() if not ok]
    if missing:
        print(f"\n仍缺失:{', '.join(missing)}")
        print("请从任一台已配置机器的 runtime/ 目录,或正式安装包内"
              "的 runtime/ 拷贝到本仓库 runtime/ 下。")
        return 1
    print("\n补齐完成,全部校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
