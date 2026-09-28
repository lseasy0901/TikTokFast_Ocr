# -*- coding: utf-8 -*-
"""
FastAPI application for the License Server
"""

from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.security import APIKeyHeader
from contextlib import asynccontextmanager

import logging

import config
import database
import models
import schemas
from services import (
    feijipan_resolver,
    heartbeat_service,
    license_service,
    redemption_service,
    release_service,
)
from services.susi_security_service import SusiSecurityService

logger = logging.getLogger("DouyinLowLatencyViewer.license.server")

# Create database tables
database.Base.metadata.create_all(bind=database.engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("License Server starting...")
    yield
    # Shutdown
    print("License Server shutting down...")


# Create FastAPI app.
# Swagger UI and ReDoc are a development aid: in production they hand out a
# complete map of the activation API to anyone who asks. They are therefore
# mounted only when DEBUG is on. No route or business behavior depends on this.
app = FastAPI(
    title="License Server API",
    description="API for managing and validating license keys",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if config.settings.DEBUG else None,
    redoc_url="/redoc" if config.settings.DEBUG else None
)

# Security
api_key_header = APIKeyHeader(name="X-API-Key")

# Create SusiSecurityService for Phase 7.2-6 integration
susi_security_service = SusiSecurityService(config.settings)


def verify_admin_api_key(api_key: str = Security(api_key_header)):
    """Verify admin API key for protected endpoints"""
    if api_key != config.settings.ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    return api_key


@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "License Server API", "version": "1.0.0"}


@app.post(
    f"{config.settings.API_PREFIX}/admin/licenses",
    response_model=schemas.LicenseResponse,
    tags=["Admin"]
)
async def create_license(
    license_data: schemas.LicenseCreate,
    api_key: str = Depends(verify_admin_api_key),
    db=Depends(database.get_db)
):
    """Create a new license key (admin only)"""
    service = license_service.LicenseService(db)
    license = service.create_license(license_data)
    return license


@app.post(
    f"{config.settings.API_PREFIX}/licenses/activate",
    response_model=dict,
    tags=["License Operations"]
)
async def activate_license(
    activation_data: schemas.LicenseActivate,
    db=Depends(database.get_db)
):
    """Activate/redeem a license key and return SignedLicense"""
    service = redemption_service.RedemptionService(db, susi_security_service)
    try:
        license, response = service.redeem_license(activation_data)
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.args[0]
        )


@app.post(
    f"{config.settings.API_PREFIX}/licenses/validate",
    response_model=schemas.LicenseValidationResponse,
    tags=["License Operations"]
)
async def validate_license(
    validation_data: schemas.LicenseValidate,
    db=Depends(database.get_db)
):
    """Validate a license key"""
    service = redemption_service.RedemptionService(db, susi_security_service)
    return service.validate_license(validation_data)


@app.post(
    f"{config.settings.API_PREFIX}/licenses/heartbeat",
    response_model=schemas.HeartbeatResponse,
    tags=["License Operations"]
)
async def license_heartbeat(
    heartbeat_data: schemas.LicenseHeartbeat,
    db=Depends(database.get_db)
):
    """Record a client launch for DAU (Phase 7.4).

    Unauthenticated, like /activate and /validate: the device identifier is not a
    secret, and this endpoint grants nothing. It is idempotent per
    (device, business day) and deliberately does not check authorization state --
    an expired device that still starts is still an active device.

    Failing loudly (5xx) when the business time zone is unavailable is intended;
    see services/heartbeat_service.py. The client ignores every failure.
    """
    service = heartbeat_service.HeartbeatService(db)
    return service.record_heartbeat(heartbeat_data)


# ----------------------------------------------------------------------
# Phase 2: 自动更新分发（Release Registry + Velopack feed + 包重定向）
#
# 只服务「**已安装** LiveLens」的自动更新链路，与授权系统完全解耦：
#   - 发布接口沿用既有 admin API key 鉴权；
#   - feed / package 接口**不要求** admin key（客户端不可能持有它）；
#   - 大文件绝不经过本服务：解析出临时直链后 307 交给 Feijipan CDN。
# ----------------------------------------------------------------------
@app.post(
    f"{config.settings.API_PREFIX}/updates/releases",
    response_model=schemas.UpdateReleaseResponse,
    tags=["Updates"],
)
async def create_update_release(
    payload: schemas.UpdateReleaseCreate,
    api_key: str = Depends(verify_admin_api_key),
    db=Depends(database.get_db),
):
    """登记一个 stable release（admin only）。

    发布前会调用 Feijipan resolver，按 ``package_filename`` **精确**确认目标文件
    在分享里确实存在；解析不到、或解析结果文件名不完全一致，一律拒绝发布，
    库里不会留下半条记录。
    """
    try:
        return await release_service.publish(db, payload)
    except release_service.ReleaseError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@app.get("/updates/{channel}/{filename}", tags=["Updates"])
