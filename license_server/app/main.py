# -*- coding: utf-8 -*-
"""
FastAPI application for the License Server
"""

from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from contextlib import asynccontextmanager

import config
import database
import models
import schemas
from services import heartbeat_service, license_service, redemption_service
from services.susi_security_service import SusiSecurityService

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
# Phase 7.2-7: Admin Web (SQLAdmin) — mounted at /admin
#
# 纯增量：不改动上面任何既有路由。管理后台使用内置的 SQLAdmin 0.31.1 快照
# (admin_web/sqladmin)，并通过既有 Business Layer 完成所有许可证变更。
# ----------------------------------------------------------------------
from admin import setup_admin  # noqa: E402

setup_admin(app)


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