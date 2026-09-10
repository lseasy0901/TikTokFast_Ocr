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
from services import license_service

# Create database tables
database.Base.metadata.create_all(bind=database.engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("License Server starting...")
    yield
    # Shutdown
    print("License Server shutting down...")


# Create FastAPI app
app = FastAPI(
    title="License Server API",
    description="API for managing and validating license keys",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Security
api_key_header = APIKeyHeader(name="X-API-Key")


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
    """Activate a license key"""
    service = license_service.LicenseService(db)
    try:
        license, response = service.activate_license(activation_data)
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
    service = license_service.LicenseService(db)
    return service.validate_license(validation_data)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=config.settings.DEBUG
    )