# -*- coding: utf-8 -*-
"""
Velopack 更新源地址 —— 全项目唯一配置入口 (Phase 3)。

契约：
    更新源地址只在这里定义一次。客户端运行时（utils.update_service）、发布
    脚本（scripts/velopack_release.py）都从这里取，任何其它地方不得再写第二份
    URL。之前的教训正是同一份地址散落在 client / GUI / 构建脚本里，改一处漏一处。

为什么是一条「目录」地址：
    Velopack 的 HttpSource 把源当成**静态文件目录**，它取的是
    ``<URL>/releases.<channel>.json``（清单）以及清单里列出的 .nupkg / Setup.exe。
    因此这里配置的是目录地址，而不是某个具体文件。

    这条 URL 与发布 channel 是绑定的：源目录里放的是哪个 channel 的 feed，URL
    就必须指向那个目录。channel 名由发布脚本单独定义（scripts/velopack_release.py
    的 CHANNEL），两处描述的是同一件事的两个侧面（"哪个目录" / "目录里是哪个
    channel 的文件"），不构成同一个配置的双份拷贝。

客户端禁止直接依赖：
    中转网盘（share.feijipan.com / api.feijipan.com）、网盘的 AES 参数、
    任何临时 CDN URL。客户端只认这一条自有源。

临时 / 本地测试源：
    正式源尚未部署，或做本地 E2E 时，用环境变量 LIVELENS_UPDATE_URL 覆盖即可，
    不必改这个文件，也不会产生第二份配置（见 get_update_source）。
"""

import os

#: 正式更新源目录。客户端默认只走这一条。
#:
#: 未完成项：livelen.icu 上的这个目录**尚未部署**（Phase 3 未扩服务器范围）。
#: 部署时把 vpk pack 的输出目录内容原样上传到该路径即可，客户端无需改动。
DEFAULT_UPDATE_SOURCE = "https://livelen.icu/updates/stable"

#: 覆盖用环境变量名。仅用于本地/临时测试源与 E2E。
ENV_OVERRIDE = "LIVELENS_UPDATE_URL"

#: 发布渠道。与上面那条 URL 描述的是同一件事的两面：
#: URL 说「目录在哪」，channel 说「目录里的清单叫什么」——
#: Velopack 取的清单文件名是 ``releases.<channel>.json``，客户端要自己取一次
#: 清单（Python binding 没有暴露 release feed），所以需要这个名字。
#:
#: 必须与 scripts/velopack_release.py 的 CHANNEL 一致，否则回滚时会 404。
CHANNEL = "stable"


def get_update_source() -> str:
    """返回当前生效的更新源地址（去掉首尾空白，末尾斜杠统一去掉）。

    环境变量 LIVELENS_UPDATE_URL 非空时优先 —— 这是唯一的覆盖途径，
    目的是让测试可以指向本地临时源而不改代码、不产生第二份硬编码。
    """
    override = (os.environ.get(ENV_OVERRIDE) or "").strip()
    url = override or DEFAULT_UPDATE_SOURCE
    return url.rstrip("/")
