# -*- coding: utf-8 -*-
"""更新发布登记 + Velopack feed 构建（Phase 2）。

职责边界
--------
只服务「**已安装** LiveLens 用户的自动更新链路」：

    Velopack
      → releases.<channel>.json / assets.<channel>.json / RELEASES-<channel>
      → .nupkg 请求
      → 本服务
      → Feijipan share_url 实时解析
      → HTTP 302/307
      → Feijipan CDN
      → 客户端直接下载

不处理首次安装 / Setup.exe / 官网 / index.html / 安装链路 / 客户端代码。

不变量（本模块存在的理由）
--------------------------
* 库里**只**有永久 share_url。``direct_url`` 只活在一次请求的生命周期里，
  既不落库、也不进日志、更不缓存。
* feed 里的 ``FileName`` 必须与 ``UpdateRelease.package_filename`` **逐字相同**。
* 服务端**绝不代理** .nupkg 内容：只做「查库 → 解析 → redirect」。
* ``releases.<channel>.json`` 必须携带 SHA1/SHA256/Size —— 客户端下载后要
  用它校验包完整性，所以发布时必须由调用方（打包工具 ``vpk pack`` 的作者）
  提供；服务端不下载 .nupkg，无法自算。
"""

import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

import aiohttp

import config
import models
from services import feijipan_resolver

logger = logging.getLogger("DouyinLowLatencyViewer.license.release")

#: 本阶段唯一支持的渠道。
SUPPORTED_CHANNELS = ("stable",)

#: 与 scripts/velopack_release.py 的 PACK_ID 一致（feed 里的 PackageId）。
PACK_ID = "LiveLensDesktop"

#: 版本号：MAJOR.MINOR.PATCH，纯数字三段式（与 utils/version.py 的约定一致，
#: 因此可以直接做数值比较）。
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: 包文件名：只允许安全字符 + 必须以 .nupkg 结尾。斜杠 / 反斜杠 / ".." 一律不接受，
#: 避免把路径当成文件名写进 feed 或拼进 URL。
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.nupkg$")

_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

#: 旧版 Squirrel 格式的 RELEASES 文件带 UTF-8 BOM，且**没有**结尾换行。
#: 实测现有 feed 文件为 93 字节：BOM + "<SHA1> <FileName> <Size>"。
_RELEASES_BOM = b"\xef\xbb\xbf"


