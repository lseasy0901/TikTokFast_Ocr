# -*- coding: utf-8 -*-
"""构建/转换期工具：从 PP-OCRv6 inference.yml 导出 ONNX 后端的 charset.txt。

为什么需要这一步
----------------
ONNX 后端的运行时**只允许**读取 `runtime/onnx/charset.txt`，不得读取
Paddle 侧的 `runtime/ppocr/PP-OCRv6_medium_rec_infer/inference.yml`。
否则 ONNX 后端就永远绑死在 Paddle 的投放目录上，将来撤掉 Paddle 时会一起坏掉。

因此字典在**构建/转换阶段**被摘出来，作为 ONNX 产物的一部分独立投放：

    runtime/onnx/
      PP-OCRv6_medium_rec_fp32.onnx
      charset.txt                    <- 本脚本生成，一字符一行

导出的是什么
------------
`inference.yml` 里 `PostProcess.character_dict` 的**原始字典项**（18,708 项），
**不含 blank、不含空格**。PaddleX 的 `use_space_char`（末尾追加空格）与
`add_special_char`（头部插入 "blank"）两步仍由运行时
`ocr/onnx_preprocess.CTCLabelDecodeMirror` 按既有语义完成 —— 这样该类
与 Step 0 验证过的版本保持**逐字节相同**，索引映射也就逐项一致。

因此：
    charset.txt        18,708 行
    运行时最终字符表    18,710 项（blank + 18,708 + space）

格式契约（`ocr/onnx_preprocess.load_charset` 依赖它）
----------------------------------------------------
一字符一行、UTF-8、**LF 换行**、末尾一个换行、**不做任何 strip**。
写之前本脚本会硬校验源字典确实满足「每项都是单个字符、无空串、无空白字符、
无重复」，不满足就直接失败 —— 因为任何一项含换行都会让行数与索引错位，
而那会**静默**地把每一次识别结果都解错。

用法
----
    python scripts/export_onnx_charset.py
    python scripts/export_onnx_charset.py --out runtime/onnx/charset.txt
    python scripts/export_onnx_charset.py --inference-yml <path>

本脚本是构建期工具，**允许**读 Paddle 侧 yml；它不参与运行时。
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)

DEFAULT_INFERENCE_YML = os.path.join(
    PROJECT_ROOT, "runtime", "ppocr", "PP-OCRv6_medium_rec_infer", "inference.yml"
)
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "runtime", "onnx", "charset.txt")


def load_character_dict(inference_yml_path: str):
    """从 inference.yml 读 PostProcess.character_dict（原始项，不做增补）。"""
    import yaml

    with open(inference_yml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    try:
        chars = config["PostProcess"]["character_dict"]
    except (KeyError, TypeError) as e:
        raise SystemExit(
            "inference.yml 里找不到 PostProcess.character_dict: %s (%s)"
            % (inference_yml_path, e)
        )
    if not isinstance(chars, list):
        raise SystemExit(
            "character_dict 不是列表: %r" % type(chars)
        )
    return [str(c) for c in chars]


def validate(chars):
    """校验源字典能满足「一字符一行」的无损往返。不满足就硬失败。"""
    problems = []
    if not chars:
        problems.append("字典为空")
    bad_len = [i for i, c in enumerate(chars) if len(c) != 1]
    if bad_len:
        problems.append(
            "有 %d 项不是单字符（前 5 个索引: %s）"
            % (len(bad_len), bad_len[:5])
        )
    for label, pred in (
        ("空串", lambda c: c == ""),
        ("含 \\n", lambda c: "\n" in c),
        ("含 \\r", lambda c: "\r" in c),
        ("含 \\t", lambda c: "\t" in c),
    ):
        n = sum(1 for c in chars if pred(c))
        if n:
            problems.append("有 %d 项%s" % (n, label))
    dups = len(chars) - len(set(chars))
    if dups:
        problems.append("有 %d 个重复项" % dups)
    if problems:
        raise SystemExit(
            "源字典不满足「一字符一行」的无损格式，拒绝导出：\n  - "
            + "\n  - ".join(problems)
        )


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inference-yml", default=DEFAULT_INFERENCE_YML,
                    help="源 PP-OCRv6 inference.yml")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="写出的 charset.txt（默认 runtime/onnx/charset.txt）")
    args = ap.parse_args()

    if not os.path.isfile(args.inference_yml):
        print("源 inference.yml 不存在: %s" % args.inference_yml)
        return 1

    chars = load_character_dict(args.inference_yml)
    validate(chars)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # newline="\n" 强制 LF：CRLF 会让运行时每一项尾部多一个 \r，
    # 静默污染整个索引映射（load_charset 会拒绝，但更该在写的时候就避免）。
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for c in chars:
            f.write(c + "\n")

    # 往返自检：读回来必须与源字典逐项相同。
    with open(args.out, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    if raw.endswith("\n"):
        raw = raw[:-1]
    round_trip = raw.split("\n")
    if round_trip != chars:
        print("往返自检失败：写出的文件与源字典不一致")
        return 1

    digest = sha256_of(args.out)
    info = {
        "source_inference_yml": args.inference_yml,
        "charset_path": args.out,
        "charset_entries": len(chars),
        "charset_bytes": os.path.getsize(args.out),
        "charset_sha256": digest,
        "final_charset_size": len(chars) + 2,  # + blank + space，运行时增补
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("\nOK: charset.txt 已导出（一字符一行，LF，末尾换行，未做 strip）")
    print("提醒：ocr/onnx_preprocess.CHARSET_ENTRIES 必须等于 %d；"
          "不等就改它，否则运行时会硬失败。" % len(chars))
    return 0


if __name__ == "__main__":
    sys.exit(main())
