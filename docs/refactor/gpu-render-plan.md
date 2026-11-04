# GPU 零拷贝渲染立项方案(2026-11-05)

> 状态:**立项待排期**。本文档是 main_window 拆分(docs/refactor/
> main_window-split-plan.md)之后下一个架构级工程的施工图。
> 预算:两周(Phase A 原型 3 天 → Phase B 实现 5 天 → Phase C 切换与回归 2 天)。
> 前置:PlaybackUI 操作面已收口(28983cb),渲染路径只剩
> video_widget.py 单点——这是本工程可行的前提。

## 1. 为什么(动机与量化)

- 现状:FFmpeg 子进程解出 BGR24 → 管道(~190MB/s @1080p60)→ Python
  numpy → GUI 主线程 QImage/QPixmap → 主线程 paint。**帧的缩放/上屏
  全部发生在 GUI 主线程栅格引擎里**,这是:
  - 主线程满载的直接来源(校准点击轮询漏检的根因,2026-10-24 实测);
  - 1440p/4K 直播普及后延迟与流畅度劣化的结构性天花板。
- GPU 方案收益:帧缩放/上屏走 GPU,主线程只剩"换纹理引用";
  CPU 解码占用预计持平,主线程占用预计 -10~20pp(以
  tests/automation/ui_baseline_probe.py 既有探针验收,不拍脑袋)。

## 2. 硬约束(不因重构破坏)

- LatestFrameBuffer 语义不变(最新帧、丢旧不攒);
- set_frame/clear_frame/visible_video_rect/clear_roi/set_qr_highlight
  对外 API 不变——ROI 叠加层、水印、QR 高亮、OCR 取帧全部原样;
- 帧仍是 BGR24 numpy(CPU 侧),**不做** FFmpeg 管道协议改造
  (那是下一立项);本工程只动"CPU 帧 → 屏幕"这一段;
- tests/test_phase_b2_frame_ready.py 41 项与 capture_ui_layout 截图
  全绿;回归主力 onboarding 225 照跑。

## 3. 分阶段

### Phase A(3 天):QOpenGLWidget 原型,特性门控
- VideoWidget 保持 QWidget 外壳,内部换 GLVideoSurface(QOpenGLWidget);
- paintGL 里用 PBO 上传 BGR 纹理 + 双采样 shader;
- `DLV_RENDERER=gl|raster` 环境变量切换,默认 raster(零风险);
- 验收:b2 专项 41 项在两种 renderer 下全过;ui_baseline_probe
  主线程占用对比数据出炉。

### Phase B(5 天):功能补齐 + 切默认
- 窗口缩放时的纹理重采样、ROI/水印叠加层与 GL 表面合成方式验证;
- CPU 占位/延迟 A/B(与 raster 对照,基线=Phase A 数据);
- 达标(主线程占用不劣化、专项全过)→ 默认切 gl,raster 保留为回退。

### Phase C(2 天):收尾
- capture_ui_layout/打包 spec 验证;文档;删除特性门控的过渡代码。

## 4. 风险与回退

- 驱动兼容性(GL 上下文失败)→ 启动探测失败自动回退 raster
  (与 Acrylic 的回退纪律同一套);
- Overlay 合成(ROI/水印在 GL 表面之上的透明层)是最大技术风险点,
  Phase A 原型必须先验证这一点,不通过则整个立项降级重评;
- 回退开关常驻:DLV_RENDERER=raster 一行恢复旧行为。

## 5. 非目标

- FFmpeg 管道协议改造(共享内存/原生解码绑定)——本工程后另行立项;
- HDR/色彩管理——当前直播源不支持。
