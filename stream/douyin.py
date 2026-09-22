# -*- coding: utf-8 -*-
"""抖音解析的兼容入口。

真实实现在 stream/adapters/douyin.py（平台适配器），这里只做再导出，
不包含第二套解析逻辑。保留本模块是为了让既有的
`from stream.douyin import DouyinStream, DouyinStreamError` 继续可用。
"""

import logging

from .adapters.douyin import DouyinAdapter, DouyinStream, DouyinStreamError

__all__ = ["DouyinStream", "DouyinStreamError", "DouyinAdapter"]


# ======================================================================
# 命令行测试入口
# ======================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s %(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    live_url = input("请输入抖音直播间URL: ").strip()
    douyin = DouyinStream(live_url)
    try:
        result = douyin.get_stream_url()
    except DouyinStreamError as e:
        print(f"[解析失败] {e}")
    else:
        print(f"room_id:     {result['room_id']}")
        print(f"stream_type: {result['stream_type']}")
        print(f"stream_url:  {result['stream_url']}")
