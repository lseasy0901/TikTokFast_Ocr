# -*- coding: utf-8 -*-
"""
去重（Phase: Dataset Generator）。

两级：
    1. **SHA-256** —— 完全相同文件的判定。本阶段就完整实现并写进 manifest。
    2. **感知哈希（dHash）** —— 近重复图片的判定。本阶段把接口与实现都做出来，
       但因为还没有真实图片，它只在测试里用合成数组验证。

与配额统计的关系（重要）
    去重**只报告、不改配额**。任何"删掉重复样本"的动作都会让 sample_count
    少于 samples_per_code，从而与 dataset.json 里声明的配额矛盾。因此：
      * 码级去重在生成阶段就保证（同一格式内不重复）；
      * 文件级去重只产出报告，写进 dataset.json 的 dedupe 段。
    要真正裁剪，必须由调用方显式决定并同步更新配额。

依赖：只依赖标准库 + pillow（项目 requirements.txt 已声明 pillow）。
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Sequence, Tuple

__all__ = [
    "sha256_file",
    "sha256_bytes",
    "dhash_from_array",
    "perceptual_hash",
    "hamming_distance",
    "find_exact_duplicates",
    "find_near_duplicates",
    "DEFAULT_HASH_SIZE",
]

DEFAULT_HASH_SIZE = 8      # 8x8 → 64 bit


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """分块计算文件 SHA-256（大文件也不吃内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ----------------------------------------------------------------------
# 感知哈希（近重复）
# ----------------------------------------------------------------------
def dhash_from_array(gray, hash_size: int = DEFAULT_HASH_SIZE) -> str:
    """dHash：缩到 (hash_size+1) x hash_size，逐行比较相邻像素灰度。

    参数：
        gray: 2-D uint8 数组（或任何可转成 'L' 的图像数组）
    返回：
        十六进制字符串，长度 hash_size*2 个字符（8 → 16 字符 = 64 bit）
    """
    from PIL import Image
    import numpy as np

    img = Image.fromarray(gray).convert("L").resize(
        (hash_size + 1, hash_size), Image.LANCZOS)
    arr = np.asarray(img, dtype="int16")
    diff = arr[:, 1:] > arr[:, :-1]          # 每行 hash_size 位
    bits = "".join("1" if b else "0" for b in diff.flatten())
    # 宽度是 size*size 位对应的十六进制位数（8 -> 16 字符）。写成 hash_size*2//4
    # 会把宽度当成**下限**：纯色图的哈希是 0，会产出只有 4 个字符的串，
    # 与其它哈希长度不一致（hamming_distance 会报长度不等）。
    return "%0*x" % (hash_size * hash_size // 4, int(bits, 2))


def perceptual_hash(path: str, hash_size: int = DEFAULT_HASH_SIZE) -> str:
    """对图片文件求 dHash。文件不存在/无法解码时抛 OSError。"""
    from PIL import Image
    import numpy as np

    with Image.open(path) as im:
        gray = np.asarray(im.convert("L"))
    return dhash_from_array(gray, hash_size)


def hamming_distance(a: str, b: str) -> int:
    if len(a) != len(b):
        raise ValueError("两个哈希长度不一致: %d vs %d" % (len(a), len(b)))
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ----------------------------------------------------------------------
# 分组
# ----------------------------------------------------------------------
def find_exact_duplicates(paths: Sequence[str]) -> Dict[str, List[str]]:
    """按 SHA-256 分组，只返回出现次数 > 1 的组。"""
    seen: Dict[str, List[str]] = {}
    for p in paths:
        seen.setdefault(sha256_file(p), []).append(p)
    return {h: v for h, v in seen.items() if len(v) > 1}


def find_near_duplicates(paths: Sequence[str],
                         max_distance: int = 5,
                         hash_size: int = DEFAULT_HASH_SIZE
                         ) -> List[Tuple[str, str, int]]:
    """两两比较 dHash，返回海明距离 <= max_distance 的对。

    O(n^2)，只适用于本阶段的几千张量级；更大规模需要分桶（预留，不本阶段做）。
    """
    hashes = [(p, perceptual_hash(p, hash_size)) for p in paths]
    out: List[Tuple[str, str, int]] = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            d = hamming_distance(hashes[i][1], hashes[j][1])
            if d <= max_distance:
                out.append((hashes[i][0], hashes[j][0], d))
    return out
