# -*- coding: utf-8 -*-
"""
admin2 认证 — 完全复用既有后台凭据与会话键（fail closed）。

* 用户名固定 ``admin.auth.ADMIN_USERNAME``，密码 = ``settings.ADMIN_API_KEY``，
  ``secrets.compare_digest`` 定长比较；
* 会话键复用 ``admin.auth.SESSION_KEY`` → 与经典后台 /admin 共享登录态；
* 未配置 ADMIN_API_KEY 时任何登录一律失败；
* CSRF：会话内保存随机 token，表单与 JSON POST 均须携带
  （表单字段 ``csrf_token`` 或请求头 ``X-CSRF-Token``）。
"""

import secrets

import config
from admin.auth import SESSION_KEY, ADMIN_USERNAME

#: 会话中保存 CSRF token 的键
CSRF_SESSION_KEY = "admin2_csrf_token"

#: 登录失败锁定(2026-10-25 加固):15 分钟窗口内连错 5 次锁 10 分钟。
#: 内存态,重启即清——目标是拖慢在线爆破,不追求持久惩罚。
from services.rate_limit import LoginThrottle  # noqa: E402

_login_throttle = LoginThrottle(max_failures=5, failure_window=900.0,
                                lock_seconds=600.0)


def login_throttle() -> LoginThrottle:
    """暴露给登录路由:按 IP 记录成败;锁定期内直接拒绝。"""
    return _login_throttle


def is_authenticated(request) -> bool:
    try:
        return bool(request.session.get(SESSION_KEY))
    except (AssertionError, KeyError):
        return False


def verify_credentials(username: str, password: str) -> bool:
    """校验登录表单；未配置运维凭据时一律失败（fail closed）。"""
    admin_api_key = config.settings.ADMIN_API_KEY
    if not admin_api_key:
        return False
    username_ok = secrets.compare_digest(str(username or ""), ADMIN_USERNAME)
    password_ok = secrets.compare_digest(str(password or ""), str(admin_api_key))
    return username_ok and password_ok


def login_session(request) -> None:
    request.session.update({SESSION_KEY: True})
    request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(24)


def logout_session(request) -> None:
    request.session.clear()


def get_csrf_token(request) -> str:
    """取会话 CSRF token；没有则生成并写入（幂等）。"""
    try:
        token = request.session.get(CSRF_SESSION_KEY)
        if not token:
            token = secrets.token_urlsafe(24)
            request.session[CSRF_SESSION_KEY] = token
        return token
    except (AssertionError, KeyError):
        # SessionMiddleware 未挂载时无法提供 CSRF 保护，直接拒绝
        return ""


def check_csrf(request, submitted: str | None) -> bool:
    expected = ""
    try:
        expected = request.session.get(CSRF_SESSION_KEY) or ""
    except (AssertionError, KeyError):
        return False
    if not expected or not submitted:
        return False
    return secrets.compare_digest(str(submitted), expected)
