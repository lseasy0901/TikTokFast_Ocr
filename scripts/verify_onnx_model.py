# -*- coding: utf-8 -*-
"""
ONNX 后端随包产物完整性校验（构建期工具，不随正式包发布）

为什么需要它：
    ONNX 后端的运行时产物 runtime/onnx/ 不进 Git（见 .gitignore），只由部署阶段
    按机器供应。它只有两个文件，缺一不可：

        PP-OCRv6_medium_rec_fp32.onnx   模型本体
        charset.txt                     字符集（由 scripts/export_onnx_charset.py
                                        从 PP-OCRv6 inference.yml 导出）

    换机器或重新构建时，"拿到的是不是同一份产物"必须能当场证明，否则 OCR 行为
    的变化会被当成代码回归去查。字符集尤其危险：**项数或顺序只要差一点，
    每个字符索引都会错位**，而且不会报错，只会把结果悄悄解错。

它做什么：
    1. 读 scripts/onnx_model_manifest.json（进 Git 的期望值）
    2. 逐文件比对 runtime/onnx/ 的字节数与 SHA-256
    3. 校验 charset.txt 的项数，并与运行时认知交叉核对
       （ocr.onnx_preprocess.CHARSET_ENTRIES / CHARSET_FINAL_SIZE）
    4. 确认 ONNXModelLocator 解析到的是**随包目录**而不是开发兜底路径
    5. 全部一致 -> 退出码 0；否则列出差异 -> 非 0

它不做什么：
    不修改模型、不写 OCR 运行时、不做任何推理、不触碰识别规则、不碰 spec /
    Installer / requirements —— 把本 manifest 接进构建闸门属于打包阶段的事。
    （注意：ONNX 后端目前只在源码环境可用，尚未随包投放。）

用法：
    python scripts/verify_onnx_model.py            # 校验
    python scripts/verify_onnx_model.py --record   # 按当前产物重写 manifest
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
MANIFEST_PATH = os.path.join(_HERE, "onnx_model_manifest.json")

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


def measure(artifact_dir: str, file_names) -> dict:
    files = {}
    total = 0
    for name in file_names:
        path = os.path.join(artifact_dir, name)
        size = os.path.getsize(path)
        total += size
        files[name] = {"bytes": size, "sha256": sha256_of(path)}
    return {"total_bytes": total, "files": files}


def record(artifact_dir: str, manifest: dict) -> int:
    names = sort_names(list(manifest["files"]))
    measured = measure(artifact_dir, names)

    from ocr.onnx_preprocess import CHARSET_ENTRIES, CHARSET_FINAL_SIZE, load_charset

    charset = load_charset(os.path.join(artifact_dir, "charset.txt"))

    manifest["files"] = measured["files"]
    manifest["total_bytes"] = measured["total_bytes"]
    manifest["charset_entries"] = len(charset)
    manifest["charset_final_size"] = len(charset) + 2
    manifest["recorded_at"] = datetime.date.today().isoformat()
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("[record] 已写入 %s" % MANIFEST_PATH)
    for name, meta in measured["files"].items():
        print("  %-34s %10d bytes  %s" % (name, meta["bytes"], meta["sha256"]))
    print("  total %d bytes | charset entries=%d (runtime const=%d), "
          "final=%d (runtime const=%d)"
          % (measured["total_bytes"], len(charset), CHARSET_ENTRIES,
             len(charset) + 2, CHARSET_FINAL_SIZE))
    return 0


def verify(artifact_dir: str, manifest: dict) -> int:
    failures = []
    expected_files = manifest["files"]
    expected_names = sort_names(list(expected_files))
    actual_names = sort_names([
        n for n in os.listdir(artifact_dir)
        if os.path.isfile(os.path.join(artifact_dir, n))
    ]) if os.path.isdir(artifact_dir) else []

    print("产 物 目 录 : %s" % artifact_dir)
    print("manifest   : %s" % MANIFEST_PATH)
    print()

    # runtime/onnx/ 只允许这两个文件：多一个都可能意味着投放了不该有的东西
    # （例如误把 Paddle 侧的 inference.yml 复制进来）。
    extra = [n for n in actual_names if n not in expected_names]
    missing_extra = [n for n in expected_names if n not in actual_names]
    if missing_extra:
        failures.append("目录里缺少文件: %s" % missing_extra)
    if extra:
        failures.append("目录里有 manifest 之外的文件: %s（runtime/onnx/ 只应有 "
                        "onnx + charset.txt）" % extra)

    for name in expected_names:
        path = os.path.join(artifact_dir, name)
        if not os.path.isfile(path):
            failures.append("缺少文件 %s" % name)
            print("  [FAIL] %-34s 缺失" % name)
            continue
        expected = expected_files[name]
        size = os.path.getsize(path)
        if size != expected["bytes"]:
            failures.append("%s 字节数不符（期望 %d，实际 %d）"
                            % (name, expected["bytes"], size))
        if size == 0:
            failures.append("%s 是 0 字节（定位器会判为未提供）" % name)
        digest = sha256_of(path)
        if digest != expected["sha256"]:
            failures.append("%s SHA-256 不符（期望 %s，实际 %s）"
                            % (name, expected["sha256"][:16], digest[:16]))
        ok = (size == expected["bytes"] and digest == expected["sha256"])
        print("  [%s] %-34s %10d bytes  %s"
              % ("PASS" if ok else "FAIL", name, size, digest[:16]))

    total = sum(os.path.getsize(os.path.join(artifact_dir, n))
                for n in expected_names
                if os.path.isfile(os.path.join(artifact_dir, n)))
    if total != manifest["total_bytes"]:
        failures.append("目录总字节数不符（期望 %d，实际 %d）"
                        % (manifest["total_bytes"], total))
    print("\n  目录总大小: %d bytes (%.1f MB)，manifest 期望 %d bytes"
          % (total, total / 1048576.0, manifest["total_bytes"]))

    # ---- 字符集：项数 + 运行时视角 ----
    # 项数不对 = 每个字符索引都错位，且**不会报错**，所以这是必须拦的一条。
    charset_path = os.path.join(artifact_dir, "charset.txt")
    if os.path.isfile(charset_path):
        from ocr.onnx_preprocess import (
            CHARSET_ENTRIES, CHARSET_FINAL_SIZE, load_charset,
        )

        try:
            charset = load_charset(charset_path)
        except Exception as e:  # noqa: BLE001 - 读不出来就是一条失败，不吞
            failures.append("charset.txt 无法按运行时口径读取: %s" % e)
            print("  [FAIL] charset.txt 无法读取: %s" % e)
        else:
            exp_entries = manifest.get("charset_entries", CHARSET_ENTRIES)
            if len(charset) != exp_entries:
                failures.append("charset 项数不符（manifest 期望 %d，实际 %d）"
                                % (exp_entries, len(charset)))
            if len(charset) != CHARSET_ENTRIES:
                failures.append("charset 项数与本机运行时常量不符"
                                "（常量 %d，实际 %d）—— 请同步 "
                                "ocr/onnx_preprocess.CHARSET_ENTRIES"
                                % (CHARSET_ENTRIES, len(charset)))
            final = len(charset) + 2   # + blank + space
            if final != CHARSET_FINAL_SIZE:
                failures.append("最终字符表长度不符（期望 %d，实际 %d）"
                                % (CHARSET_FINAL_SIZE, final))
            ok = (len(charset) == exp_entries == CHARSET_ENTRIES
                  and final == CHARSET_FINAL_SIZE)
            print("  [%s] %-34s %10d 项  (最终字符表 %d 项)"
                  % ("PASS" if ok else "FAIL", "charset.txt 项数",
                     len(charset), final))

    # ---- 定位器视角：确认走的是随包目录，而不是开发兜底 ----
    from ocr.onnx_engine import ONNXModelLocator, _MODEL_FILENAME

    bundled = os.path.normcase(os.path.join(
        PROJECT_ROOT, manifest["relpath"].replace("/", os.sep), _MODEL_FILENAME))
    for label, fn in (("find_model_path()", ONNXModelLocator.find_model_path),
                      ("find_charset_path()", ONNXModelLocator.find_charset_path)):
        try:
            resolved = os.path.normcase(fn())
        except Exception as e:  # noqa: BLE001
            failures.append("%s 抛错: %s" % (label, e))
            print("  [FAIL] %s -> %s" % (label, e))
            continue
        same_dir = os.path.dirname(resolved) == os.path.dirname(bundled)
        if not same_dir:
            failures.append("%s 解析到 %s，期望随包目录 %s"
                            % (label, resolved, os.path.dirname(bundled)))
        print("  [%s] %s 解析到%s"
              % ("PASS" if same_dir else "FAIL", label,
                 "随包目录" if same_dir else "开发兜底路径（投放缺失？）"))

    print()
    if failures:
        print("失败 %d 项：" % len(failures))
        for item in failures:
            print("  - %s" % item)
        return 1
    print("ONNX 产物完整性校验通过：与 manifest 完全一致。")
    return 0


def sort_names(names):
    """固定顺序：模型文件在前，charset 在后，便于人读。"""
    return sorted(names, key=lambda n: (n.endswith(".txt"), n))


def main() -> int:
    parser = argparse.ArgumentParser(description="ONNX 后端随包产物完整性校验")
    parser.add_argument("--record", action="store_true",
                        help="按当前产物重写 manifest（供应新产物后使用）")
    parser.add_argument("--artifact-dir", default=None,
                        help="产物目录（默认 runtime/onnx/）")
    args = parser.parse_args()

    artifact_dir = args.artifact_dir or os.path.join(PROJECT_ROOT, "runtime", "onnx")

    if args.record:
        if not os.path.isdir(artifact_dir):
            print("产物目录不存在：%s" % artifact_dir)
            return 1
        # --record 不要求 manifest 已存在：首次供应时它还没有。
        manifest = load_manifest() if os.path.isfile(MANIFEST_PATH) else {}
        manifest.setdefault("_comment", [
            "ONNX backend artifact integrity manifest.",
            "runtime/onnx/ is NOT in Git (see .gitignore). This file is, so any",
            "machine or rebuild can prove it was given the same artifacts.",
            "charset.txt is exported by scripts/export_onnx_charset.py.",
            "Recorded by: scripts/verify_onnx_model.py --record",
            "Verified by: scripts/verify_onnx_model.py",
            "Do not hand-edit: regenerate with --record after provisioning.",
        ])
        manifest.setdefault("model_name", "PP-OCRv6_medium_rec")
        manifest.setdefault("relpath", "runtime/onnx")
        present = sort_names([
            n for n in os.listdir(artifact_dir)
            if os.path.isfile(os.path.join(artifact_dir, n))
        ])
        if not present:
            print("产物目录为空：%s" % artifact_dir)
            return 1
        existing = manifest.get("files", {})
        manifest["files"] = {n: existing.get(n, {}) for n in present}
        return record(artifact_dir, manifest)

    if not os.path.isfile(MANIFEST_PATH):
        print("manifest 不存在：%s" % MANIFEST_PATH)
        print("请先运行：python scripts/verify_onnx_model.py --record")
        return 1
    if not os.path.isdir(artifact_dir):
        print("产物目录不存在：%s" % artifact_dir)
        print("请先供应 runtime/onnx/（onnx + charset.txt）。")
        return 1
    return verify(artifact_dir, load_manifest())


if __name__ == "__main__":
    sys.exit(main())
