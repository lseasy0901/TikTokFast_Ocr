# -*- coding: utf-8 -*-
"""
PP-OCRv6 随包模型完整性校验（构建期工具，不随正式包发布）

为什么需要它：
    生产模型 runtime/ppocr/PP-OCRv6_medium_rec_infer/ 不进 Git（见 .gitignore），
    只由打包阶段按机器供应。换机器或重新构建时，"拿到的是不是同一份权重"
    必须能当场证明，否则 OCR 行为的变化会被当成代码回归去查。

它做什么：
    1. 读 scripts/ppocr_model_manifest.json（进 Git 的期望值）
    2. 逐文件比对 runtime/ppocr/<dir_name>/ 的字节数与 SHA-256
    3. 确认 PPOCRModelLocator 判定该目录可用，且 find_model_dir() 解析到的是
       **随包目录**而不是开发目录
    4. 全部一致 -> 退出码 0；否则列出差异 -> 非 0

它不做什么：
    不修改模型、不写 OCR 运行时、不做任何推理、不触碰识别规则。

用法：
    python scripts/verify_ppocr_model.py            # 校验
    python scripts/verify_ppocr_model.py --record   # 按当前模型重写 manifest
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
MANIFEST_PATH = os.path.join(_HERE, "ppocr_model_manifest.json")

sys.path.insert(0, PROJECT_ROOT)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def measure(model_dir: str, file_names) -> dict:
    files = {}
    total = 0
    for name in file_names:
        path = os.path.join(model_dir, name)
        size = os.path.getsize(path)
        total += size
        files[name] = {"bytes": size, "sha256": sha256_of(path)}
    return {"total_bytes": total, "files": files}


def record(model_dir: str, file_names, manifest: dict) -> int:
    measured = measure(model_dir, file_names)
    manifest["total_bytes"] = measured["total_bytes"]
    manifest["files"] = measured["files"]
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("[record] 已写入 %s" % MANIFEST_PATH)
    for name, meta in measured["files"].items():
        print("  %-22s %10d bytes  %s" % (name, meta["bytes"], meta["sha256"]))
    print("  total %d bytes" % measured["total_bytes"])
    return 0


def verify(model_dir: str, manifest: dict) -> int:
    failures = []
    expected_files = manifest["files"]

    print("模 型 目 录 : %s" % model_dir)
    print("manifest   : %s" % MANIFEST_PATH)
    print()

    for name, expected in expected_files.items():
        path = os.path.join(model_dir, name)
        if not os.path.isfile(path):
            failures.append("缺少文件 %s" % name)
            print("  [FAIL] %-22s 缺失" % name)
            continue
        size = os.path.getsize(path)
        if size != expected["bytes"]:
            failures.append("%s 字节数不符（期望 %d，实际 %d）"
                            % (name, expected["bytes"], size))
        if size == 0:
            failures.append("%s 是 0 字节（PPOCRModelLocator 会判为未提供）" % name)
        digest = sha256_of(path)
        if digest != expected["sha256"]:
            failures.append("%s SHA-256 不符（期望 %s，实际 %s）"
                            % (name, expected["sha256"][:16], digest[:16]))
        ok = (size == expected["bytes"] and digest == expected["sha256"])
        print("  [%s] %-22s %10d bytes  %s"
              % ("PASS" if ok else "FAIL", name, size, digest[:16]))

    total = sum(os.path.getsize(os.path.join(model_dir, n))
                for n in expected_files
                if os.path.isfile(os.path.join(model_dir, n)))
    if total != manifest["total_bytes"]:
        failures.append("目录总字节数不符（期望 %d，实际 %d）"
                        % (manifest["total_bytes"], total))
    print("\n  目录总大小: %d bytes (%.1f MB)，manifest 期望 %d bytes"
          % (total, total / 1048576.0, manifest["total_bytes"]))

    # 定位器视角：与构建闸门、生产运行看到的是同一份判据
    from ocr.ppocr_engine import PPOCRModelLocator

    if not PPOCRModelLocator.is_usable_model_dir(model_dir):
        failures.append("PPOCRModelLocator.is_usable_model_dir() 判为不可用")
        print("  [FAIL] PPOCRModelLocator 判定不可用")
    else:
        print("  [PASS] PPOCRModelLocator 判定可用")

    resolved = PPOCRModelLocator.find_model_dir()
    bundled = os.path.normcase(os.path.join(
        PROJECT_ROOT, manifest["relpath"].replace("/", os.sep)))
    if os.path.normcase(resolved) != bundled:
        failures.append("find_model_dir() 解析到 %s，期望随包目录 %s"
                        % (resolved, bundled))
        print("  [FAIL] find_model_dir() = %s（不是随包目录）" % resolved)
    else:
        print("  [PASS] find_model_dir() 解析到随包目录")

    print()
    if failures:
        print("失败 %d 项：" % len(failures))
        for item in failures:
            print("  - %s" % item)
        return 1
    print("模型完整性校验通过：与 manifest 完全一致。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PP-OCRv6 随包模型完整性校验")
    parser.add_argument("--record", action="store_true",
                        help="按当前模型重写 manifest（供应新模型后使用）")
    parser.add_argument("--model-dir", default=None,
                        help="模型目录（默认取 manifest 的 relpath）")
    args = parser.parse_args()

    manifest = load_manifest()
    model_dir = args.model_dir or os.path.join(
        PROJECT_ROOT, manifest["relpath"].replace("/", os.sep))

    if not os.path.isdir(model_dir):
        print("模型目录不存在：%s" % model_dir)
        print("请先按 manifest %s 供应模型（见 douyin_viewer.spec 的构建闸门）。"
              % manifest["dir_name"])
        return 1

    if args.record:
        return record(model_dir, list(manifest["files"]), manifest)
    return verify(model_dir, manifest)


if __name__ == "__main__":
    sys.exit(main())
