# -*- coding: utf-8 -*-
"""二维码重生成后端（qrencode.dll ctypes 绑定）。

「放大」的本质在原程序里就是：把直播画面里解码出的内容文本，用
qrencode 重新生成一张干净的大二维码（ECC 级别 H，版本自适应），
三级编码回退与原程序 0x140014b80 一致：

  1. QRcode_encodeString8bit(utf8, 0, QR_ECLEVEL_H, 1)
  2. QRcode_encodeString(utf8, 0, QR_ECLEVEL_H, QR_MODE_AN, 1)
  3. QRcode_encodeData(size, bytes, 0, QR_ECLEVEL_H)

qrencode.dll 复制自原程序目录（qrencode 为 LGPL，动态链接 + 保留声明）。
"""
from __future__ import annotations

import ctypes
import os
from typing import Optional

QR_ECLEVEL_L = 0
QR_ECLEVEL_M = 1
QR_ECLEVEL_Q = 2
QR_ECLEVEL_H = 3  # 原程序全部用 H

QR_MODE_NUL = 0
QR_MODE_NUM = 1
QR_MODE_AN = 2  # 字母数字
QR_MODE_8 = 3
QR_MODE_KANJI = 4

_BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")


class QRcodeStruct(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("width", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _load_qrencode() -> ctypes.CDLL:
    candidates = []
    env = os.environ.get("QR_DETECT_QRENCODE_DLL")
    if env:
        candidates.append(env)
    candidates.append(os.path.join(_BIN_DIR, "qrencode.dll"))
    candidates.append("qrencode.dll")  # 系统路径兜底
    errors = []
    for path in candidates:
        try:
            return ctypes.CDLL(path)
        except OSError as e:
            errors.append(f"{path}: {e}")
    raise OSError("找不到可用的 qrencode DLL:\n  " + "\n  ".join(errors))


def encode_qr_matrix(content: bytes, ecc_level: int = QR_ECLEVEL_H) -> Optional["tuple[int, list[int]]"]:
    """把内容编码为二维码矩阵。

    返回 (version, matrix)；matrix 是 width*width 的一维列表，
    1 = 黑模块，0 = 白模块（qrencode 原始 data 每字节低 1 位即模块颜色）。
    全部回退失败时返回 None（对应原程序的「生成二维码失败」分支）。
    """
    lib = _load_qrencode()
    lib.QRcode_free.argtypes = [ctypes.POINTER(QRcodeStruct)]
    code_ptr = None

    # 回退 1：8bit 模式（原版第 4 参 caseSensitive=1）
    lib.QRcode_encodeString8bit.restype = ctypes.POINTER(QRcodeStruct)
    lib.QRcode_encodeString8bit.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    try:
        code_ptr = lib.QRcode_encodeString8bit(content, 0, ecc_level, 1)
    except Exception:
        code_ptr = None

    # 回退 2：字符串模式（AN hint）
    if not code_ptr:
        lib.QRcode_encodeString.restype = ctypes.POINTER(QRcodeStruct)
        lib.QRcode_encodeString.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        try:
            code_ptr = lib.QRcode_encodeString(content, 0, ecc_level, QR_MODE_AN, 1)
        except Exception:
            code_ptr = None

    # 回退 3：原始字节
    if not code_ptr:
        lib.QRcode_encodeData.restype = ctypes.POINTER(QRcodeStruct)
        lib.QRcode_encodeData.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        try:
            code_ptr = lib.QRcode_encodeData(len(content), content, 0, ecc_level)
        except Exception:
            code_ptr = None

    if not code_ptr:
        return None

    try:
        code = code_ptr[0]
        width = code.width
        n = width * width
        raw = ctypes.string_at(code.data, n)
        matrix = [1 if (b & 1) else 0 for b in raw]
        return code.version, matrix
    finally:
        lib.QRcode_free(code_ptr)


def matrix_to_image(matrix: list[int], width_modules: int = None,
                    target_px: int = 500, quiet_zone: int = 4,
                    dark=(15, 23, 42), light=(255, 255, 255)):
    """把模块矩阵渲染成 PIL Image。

    target_px 对应原程序 QrImageWidget 的 int(dpr*500+0.5)（1x 屏即 500px）。
    quiet_zone 默认 4 模块（QR 规范建议值）。
    返回 (PIL.Image, 模块宽度含静区)。
    """
    from PIL import Image, ImageDraw
    w = width_modules or _infer_width(matrix)
    total = w + quiet_zone * 2
    # 先画成 total×total 的单位图，再用最近邻放大到目标尺寸（整数倍缩放不糊）
    img = Image.new("RGB", (total, total), light)
    draw = ImageDraw.Draw(img)
    for y in range(w):
        row = matrix[y * w:(y + 1) * w]
        for x, v in enumerate(row):
            if v:
                draw.rectangle(
                    [(x + quiet_zone, y + quiet_zone),
                     (x + quiet_zone, y + quiet_zone)],
                    fill=dark,
                )
    scale = max(target_px // total, 1)
    img = img.resize((total * scale, total * scale), Image.NEAREST)
    return img, total


def _infer_width(matrix: list[int]) -> int:
    n = len(matrix)
    w = int(round(n ** 0.5))
    if w * w != n:
        raise ValueError(f"矩阵不是正方形: {n}")
    return w
