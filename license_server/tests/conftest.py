# -*- coding: utf-8 -*-
"""
pytest 全局配置（Phase 2 阶段 E）。

在**任何 app 模块导入之前**把 DATABASE_URL 指向一次性临时库，
使全部测试（含既有 tests/）运行在与生产、与本地开发库完全隔离的
空数据库上；每次 pytest 进程启动时重建。
"""

import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="livelens-license-test-")
_DB_PATH = os.path.join(_TMPDIR, "test_license_server.db").replace("\\", "/")

# 环境变量优先级高于 license_server/.env（pydantic-settings 约定）
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH
os.environ.setdefault("DEBUG", "false")
