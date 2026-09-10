# Douyin Low Latency Viewer

抖音低延迟直播流查看器

## 项目目标

实现最低延迟的抖音（Douyin）直播流显示，为后续扩展功能（OCR、AI、剪贴板、ROI 等）提供稳定、高性能的视频显示底座。

## 运行环境

- Windows 10 / Windows 11
- Python 3.11

## 开发阶段说明

### Phase 1（当前阶段）

目标：实现最低延迟抖音直播流显示。

- 直播流地址获取（抖音）
- FFmpeg 低延迟解码读取
- PySide6 视频显示窗口
- 帧缓冲队列
- 性能监控

### 暂不包含（Phase 1 明确排除）

- OCR
- AI
- 剪贴板
- ROI
- 其他业务功能

后续阶段将在此基础上逐步加入上述功能。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行程序（当前仅为入口占位）
python main.py
```

## 目录结构

```
DouyinLowLatencyViewer
├── main.py               # 启动入口
├── requirements.txt      # 依赖清单
├── config.yaml           # 全局配置
├── README.md
├── gui/                  # GUI 界面模块
│   ├── main_window.py    # 主窗口
│   └── video_widget.py   # 视频显示控件
├── stream/               # 直播流模块
│   ├── douyin.py         # 抖音流获取
│   ├── ffmpeg_reader.py  # FFmpeg 解码读取
│   └── frame_buffer.py   # 帧缓冲队列
└── utils/                # 工具模块
    ├── logger.py         # 日志
    └── performance.py    # 性能监控
```

## 当前状态

Phase 1 - Step 1 已完成：仅创建项目基础结构，无任何业务功能实现。
