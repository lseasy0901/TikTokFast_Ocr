# -*- coding: utf-8 -*-
"""
admin2 路由 — 页面 + 认证 JSON API + CSV 导出（Phase 2 阶段 B）。

安全约定：
* 页面未登录 → 303 到 /admin2/login；
* API 未登录 → 401 JSON（前端据此跳登录页）；
* 所有 POST 校验会话 CSRF token（表单字段 csrf_token / 请求头 X-CSRF-Token）；
* 生成/反查等涉及明文 KEY 的响应带 no-store 头，且**不写任何日志**；
* 所有输入经 pydantic 校验或白名单过滤，SQL 全部参数绑定（IN 子句使用
  白名单内联，见 stats_service）。
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import database
import services.stats_service as stats
from admin.auth import SESSION_KEY
from admin2 import auth as a2auth
from admin2 import TEMPLATES_DIR, STATIC_DIR
from database import get_db
from schemas import LicenseCreate
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter()


def register_admin2(app) -> None:
    """挂载静态资源与全部 admin2 路由（由 admin2.setup_admin2 调用）。"""
    if os.path.isdir(STATIC_DIR):
        app.mount("/admin2/static", StaticFiles(directory=STATIC_DIR), name="admin2_static")
    else:
        logger.warning("admin2 静态目录缺失：%s（页面样式与脚本将 404）", STATIC_DIR)
    app.include_router(router)


# ======================================================================
# 依赖
# ======================================================================

def require_api_auth(request: Request) -> None:
    """API 鉴权依赖：未登录返回 401 JSON。"""
    if not a2auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="未登录或会话已失效")


def require_csrf(request: Request, token: str | None) -> None:
    if not a2auth.check_csrf(request, token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试")


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


# ======================================================================
# 页面
# ======================================================================

def _page_ctx(request: Request, active: str, title: str, subtitle: str) -> dict:
    return {
        "request": request,
        "active": active,
        "title": title,
        "subtitle": subtitle,
        "csrf_token": a2auth.get_csrf_token(request),
        "authenticated": a2auth.is_authenticated(request),
    }


def _page(request: Request, template: str, active: str, title: str, subtitle: str):
    if not a2auth.is_authenticated(request):
        return RedirectResponse("/admin2/login", status_code=303)
    resp = templates.TemplateResponse(request, template, _page_ctx(request, active, title, subtitle))
    return no_store(resp)


@router.get("/admin2")
@router.get("/admin2/")
async def page_index(request: Request):
    return _page(request, "index.html", "index", "数据总览", "今日经营指标与近期趋势")


@router.get("/admin2/keys")
async def page_keys(request: Request):
    return _page(request, "keys.html", "keys", "卡密管理", "批量生成、状态筛选与 KEY 反向查询")


@router.get("/admin2/devices")
async def page_devices(request: Request):
    return _page(request, "devices.html", "devices", "设备授权", "设备列表、授权状态与到期时间")


@router.get("/admin2/redemptions")
async def page_redemptions(request: Request):
    return _page(request, "redemptions.html", "redemptions", "兑换记录", "首次激活、续费与完整兑换流水")


@router.get("/admin2/analytics")
async def page_analytics(request: Request):
    return _page(request, "analytics.html", "analytics", "数据分析", "按天统计新增设备与套餐兑换")


# ======================================================================
# 登录 / 登出
# ======================================================================

@router.get("/admin2/login")
async def login_page(request: Request):
    if a2auth.is_authenticated(request):
        return RedirectResponse("/admin2/", status_code=303)
    resp = templates.TemplateResponse(request, "login.html", {
        "request": request, "error": None, "csrf_token": a2auth.get_csrf_token(request),
    })
    return no_store(resp)


@router.post("/admin2/login")
async def login_submit(request: Request):
    form = await request.form()
    submitted_csrf = str(form.get("csrf_token") or "")
    if not a2auth.check_csrf(request, submitted_csrf):
        resp = templates.TemplateResponse(request, "login.html", {
            "request": request, "error": "会话已过期，请重新提交。",
            "csrf_token": a2auth.get_csrf_token(request),
        }, status_code=403)
        return no_store(resp)

    if not a2auth.verify_credentials(form.get("username"), form.get("password")):
        # 不区分「用户名错误/密码错误/未配置凭据」，避免泄露信息
        resp = templates.TemplateResponse(request, "login.html", {
            "request": request,
            "error": "用户名或密码错误。",
            "csrf_token": a2auth.get_csrf_token(request),
        }, status_code=401)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    a2auth.login_session(request)
    return RedirectResponse("/admin2/", status_code=303)


@router.get("/admin2/logout")
async def logout(request: Request):
    a2auth.logout_session(request)
    return RedirectResponse("/admin2/login", status_code=303)


# ======================================================================
# JSON API
# ======================================================================

@router.get("/admin2/api/overview", dependencies=[Depends(require_api_auth)])
async def api_overview(days: int = 30, db: Session = Depends(get_db)):
    try:
        data = stats.overview(db, days=days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return no_store(JSONResponse(data))


@router.get("/admin2/api/keys", dependencies=[Depends(require_api_auth)])
async def api_keys(state: str | None = None, plan: str | None = None, q: str | None = None,
                   page: int = 1, page_size: int = 10, db: Session = Depends(get_db)):
    data = stats.keys_page(db, state=state, plan=plan, q=(q or "").strip() or None,
                           page=page, page_size=page_size)
    data["counts"] = stats.key_counts(db)
    return no_store(JSONResponse(data))


class GenerateKeysRequest(BaseModel):
    duration_days: int = Field(..., description="有效期天数，必须为后台预设之一")
    quantity: int = Field(..., ge=1, le=1000)
    features: str | None = Field(None, max_length=500, description="英文逗号分隔")


@router.post("/admin2/api/keys/generate", dependencies=[Depends(require_api_auth)])
async def api_keys_generate(payload: GenerateKeysRequest, request: Request,
                            db: Session = Depends(get_db)):
    require_csrf(request, request.headers.get("X-CSRF-Token"))

    # 单一事实来源：预设与上限直接引用 admin1 视图中的同一份常量
    from admin.views import DURATION_PRESETS, MAX_BATCH_QUANTITY, _create_license_batch

    if payload.duration_days not in DURATION_PRESETS:
        raise HTTPException(status_code=400,
                            detail="有效期必须为以下预设之一：" + "、".join(map(str, DURATION_PRESETS)) + " 天")
    if payload.quantity > MAX_BATCH_QUANTITY:
        raise HTTPException(status_code=400, detail=f"单次最多生成 {MAX_BATCH_QUANTITY} 个卡密")

    features = [f.strip() for f in (payload.features or "").split(",") if f.strip()]
    keys, days, error = _create_license_batch(LicenseCreate(duration_days=payload.duration_days),
                                              payload.quantity, features)
    if error:
        raise HTTPException(status_code=500, detail=error)
    return no_store(JSONResponse({
        "count": len(keys),
        "duration_days": days,
        "plan_name": stats.plan_name_of(days),
        "plaintext_keys": keys,   # 明文仅此一次；不落库、不写日志、响应 no-store
    }))


class RevokeRequest(BaseModel):
    license_id: int = Field(..., ge=1)


@router.post("/admin2/api/keys/revoke", dependencies=[Depends(require_api_auth)])
async def api_keys_revoke(payload: RevokeRequest, request: Request, db: Session = Depends(get_db)):
    require_csrf(request, request.headers.get("X-CSRF-Token"))

    from models import License, LicenseState
    from services.license_service import LicenseService

    lic = db.query(License).filter(License.id == payload.license_id).first()
    if lic is None:
        raise HTTPException(status_code=404, detail="卡密不存在")
    if lic.state is not LicenseState.UNUSED:
        raise HTTPException(status_code=409, detail="仅「未使用」的卡密可吊销")
    service = LicenseService(db)
    service.revoke_license(lic.id)
    return no_store(JSONResponse({"ok": True, "id": lic.id, "state": "revoked"}))


class LookupRequest(BaseModel):
    license_key: str = Field(..., min_length=1, max_length=200)


@router.post("/admin2/api/keys/lookup", dependencies=[Depends(require_api_auth)])
async def api_keys_lookup(payload: LookupRequest, request: Request, db: Session = Depends(get_db)):
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    # 输入不落库、不写日志（FastAPI 默认不记录请求体；本函数也不记录）
    data = stats.lookup_license(db, payload.license_key)
    return no_store(JSONResponse(data))


@router.get("/admin2/api/devices", dependencies=[Depends(require_api_auth)])
async def api_devices(state: str | None = None, q: str | None = None, sort: str | None = None,
                      page: int = 1, page_size: int = 10, db: Session = Depends(get_db)):
    data = stats.devices_page(db, state=state, q=(q or "").strip() or None, sort=sort,
                              page=page, page_size=page_size)
    return no_store(JSONResponse(data))


@router.get("/admin2/api/devices/{device_id}", dependencies=[Depends(require_api_auth)])
async def api_device_detail(device_id: str, db: Session = Depends(get_db)):
    data = stats.device_detail(db, device_id)
    if data is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return no_store(JSONResponse(data))


@router.get("/admin2/api/redemptions", dependencies=[Depends(require_api_auth)])
async def api_redemptions(type_: str | None = None, plan: str | None = None, q: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          page: int = 1, page_size: int = 12, db: Session = Depends(get_db)):
    try:
        data = stats.redemptions_page(db, type_=type_, plan=plan, q=(q or "").strip() or None,
                                      date_from=date_from or None, date_to=date_to or None,
                                      page=page, page_size=page_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return no_store(JSONResponse(data))


@router.get("/admin2/api/analytics", dependencies=[Depends(require_api_auth)])
async def api_analytics(days: int | None = 30, date_from: str | None = None,
                        date_to: str | None = None, db: Session = Depends(get_db)):
    try:
        data = stats.analytics(db, days=days,
                               date_from=date_from or None, date_to=date_to or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return no_store(JSONResponse(data))


# ======================================================================
# CSV 导出（与页面同鉴权；GET 带筛选参数）
# ======================================================================

def _csv_response(filename: str, rows: list[list]) -> Response:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow([stats.csv_safe_cell(c) for c in row])
    content = "\ufeff" + buf.getvalue()  # BOM：Excel 正确识别 UTF-8
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}", "Cache-Control": "no-store"},
    )


@router.get("/admin2/api/export/csv", dependencies=[Depends(require_api_auth)])
async def api_export_csv(scope: str, state: str | None = None, plan: str | None = None,
                         q: str | None = None, type_: str | None = None,
                         date_from: str | None = None, date_to: str | None = None,
                         days: int | None = None, db: Session = Depends(get_db)):
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d")
    try:
        if scope == "keys":
            rows = [["ID", "KEY指纹", "套餐", "功能标签", "状态", "创建时间(北京)", "兑换时间(北京)", "关联设备"]]
            # 全量导出：按页迭代直到取尽（每行仍经 csv_safe_cell 防公式注入）
            page = 1
            while True:
                data = stats.keys_page(db, state=state, plan=plan, q=(q or "").strip() or None,
                                       page=page, page_size=500)
                rows += [[i["id"], i["hash"], i["plan_name"], ",".join(i["features"]), i["state"],
                          i["created_at_bj"] or "", i["redeemed_at_bj"] or "", i["device_id"] or ""]
                         for i in data["items"]]
                if len(data["items"]) < 200 or page >= 250:  # 取尽即停；安全上限 5 万行
                    break
                page += 1
            return _csv_response(f"keys-{stamp}.csv", rows)
        if scope == "devices":
            rows = [["设备ID", "状态", "首次激活(北京)", "到期时间(北京)", "最近活跃(北京)", "兑换次数", "最新套餐", "剩余天数"]]
            page = 1
            while True:
                data = stats.devices_page(db, state=state, q=(q or "").strip() or None, sort=None,
                                          page=page, page_size=500)
                rows += [[i["device_id"], i["state"], i["first_activation_bj"] or "", i["expires_at_bj"] or "",
                          i["last_seen_bj"] or "", i["redemptions"], i["latest_plan_name"], i["remain_days"]]
                         for i in data["items"]]
                if len(data["items"]) < 200 or page >= 250:
                    break
                page += 1
            return _csv_response(f"devices-{stamp}.csv", rows)
        if scope == "redemptions":
            rows = stats.redemptions_csv(db, type_=type_, plan=plan, q=(q or "").strip() or None,
                                         date_from=date_from or None, date_to=date_to or None)
            return _csv_response(f"redemptions-{stamp}.csv", rows)
        if scope == "daily":
            rows = stats.daily_csv(db, days, date_from or None, date_to or None)
            return _csv_response(f"daily-{stamp}.csv", rows)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=400, detail="未知导出类型")
