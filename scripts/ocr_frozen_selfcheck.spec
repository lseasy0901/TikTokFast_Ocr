# -*- mode: python ; coding: utf-8 -*-
"""
冻结环境 OCR 自检 spec（仅供构建期验证，不随正式包发布）

它不重复任何打包配置：直接 exec 正式的 douyin_viewer.spec，只把 Analysis 的
入口脚本从 main.py 换成 scripts/ocr_frozen_selfcheck.py。

这样得到的 EXE 与正式包共享**同一份** datas / binaries / hiddenimports /
COLLECT 布局，因此：
    "自检能跑" == "正式包里 OCR 能跑"
而"改坏了打包配置导致 PP-OCRv6 模型 / Paddle 运行时没进包"这类问题也会在
自检里暴露 —— 它验证的是生产后端（PPOCREngine）而不是已退役的 Tesseract。

用法：
    python -m PyInstaller --noconfirm --clean \
        --distpath <dist> --workpath <build> scripts/ocr_frozen_selfcheck.spec
"""

import os
import sys

# PyInstaller 用 exec 执行 spec，注入的命名空间里没有 __file__，只能靠 SPECPATH。
_HERE = globals().get("SPECPATH") or globals().get("__file__") or os.path.abspath(".")
PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if not os.path.isfile(os.path.join(PROJECT_ROOT, "douyin_viewer.spec")):
    raise SystemExit(f"定位工程根失败：{PROJECT_ROOT} 下没有 douyin_viewer.spec")

_SELF = os.path.join(PROJECT_ROOT, "scripts", "ocr_frozen_selfcheck.py")

# 正式 spec 用 os.path.abspath(".") 定位工程根，因此必须从工程根执行。
os.chdir(PROJECT_ROOT)

import PyInstaller.building.build_main as _bm

_real_analysis = _bm.Analysis
_real_exe = _bm.EXE

# EXE(name=...) 与 COLLECT(name=...) 必须换成自检自己的名字，否则会在 dist 里
# 覆盖正式包的输出目录，让"自检通过"和"正式包通过"混为一谈。
_SELF_NAME = "OcrSelfCheck"


def _analysis(scripts=None, *args, **kwargs):
    """把入口脚本替换掉，其余参数原样交给正式 spec。"""
    return _real_analysis([_SELF], *args, **kwargs)


class _EXE(_real_exe):
    def __init__(self, *args, **kwargs):
        kwargs["name"] = _SELF_NAME
        # 自检要靠控制台退出码判断成败；正式包仍然是 console=False。
        kwargs["console"] = True
        super().__init__(*args, **kwargs)


class _COLLECT(_bm.COLLECT):
    def __init__(self, *args, **kwargs):
        kwargs["name"] = _SELF_NAME
        super().__init__(*args, **kwargs)


_bm.Analysis = _analysis
_bm.EXE = _EXE
_bm.COLLECT = _COLLECT

# 正式 spec 里 `COLLECT(...)` 是模块级名字，而 EXE/COLLECT 由 PyInstaller 注入到
# spec 的命名空间。这里用同一套注入方式执行正式 spec，只是把两个类换掉。
with open(os.path.join(PROJECT_ROOT, "douyin_viewer.spec"), encoding="utf-8") as f:
    _spec_source = f.read()

exec(compile(_spec_source, "douyin_viewer.spec", "exec"), {
    "SPEC": os.path.join(PROJECT_ROOT, "douyin_viewer.spec"),
    "Analysis": _analysis,
    "PYZ": _bm.PYZ,
    "EXE": _EXE,
    "COLLECT": _COLLECT,
    "__file__": os.path.join(PROJECT_ROOT, "douyin_viewer.spec"),
    "__name__": "__main__",
})
