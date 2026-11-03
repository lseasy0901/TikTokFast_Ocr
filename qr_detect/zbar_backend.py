# -*- coding: utf-8 -*-
"""zbar ctypes 后端。

从「闪-live_player」(DouyinQR) 逆向还原的 zbar 用法：
  - 扫描器创建时: set_config(ZBAR_NONE, CFG_ENABLE, 0)      关闭全部码制
                  set_config(ZBAR_QRCODE, CFG_ENABLE, 1)    只开二维码
  - 每次扫描前:   set_config(ZBAR_QRCODE, CFG_X/Y_DENSITY, n) 调扫描线密度
  - 图像格式:     Y800 (8bit 灰度)
  - 结果:         只要 ZBAR_QRCODE 符号, 取 data + 定位多边形

zbar-0.dll / iconv-2.dll 复制自原程序目录（zbar 为 LGPL-2.1，以独立 DLL
形式动态链接并保留版权声明即可）。
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from typing import List

# zbar 常量（zbar.h）
ZBAR_NONE = 0        # 全部码制
ZBAR_QRCODE = 0x40   # 只关心二维码
ZBAR_CFG_ENABLE = 0
ZBAR_CFG_X_DENSITY = 0x100
ZBAR_CFG_Y_DENSITY = 0x101

FOURCC_Y800 = 0x30303859  # 'Y800' 小端

_BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")


def _load_zbar() -> ctypes.CDLL:
    """按优先级加载 zbar DLL：环境变量指定 → 模块 bin/ → pyzbar 自带 → 系统。"""
    candidates: List[str] = []
    env = os.environ.get("QR_DETECT_ZBAR_DLL")
    if env:
        candidates.append(env)
    for name in ("zbar-0.dll", "libzbar-64.dll", "libzbar.dll"):
        candidates.append(os.path.join(_BIN_DIR, name))
    try:
        import pyzbar  # noqa: F401
        import pyzbar.pyzbar as _p
        # pyzbar wheel 把 DLL 装在包目录
        pkg = os.path.dirname(os.path.abspath(_p.__file__))
        for name in ("libzbar-64.dll", "libzbar.dll", "zbar-0.dll"):
            candidates.append(os.path.join(pkg, name))
    except Exception:
        pass
    candidates.extend(["zbar-0.dll", "libzbar-64.dll"])  # 让系统搜索路径兜底

    errors = []
    for path in candidates:
        try:
            # iconv-2.dll 与 zbar 同目录时需要显式加载，避免依赖系统搜索
            sibling = os.path.join(os.path.dirname(path) or ".", "iconv-2.dll")
            if os.path.isfile(sibling):
                try:
                    ctypes.CDLL(sibling)
                except OSError:
                    pass
            return ctypes.CDLL(path)
        except OSError as e:
            errors.append(f"{path}: {e}")
    raise OSError("找不到可用的 zbar DLL，尝试过:\n  " + "\n  ".join(errors))


@dataclass
class ZbarSymbol:
    """一个解码出的二维码符号。"""
    data: bytes                       # 原始字节（UTF-8 居多）
    polygon: List[tuple] = field(default_factory=list)  # 定位多边形 [(x, y), ...] 像素坐标


class ZbarScanner:
    """可复用的 zbar 扫描器（对应原程序的 scanner + image 生命周期）。"""

    def __init__(self) -> None:
        self._lib = _load_zbar()
        lib = self._lib
        lib.zbar_image_scanner_create.restype = ctypes.c_void_p
        lib.zbar_image_scanner_create.argtypes = []
        lib.zbar_image_scanner_destroy.argtypes = [ctypes.c_void_p]
        lib.zbar_image_scanner_set_config.restype = ctypes.c_int
        lib.zbar_image_scanner_set_config.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.zbar_image_create.restype = ctypes.c_void_p
        lib.zbar_image_create.argtypes = []
        lib.zbar_image_destroy.argtypes = [ctypes.c_void_p]
        lib.zbar_image_set_format.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.zbar_image_set_size.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        lib.zbar_image_set_data.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        lib.zbar_scan_image.restype = ctypes.c_int
        lib.zbar_scan_image.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.zbar_image_first_symbol.restype = ctypes.c_void_p
        lib.zbar_image_first_symbol.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_next.restype = ctypes.c_void_p
        lib.zbar_symbol_next.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_type.restype = ctypes.c_int
        lib.zbar_symbol_get_type.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_data.restype = ctypes.c_char_p
        lib.zbar_symbol_get_data.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_data_length.restype = ctypes.c_int
        lib.zbar_symbol_get_data_length.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_loc_size.restype = ctypes.c_int
        lib.zbar_symbol_get_loc_size.argtypes = [ctypes.c_void_p]
        lib.zbar_symbol_get_loc_x.restype = ctypes.c_int
        lib.zbar_symbol_get_loc_x.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        lib.zbar_symbol_get_loc_y.restype = ctypes.c_int
        lib.zbar_symbol_get_loc_y.argtypes = [ctypes.c_void_p, ctypes.c_uint]

        self._scanner = lib.zbar_image_scanner_create()
        if not self._scanner:
            raise RuntimeError("zbar_image_scanner_create 失败")
        self._image = lib.zbar_image_create()
        if not self._image:
            lib.zbar_image_scanner_destroy(self._scanner)
            raise RuntimeError("zbar_image_create 失败")

        # 与原程序一致：关全部码制，只开 QRCODE
        lib.zbar_image_scanner_set_config(self._scanner, ZBAR_NONE, ZBAR_CFG_ENABLE, 0)
        lib.zbar_image_scanner_set_config(self._scanner, ZBAR_QRCODE, ZBAR_CFG_ENABLE, 1)

    # ------------------------------------------------------------------
    def set_density(self, x: int, y: int) -> None:
        """设置扫描线密度（原程序三档：3,3 / 5,5 / 4,4）。"""
        lib = self._lib
        lib.zbar_image_scanner_set_config(self._scanner, ZBAR_QRCODE, ZBAR_CFG_X_DENSITY, int(x))
        lib.zbar_image_scanner_set_config(self._scanner, ZBAR_QRCODE, ZBAR_CFG_Y_DENSITY, int(y))

    def scan(self, gray, width: int, height: int) -> List[ZbarSymbol]:
        """扫描一帧 Y800 灰度图。gray 可为 numpy uint8 数组或 bytes。

        zbar_image_set_data 只保存指针，调用返回前 gray 必须保持有效。
        """
        import numpy as np
        lib = self._lib
        buf = np.ascontiguousarray(gray, dtype=np.uint8)
        if buf.size < width * height:
            raise ValueError(f"灰度缓冲过小: {buf.size} < {width * height}")

        lib.zbar_image_set_format(self._image, FOURCC_Y800)
        lib.zbar_image_set_size(self._image, width, height)
        lib.zbar_image_set_data(
            self._image, buf.ctypes.data_as(ctypes.c_void_p), width * height, None
        )
        try:
            lib.zbar_scan_image(self._scanner, self._image)
            return self._collect_symbols()
        finally:
            # 解除数据引用，避免悬垂指针
            lib.zbar_image_set_data(self._image, None, 0, None)

    def _collect_symbols(self) -> List[ZbarSymbol]:
        lib = self._lib
        out: List[ZbarSymbol] = []
        sym = lib.zbar_image_first_symbol(self._image)
        while sym:
            if lib.zbar_symbol_get_type(sym) == ZBAR_QRCODE:
                length = lib.zbar_symbol_get_data_length(sym)
                raw = lib.zbar_symbol_get_data(sym)
                if raw and length > 0:
                    data = ctypes.string_at(raw, length)
                    n = lib.zbar_symbol_get_loc_size(sym)
                    poly = []
                    for i in range(max(n, 0)):
                        poly.append((lib.zbar_symbol_get_loc_x(sym, i),
                                     lib.zbar_symbol_get_loc_y(sym, i)))
                    out.append(ZbarSymbol(data=data, polygon=poly))
            sym = lib.zbar_symbol_next(sym)
        return out

    def close(self) -> None:
        if getattr(self, "_image", None):
            self._lib.zbar_image_destroy(self._image)
            self._image = None
        if getattr(self, "_scanner", None):
            self._lib.zbar_image_scanner_destroy(self._scanner)
            self._scanner = None

    def __del__(self):  # pragma: no cover - 解释器退出时机不可控
        try:
            self.close()
        except Exception:
            pass