class ReleaseError(Exception):
    """发布 / 查询失败。消息面向调用方，且**不含**任何临时直链。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def version_key(version: str) -> Tuple[int, int, int]:
    """把 MAJOR.MINOR.PATCH 解析成可比较的元组；格式非法即抛。"""
    if not isinstance(version, str) or not _VERSION_RE.match(version.strip()):
        raise ReleaseError("version 必须是 MAJOR.MINOR.PATCH 形式", status=400)
    major, minor, patch = version.strip().split(".")
    return int(major), int(minor), int(patch)


def validate_package_filename(filename: str) -> str:
    """校验包文件名。"""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename):
        raise ReleaseError(
            "package_filename 非法：必须是 <名称>.nupkg，且不含路径分隔符", status=400
        )
    if len(filename) > 255:
        raise ReleaseError("package_filename 过长", status=400)
    return filename


def validate_channel(channel: str) -> str:
    if channel not in SUPPORTED_CHANNELS:
        raise ReleaseError(
            "暂不支持的 channel: " + str(channel)
            + "（当前仅支持 " + ", ".join(SUPPORTED_CHANNELS) + "）",
            status=400,
        )
    return channel


def validate_share_url(share_url: str) -> str:
    """复用 resolver 的白名单校验，不接受任意 URL。"""
    try:
        feijipan_resolver.validate_share_url(share_url)
    except feijipan_resolver.FeijipanResolveError as e:
        raise ReleaseError(f"package_share_url 非法: {e}", status=400) from e
    return share_url


def is_package_object_url(url: str) -> bool:
    """判断 package_share_url 是否为对象存储直链（OSS），而非飞盘分享链接。

    分流依据是 host 后缀白名单（config.PACKAGE_URL_HOST_SUFFIXES，环境变量可配）。
    """
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    suffixes = [s.strip().lower() for s in
                config.settings.PACKAGE_URL_HOST_SUFFIXES.split(",") if s.strip()]
    return any(host == s or host.endswith("." + s) for s in suffixes)


async def verify_object_url(url: str, expected_size: Optional[int]) -> None:
    """发布前对对象存储直链做 HEAD 复核：存在性 + 字节数（精确）。

    与飞盘路径的"发布前确认文件确实存在"同一原则；OSS 返回的是**精确字节**
    （Content-Length），没有飞盘 KB 取整的问题。
    失败抛 ReleaseError：不存在/大小不符 → 400；网络异常 → 502。
    """
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise ReleaseError(
                        f"对象存储文件不存在或不可读（HTTP {resp.status}）",
                        status=400,
                    )
                length = resp.headers.get("Content-Length")
                actual = int(length) if length else None
    except ReleaseError:
        raise
    except Exception as e:  # noqa: BLE001 - 上游/网络异常 → 502
        raise ReleaseError(f"对象存储访问失败: {e}", status=502) from e

    if expected_size is not None and actual != expected_size:
        raise ReleaseError(
            f"package_size 与对象存储中的文件大小不一致"
            f"（申报 {expected_size}B，实际 {actual}B）",
            status=400,
        )


def validate_hashes(sha1: str, sha256: str) -> Tuple[str, str]:
    if not isinstance(sha1, str) or not _SHA1_RE.match(sha1):
        raise ReleaseError("package_sha1 必须是 40 位十六进制", status=400)
    if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
        raise ReleaseError("package_sha256 必须是 64 位十六进制", status=400)
    return sha1.upper(), sha256.upper()


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def current_release(db: Session, channel: str) -> Optional[models.UpdateRelease]:
    """当前渠道的生效 release。

    「生效」= 版本号数值最大的那一条（同版本由唯一约束保证不会重复）。
    用数值比较而不是「最后插入」，是为了让 feed 天然单调：即便管理员误发布了一个
    更旧的版本，客户端也不会被 feed 带回旧版本。版本格式与 utils/version.py
    的约定一致，所以这个比较是既有的项目约定，不是新发明的规则。
    """
    rows = (
        db.query(models.UpdateRelease)
        .filter(models.UpdateRelease.channel == channel)
        .all()
    )
    if not rows:
        return None
    return max(rows, key=lambda r: (version_key(r.version), r.id))


def find_release(db: Session, channel: str, package_filename: str
                 ) -> Optional[models.UpdateRelease]:
    """按渠道 + 包文件名精确查找（Full 或 Delta 任一命中即可）。"""
    return (
        db.query(models.UpdateRelease)
        .filter(
            models.UpdateRelease.channel == channel,
            (models.UpdateRelease.package_filename == package_filename)
            | (models.UpdateRelease.delta_filename == package_filename),
        )
        .first()
    )


# ----------------------------------------------------------------------
# 发布
# ----------------------------------------------------------------------
async def publish(db: Session, payload) -> models.UpdateRelease:
    """登记一次发布。

    顺序是硬要求：**先校验 → 再向 Feijipan 确认目标文件确实存在 → 才写库**。
    任何一步失败都不会留下半条 release（需求：发布失败不得写入 release）。
    """
    version = (payload.version or "").strip()
    version_key(version)                                  # 1 校验 version
    channel = validate_channel((payload.channel or "stable").strip())
    package_filename = validate_package_filename(payload.package_filename)
    # URL 合法性按后端分流：对象存储直链校验 host 白名单 + https；
    # 飞盘走 validate_share_url（域名白名单 + /s/<id> 形态）。
    share_url = (payload.package_share_url or "").strip()
    if is_package_object_url(share_url):
        from urllib.parse import urlsplit
        parts = urlsplit(share_url)
        if parts.scheme != "https" or parts.username or parts.password:
            raise ReleaseError("对象存储直链必须是 https 且不含认证信息", status=400)
        if not parts.path or parts.path.endswith("/"):
            raise ReleaseError("对象存储直链必须指向具体文件", status=400)
    else:
        share_url = validate_share_url(share_url)
    sha1, sha256 = validate_hashes(payload.package_sha1, payload.package_sha256)
    if payload.package_size is not None and payload.package_size <= 0:
        raise ReleaseError("package_size 必须为正整数", status=400)

    # 同渠道同版本不允许重复
    exists = (
        db.query(models.UpdateRelease)
        .filter(
            models.UpdateRelease.channel == channel,
            models.UpdateRelease.version == version,
        )
        .first()
    )
    if exists is not None:
        raise ReleaseError(
            f"该渠道已存在版本 {version} 的 release", status=409
        )

    # 6/7/8/9：发布前确认目标文件确实存在。两种后端（按 URL host 分流）：
    #   - 对象存储（OSS 直链）：HEAD 复核存在性 + 精确字节数（无 KB 取整问题）
    #   - 飞盘（遗留）：按精确文件名解析确认；resolved 里的 url 是临时直链，
    #     只在本函数作用域内存在，绝不入库/入日志。
    # 任一失败都不会留下半条 release（需求：发布失败不得写入 release）。
    if is_package_object_url(share_url):
        await verify_object_url(share_url, payload.package_size)
        package_size = payload.package_size or 0
        if package_size <= 0:
            raise ReleaseError(
                "对象存储发布必须提供 package_size（字节数）", status=400
            )
    else:
        try:
            resolved = await feijipan_resolver.resolve(
                share_url, "", package_filename
            )
        except feijipan_resolver.FeijipanResolveError as e:
            # 这里只透传 resolver 自己的消息（不含临时直链）。
            # 分享里根本没有这个文件名 → 调用方给错了，属于 400；
            # 其余（网络 / 上游异常）才是 502。
            status_code = 400 if getattr(e, "code", None) == 404 else 502
            raise ReleaseError(f"发布前解析失败: {e}", status=status_code) from e

        resolved_name = resolved.get("file_name")
        if resolved_name != package_filename:
            raise ReleaseError(
                "解析结果与 package_filename 不一致，拒绝发布", status=400
            )

        resolved_size = resolved.get("file_size")
        if payload.package_size is not None:
            declared = payload.package_size
            # 实测（2026-09-30，飞盘 recommend/list 原始结构）：fileSize 是
            # **KB 向下取整**，不是字节 —— 15,106,417B 的包报告为 14,752，且
            # 结构里没有字节级字段。因此两种口径都算一致：精确字节，或 KB
            # 取整口径（declared // 1024）。
            # 落库用申报的**字节**大小：Velopack feed 的 Size 字段需要真实字节，
            # KB 值会让客户端更新进度/大小显示差 1024 倍。
            if resolved_size not in (declared, declared // 1024):
                # 名字对了但大小对不上，说明多半指错了文件。
                raise ReleaseError(
                    "package_size 与分享中的文件大小不一致"
                    f"（申报 {declared}B，飞盘报告 {resolved_size}KB）",
                    status=400,
                )
            package_size = declared
        else:
            # 未申报时只能采信飞盘的 KB 值（精度有限，仅在 payload 不带 size 时发生）
            package_size = resolved_size * 1024 if resolved_size else 0
        if not package_size or package_size <= 0:
            raise ReleaseError("无法确定 package_size", status=502)

    # ---- 增量包（可选）：与 Full 同样的「先校验 → 再写库」原则 ----
    delta_vals = None
    d_name = (getattr(payload, "delta_filename", None) or "").strip()
    d_url = (getattr(payload, "delta_share_url", None) or "").strip()
    if d_name or d_url:
        if not (d_name and d_url and payload.delta_sha1 and payload.delta_sha256):
            raise ReleaseError(
                "增量包字段不完整：delta_filename / delta_share_url / "
                "delta_sha1 / delta_sha256 必须成对提供", status=400,
            )
        validate_package_filename(d_name)
        d_sha1, d_sha256 = validate_hashes(payload.delta_sha1,
                                           payload.delta_sha256)
        if is_package_object_url(d_url):
            from urllib.parse import urlsplit
            parts = urlsplit(d_url)
            if parts.scheme != "https" or parts.username or parts.password:
                raise ReleaseError(
                    "增量包对象存储直链必须是 https 且不含认证信息", status=400)
            if not parts.path or parts.path.endswith("/"):
                raise ReleaseError("增量包直链必须指向具体文件", status=400)
            await verify_object_url(d_url, payload.delta_size)
            d_size = payload.delta_size or 0
            if d_size <= 0:
                raise ReleaseError(
                    "增量包对象存储发布必须提供 delta_size（字节数）", status=400)
        else:
            try:
                d_resolved = await feijipan_resolver.resolve(d_url, "", d_name)
            except feijipan_resolver.FeijipanResolveError as e:
                status_code = 400 if getattr(e, "code", None) == 404 else 502
                raise ReleaseError(f"增量包发布前解析失败: {e}",
                                   status=status_code) from e
            if d_resolved.get("file_name") != d_name:
                raise ReleaseError(
                    "增量包解析结果与 delta_filename 不一致，拒绝发布", status=400)
            d_res_size = d_resolved.get("file_size")
            if payload.delta_size is not None:
                d_declared = payload.delta_size
                if d_res_size not in (d_declared, d_declared // 1024):
                    raise ReleaseError(
                        "delta_size 与分享中的文件大小不一致"
                        f"（申报 {d_declared}B，飞盘报告 {d_res_size}KB）",
                        status=400)
                d_size = d_declared
            else:
                d_size = d_res_size * 1024 if d_res_size else 0
            if not d_size or d_size <= 0:
                raise ReleaseError("无法确定 delta_size", status=502)
        delta_vals = dict(delta_filename=d_name, delta_share_url=d_url,
                          delta_sha1=d_sha1, delta_sha256=d_sha256,
                          delta_size=int(d_size))

    release = models.UpdateRelease(
        version=version,
        channel=channel,
        package_filename=package_filename,
        package_share_url=share_url,      # 永久公开 URL（OSS 直链或飞盘分享）
        package_sha1=sha1,
        package_sha256=sha256,
        package_size=int(package_size),
        release_notes=payload.release_notes,
        **(delta_vals or {}),
    )
    db.add(release)
    db.commit()
    db.refresh(release)

    # 只记这些；绝不记 share_url 之外的任何 URL，更不记临时直链。
    logger.info(
        "已发布更新 channel=%s version=%s file=%s size=%s",
        channel, version, package_filename, release.package_size,
    )
    return release


# ----------------------------------------------------------------------
# Feed 构建
#
# 三个文件都与现有 vpk 产出的格式保持一致（逐字段核对过真实 feed 文件）：
#   releases.<channel>.json : {"Assets":[{PackageId,Version,Type,FileName,SHA1,SHA256,Size[,NotesMarkdown]}]}
#   assets.<channel>.json   : [{"RelativeFileName": <Setup.exe>, "Type":"Installer"},
#                              {"RelativeFileName": <nupkg>,     "Type":"Full"}]
#   RELEASES-<channel>      : BOM + "<SHA1> <FileName> <Size>"
# ----------------------------------------------------------------------
def build_releases_manifest(release: models.UpdateRelease) -> dict:
    """当前客户端真正读取的更新清单（客户端读 NotesMarkdown 显示更新说明）。"""
    asset = {
        "PackageId": PACK_ID,
        "Version": release.version,
        "Type": "Full",
        "FileName": release.package_filename,
        "SHA1": release.package_sha1.upper(),
        "SHA256": release.package_sha256.upper(),
        "Size": int(release.package_size),
    }
    if release.release_notes:
        # 客户端 gui/update_dialog.py 读的就是 NotesMarkdown。
        asset["NotesMarkdown"] = release.release_notes
    assets = [asset]
    if release.delta_filename:
        # Delta 条目：只有从上一版本升级的客户端会用到（Velopack 自动
        # 选择）；其余场景（老版本 / delta 失败）回落上面的 Full。
        delta = {
            "PackageId": PACK_ID,
            "Version": release.version,
            "Type": "Delta",
            "FileName": release.delta_filename,
            "SHA1": release.delta_sha1.upper(),
            "SHA256": release.delta_sha256.upper(),
            "Size": int(release.delta_size),
        }
        assets.append(delta)
    return {"Assets": assets}


def build_assets_manifest(release: models.UpdateRelease) -> List[dict]:
    """资产索引。Installer 一项沿用既有 feed 的写法（本阶段不处理 Setup.exe）。"""
    return [
        {"RelativeFileName": f"{PACK_ID}-{release.channel}-Setup.exe",
         "Type": "Installer"},
        {"RelativeFileName": release.package_filename, "Type": "Full"},
    ]


def build_releases_legacy(release: models.UpdateRelease) -> bytes:
    """旧版 Squirrel 格式（BOM + 一行，无结尾换行），与现有 feed 逐字节同构。"""
    line = "{} {} {}".format(
        release.package_sha1.upper(), release.package_filename,
        int(release.package_size),
    )
    return _RELEASES_BOM + line.encode("utf-8")
