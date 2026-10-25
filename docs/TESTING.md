# 测试索引(2026-10-25)

两类范式并存,**运行方式不同**;CI(.github/workflows/ci.yml)只跑 pytest 型。

## pytest 型(框架断言,可进 CI)

| 文件 | 覆盖 |
|---|---|
| tests/automation/test_join_unit.py | 自动加入单元边界 18 项(配置/热键/前置/防重入/UIPI) |
| tests/test_clipboard.py | 剪贴板管理(含 force 语义) |
| tests/test_copy_log_export.py | 复制流水导出(自研风格但可收集;pytest 下显式 skip) |
| tests/test_phase_ui3_activation_settings.py | 激活弹窗/设置中心 5 项 |
| tests/test_phase_ui3_1_layout.py | 布局合同 6 项(含地址栏现行架构) |
| license_server/tests/test_stale_active_renewal.py | 授权 stale-ACTIVE 回归 3 项 |
| license_server/tests/test_rate_limit.py | 速率限制/登录锁定 4 项 |
| license_server/tests/{test_admin2,test_server,test_simple}.py | 服务端接口/Admin2(需完整服务端依赖,含 vendored admin_web) |

## 自研脚本型(直接运行,退出码 0/1)

| 文件 | 说明 |
|---|---|
| tests/test_first_run_onboarding.py | 首次引导全流程 225 项(UI 大盘回归主力) |
| tests/ui_redesign/test_toolbar_contract.py | 标题栏/溢出菜单/接线合同 |
| tests/ui_redesign/test_watermark.py | 水印生命周期/渲染/隔离 |
| tests/automation/input_boundary_live.py | 真实输入边界 15 项(**需真实桌面**,验收时一次性运行) |
| 其余 tests/test_phase_*.py | 各历史阶段验收脚本(按需直跑) |

## 明确跳过/过时(不修复即跳)

| 文件 | 状态 |
|---|---|
| tests/test_ocr_worker.py | 断言已移除的 worker 内置剪贴板架构,模块级 skip 待重写 |
| tests/test_copy_log_export.py(3/4) | GUI 场景需脚本自建 window,pytest 下 skip |
| license_server/tests/legacy/ | Phase 7.2 时代脚本存档,conftest 隔离收集 |
| tests/test_ui_internal_diagnostics_gate.py | 基线既存失败(MainWindow._stream_history 旧属性),待同批清理 |

## 基准与工具

- scripts/benchmarks/(直跑,多数需真实流环境)
- scripts/capture_ui_layout.py:界面截图(offscreen/--native)
