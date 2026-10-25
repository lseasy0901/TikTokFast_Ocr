# main_window.py 拆分方案(2026-10-25 立项)

## 现状与目标

- 起点:4613 行,187 个方法,上帝类(构建+状态机+接线+事件处理混居)。
- 目标:MainWindow 降到 <1800 行,只保留「部件装配 + 事件处理 + 窗口职责」;
  领域逻辑各归专职模块。
- **硬约束**:测试大量引用 `window._xxx` 属性(onboarding 225/layout/
  settings/toolbar)与模块级私有类名(f79887f 已用原名 re-export 验证可行)
  ——拆分不得改任何对外属性名;每阶段全量回归(onboarding 225/toolbar/
  layout 6/settings/automation 18/watermark)全绿才可提交。

## 分阶段

### Phase 1(已完成,f79887f):模块级私有类外迁
gui/playback_workers.py(_ConnectWorker/_BareResolve*)、gui/frame_bridge.py
(_FrameNotifier/_FrameBridge)、gui/activation_overlay.py(_ActivationOverlay);
main_window 原名 re-export,测试零改动。4613→4234。

### Phase 2:授权/引导/更新协调器(gui/coordinators/)
把三类「外部服务接线」搬出 MainWindow,协调器持有窗口弱引用,UI 触达通过
 MainWindow 暴露的少量回调(而非反向 import):
- LicenseCoordinator:许可证加载/激活/追加/心跳/到期提醒(约 350 行,
  含 _start_license_load/_apply_license_result/_on_license_loaded/
  _on_heartbeat_done 等)
- GuideCoordinator:首次引导/聚光灯/回顾(约 400 行)
- UpdateCoordinator:更新状态机接线(约 100 行)
搬迁模式:方法体整体移动,`self._w.xxx` 访问窗口;MainWindow 保留同名薄
委托方法(兼容测试直调 window._apply_license_result 等)。

### Phase 3:播放状态机(gui/playback.py,最大最难)
连接/断开/裸流解析/failover/帧消费/健康检查(约 1100 行连续块)。
状态字段(_reader/_frame_buffer/_failover/_connected 等)归 PlaybackSession
对象,MainWindow 持 `self._session`;UI 按钮仍直接由 MainWindow 管理。
风险最高:与 B5 验收语义耦合,必须逐段搬迁逐段跑 failover_lifecycle/
frame_ready/b2 三个专项测试。

### Phase 4:清理与收尾
- 解除 _scene_panel 保活依赖:游戏选择改单视图(下拉框),删除隐藏面板
  与 _sync_game_views 的双视图同步(注释里已说明是过渡设计);
- 构建器归位(gui/builders/ 或就地组件化 OcrPanel);
- ruff/行数验收,CLAUDE.md 更新结构图。

## 执行纪律
每阶段独立分支语义(直接在 master 分小提交亦可,本仓库单人节奏),提交信息
标注阶段;任一回归失败即回退该阶段,不带病前进。
