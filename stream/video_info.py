# -*- coding: utf-8 -*-
"""
流媒体分辨率检测模块

使用 ffprobe 检测视频流的实际分辨率、像素格式、帧率等信息。

功能：
    - 检测视频流的实际 width/height
    - 检测像素格式 (pix_fmt)
    - 检测帧率 (fps)
    - 支持 flv/hls 等多种流格式
"""

import json
import logging
import subprocess
import time

logger = logging.getLogger("DouyinLowLatencyViewer.stream.video_info")

__all__ = ["get_video_info", "VideoInfo"]


class VideoInfo:
    """视频信息数据类"""

    def __init__(self, width: int = None, height: int = None,
                 pix_fmt: str = None, fps: float = None,
                 codec_name: str = None):
        self.width = width
        self.height = height
        self.pix_fmt = pix_fmt
        self.fps = fps
        self.codec_name = codec_name

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "width": self.width,
            "height": self.height,
            "pix_fmt": self.pix_fmt,
            "fps": self.fps,
            "codec_name": self.codec_name,
        }

    def __str__(self) -> str:
        """格式化输出"""
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.pix_fmt:
            parts.append(f"pix_fmt={self.pix_fmt}")
        if self.fps:
            parts.append(f"fps={self.fps:.2f}")
        if self.codec_name:
            parts.append(f"codec={self.codec_name}")
        return ", ".join(parts) if parts else "Unknown"


def get_video_info(stream_url: str, timeout: int = 10) -> VideoInfo:
    """
    使用 ffprobe 检测视频流信息

    参数：
        stream_url: 流媒体 URL
        timeout: 超时时间（秒）

    返回：
        VideoInfo 对象，检测失败返回默认值

    异常：
        不抛出异常，检测失败时返回默认值
    """
    logger.info("[VIDEO INFO] 开始检测流分辨率: %s", stream_url[:60] + "...")

    start_time = time.perf_counter()

    try:
        # 构建 ffprobe 命令
        cmd = [
            "ffprobe",
            "-hide_banner",
            "-loglevel", "error",  # 只显示错误
            "-show_entries", "stream=width,height,pix_fmt,codec_name,r_frame_rate",
            "-select_streams", "v:0",  # 只检测视频流
            "-of", "json",  # JSON 格式输出
            stream_url
        ]

        logger.debug("[VIDEO INFO] 执行命令: %s", " ".join(cmd))

        # 执行 ffprobe
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0
        )

        elapsed = time.perf_counter() - start_time

        # 检查返回码
        if process.returncode != 0:
            logger.warning(
                "[VIDEO INFO] ffprobe 返回错误 (code=%d): %s",
                process.returncode, process.stderr.strip()[:200]
            )
            return VideoInfo()

        # 解析 JSON 输出
        try:
            data = json.loads(process.stdout)
        except json.JSONDecodeError as e:
            logger.warning("[VIDEO INFO] JSON 解析失败: %s", e)
            return VideoInfo()

        # 提取视频流信息
        streams = data.get("streams", [])
        if not streams:
            logger.warning("[VIDEO INFO] 未找到视频流")
            return VideoInfo()

        video_stream = streams[0]

        # 提取分辨率
        width = video_stream.get("width")
        height = video_stream.get("height")

        # 提取像素格式
        pix_fmt = video_stream.get("pix_fmt")

        # 提取编码格式
        codec_name = video_stream.get("codec_name")

        # 提取帧率
        fps = None
        r_frame_rate = video_stream.get("r_frame_rate")
        if r_frame_rate:
            try:
                num, den = r_frame_rate.split("/")
                fps = float(num) / float(den) if den else 0
            except (ValueError, ZeroDivisionError):
                pass

        info = VideoInfo(
            width=width,
            height=height,
            pix_fmt=pix_fmt,
            fps=fps,
            codec_name=codec_name
        )

        logger.info(
            "[VIDEO INFO] 检测完成 (%.2fs): %s",
            elapsed, info
        )

        return info

    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start_time
        logger.warning("[VIDEO INFO] ffprobe 超时 (%.2fs)", elapsed)
        return VideoInfo()

    except FileNotFoundError:
        logger.warning("[VIDEO INFO] ffprobe 未找到，请安装 FFmpeg")
        return VideoInfo()

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.warning("[VIDEO INFO] 检测失败 (%.2fs): %s", elapsed, e)
        return VideoInfo()


def detect_resolution_fallback(stream_url: str) -> tuple:
    """
    备用分辨率检测方案（使用 ffmpeg 读取首帧）

    当 ffprobe 不可用时使用，通过读取首帧获取分辨率。

    参数：
        stream_url: 流媒体 URL

    返回：
        (width, height) 元组，检测失败返回 (1920, 1080)
    """
    logger.info("[VIDEO INFO] 尝试备用分辨率检测...")

    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", stream_url,
            "-vframes", "1",  # 只读取1帧
            "-f", "null", "-"
        ]

        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0
        )

        # 从错误输出中解析分辨率信息
        stderr = process.stderr

        # 匹配分辨率模式: 1920x1080
        import re
        resolution_pattern = re.compile(r'(\d{3,4})x(\d{3,4})')
        matches = resolution_pattern.findall(stderr)

        if matches:
            # 取第一个匹配的分辨率
            width, height = int(matches[0][0]), int(matches[0][1])
            logger.info("[VIDEO INFO] 备用检测成功: %dx%d", width, height)
            return (width, height)

        logger.warning("[VIDEO INFO] 备用检测未找到分辨率信息")
        return (1920, 1080)

    except Exception as e:
        logger.warning("[VIDEO INFO] 备用检测失败: %s", e)
        return (1920, 1080)


if __name__ == "__main__":
    # 测试入口
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    test_url = input("请输入测试流URL（留空使用默认测试）: ").strip()
    if not test_url:
        test_url = "https://example.com/test.flv"

    info = get_video_info(test_url)
    print(f"视频信息: {info}")
    print(f"详细信息: {json.dumps(info.to_dict(), indent=2)}")