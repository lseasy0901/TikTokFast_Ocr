# -*- coding: utf-8 -*-
"""legacy 测试:Phase 7.2 时代的脚本型验收,保留作历史参考。

这些脚本按当时的目录布局直跑(python tests/legacy/xxx.py,工作目录
license_server/),部分依赖已不存在(如 vendored admin_web)或使用过
新语法。不参与 pytest 收集;现行回归看 tests/(conftest 隔离)与
license_server/pytest.ini。
"""

collect_ignore_glob = ["*.py"]
