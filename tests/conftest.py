# -*- coding: utf-8 -*-
"""tests/ 共享夹具。

2026-10-25 结构整理:根目录 67 个测试文件移入 tests/ 后,pytest 从仓库
根运行时由本 conftest 把项目根加入 sys.path(等价于此前"测试文件与包
同层"的隐式可导入性)。

注意:本目录混有 pytest 型与自研脚本型(main+check 直跑)测试;后者直接
``python tests/xxx.py`` 运行,conftest 不参与——文件内自带的
sys.path 处理已随迁移更新为指向项目根。
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
