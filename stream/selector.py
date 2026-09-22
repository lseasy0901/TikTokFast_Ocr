# -*- coding: utf-8 -*-
"""候选流选择：核心侧的"选哪一个"。

设计边界（本阶段刻意保持最小）：
    平台适配器产出的 candidates 已经按**平台自己的偏好**排好序
    （抖音的字段优先级 + 清晰度档位顺序都在适配器里），核心不重排 —— 重排会
    改变现有的选流结果。因此这里当前只做一件事：取第一个候选。

    它独立成模块的意义不是"现在很复杂"，而是把"选哪个"从平台适配器里拿出来，
    这样将来核心侧要改选流策略（例如统一偏好低分辨率、或按带宽自适应）时，
    改的是这里，而不是每个平台的适配器。
"""

from .models import StreamCandidate

__all__ = ["select"]


def select(candidates) -> StreamCandidate | None:
    """从候选列表里选出要播放的那一个。

    当前策略：取第一个非空 url 的候选（= 平台给出的最高优先级）。
    """
    for c in candidates or ():
        if c and c.url:
            return c
    return None