async def updates_feed_or_package(
    channel: str,
    filename: str,
    db=Depends(database.get_db),
):
    """更新 feed 与包重定向共用一个入口。

    路径形状由客户端决定，**不能改**：

        /updates/stable/releases.stable.json   更新清单（客户端真正读的）
        /updates/stable/assets.stable.json     资产索引
        /updates/stable/RELEASES-stable        旧版 Squirrel 清单
        /updates/stable/<package_filename>     Velopack 下载 .nupkg 时走这里
    """
    channel = (channel or "").lower()
    if channel not in release_service.SUPPORTED_CHANNELS:
        raise HTTPException(status_code=404, detail="未知更新渠道")

    if filename in (f"releases.{channel}.json", f"assets.{channel}.json",
                    f"RELEASES-{channel}"):
        release = release_service.current_release(db, channel)
        if release is None:
            raise HTTPException(status_code=404, detail="该渠道还没有已发布的版本")
        if filename == f"releases.{channel}.json":
            return JSONResponse(release_service.build_releases_manifest(release))
        if filename == f"assets.{channel}.json":
            return JSONResponse(release_service.build_assets_manifest(release))
        return Response(
            content=release_service.build_releases_legacy(release),
            media_type="text/plain",
        )

    if filename.endswith(".nupkg"):
        return await _redirect_package(db, channel, filename)

    raise HTTPException(status_code=404, detail="未找到该文件")


async def _redirect_package(db, channel: str, filename: str):
    """把 .nupkg 请求重定向到实时解析出的临时直链。

    **不代理内容**：只做「查库 → 解析 → redirect」，大文件由 Feijipan CDN 直接
    返回给客户端。临时直链不落库、不入日志、不进缓存。
    """
    release = release_service.find_release(db, channel, filename)
    if release is None:
        raise HTTPException(status_code=404, detail="该版本未发布")

    try:
        # 精确传 filename：resolver 只接受完全同名的那一个文件。
        resolved = await feijipan_resolver.resolve(
            release.package_share_url, "", release.package_filename
        )
    except feijipan_resolver.FeijipanResolveError as e:
        # 只记 resolver 的错误消息本身 —— 它不含临时直链。
        logger.warning(
            "更新包解析失败 channel=%s file=%s: %s", channel, filename, e
        )
        raise HTTPException(status_code=502, detail="更新包暂时不可用，请稍后重试")

    direct_url = resolved.get("url")
    if not direct_url or resolved.get("file_name") != filename:
        # 防御：绝不把别的文件重定向给客户端。
        logger.error(
            "解析结果与请求的文件不匹配 channel=%s file=%s", channel, filename
        )
        raise HTTPException(status_code=502, detail="更新包暂时不可用，请稍后重试")

    # 307：语义不变，交给 Feijipan CDN 直接传大文件。
    # no-store：临时直链不进任何缓存。
    logger.info("重定向更新包 channel=%s file=%s", channel, filename)
    return RedirectResponse(
        url=direct_url, status_code=307, headers={"Cache-Control": "no-store"}
    )


# ----------------------------------------------------------------------
# Phase 7.2-7: Admin Web (SQLAdmin) — mounted at /admin
#
# 纯增量：不改动上面任何既有路由。管理后台使用内置的 SQLAdmin 0.31.1 快照
# (admin_web/sqladmin)，并通过既有 Business Layer 完成所有许可证变更。
# ----------------------------------------------------------------------
from admin import setup_admin  # noqa: E402

setup_admin(app)

# ----------------------------------------------------------------------
# Phase 2: Admin 2.0 — 独立新后台，挂载于 /admin2，与既有 /admin（经典后台）
# 并行运行。认证复用同一套会话（admin.auth.SESSION_KEY），登录态互通。
# ----------------------------------------------------------------------
from admin2 import setup_admin2  # noqa: E402

setup_admin2(app)


if __name__ == "__main__":
    # 便捷启动入口。固定绑定回环地址，且**永不**自动开启 reload：
    # 自动 reload 只适合开发，生产环境会让进程被监视器反复重启。
    #
    # 生产请用 CLI 形式（本项目的模块布局是「app/ 在 sys.path 上，模块名 main」，
    # 因此需要 --app-dir；写成 app.main:app 会因 app/ 不在 sys.path 而导入失败）：
    #     python -m uvicorn main:app --app-dir app --host 127.0.0.1 --port 8000
    #
    # 这里传 app 对象而不是导入字符串：`python main.py` 时本文件已经以 __main__
    # 执行过一次，若再让 uvicorn 以字符串导入，模块会被执行第二遍（再建一个
    # engine、再跑一次 create_all）。传对象则只有一个实例。
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)