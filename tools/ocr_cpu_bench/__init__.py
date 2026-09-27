# -*- coding: utf-8 -*-
"""LiveLens OCR 架构迁移 Phase 1 —— PP-OCRv6 CPU 可行性验证（完全隔离实验目录）。

本目录**不是**生产链路的一部分：生产 OCR 只认 ocr/ppocr_engine.py，
这里没有任何模块被 ocr/ 或 gui/ import。反过来，本目录只**只读**地
import 生产模块（模型定位、结果取文本、校验/纠正、游戏档），
以便"同一个模型、同一套后处理规则"这两条对比前提是真的。

见 README.md 的 Scope Lock。
"""
