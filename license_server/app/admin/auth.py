# -*- coding: utf-8 -*-
"""
Phase 7.2-7 — 管理后台访问控制。

本项目没有用户表，也没有既有的运维账号体系。V1 因此**复用现有的运维凭据**
``settings.ADMIN_API_KEY``（该值已经用于保护 ``POST /api/v1/admin/licenses``），
不新增用户表、不新增配置项。

约定：
    - 用户名固定为 :data:`ADMIN_USERNAME`（占位，仅为满足登录表单字段；
      改动只需改这一行的常量）。
    - 密码必须等于 ``settings.ADMIN_API_KEY``，使用 ``secrets.compare_digest``
      做定长比较，避免时序侧信道。
    - **未配置 ADMIN_API_KEY 时一律登录失败（fail closed）**，
      绝不允许出现「无凭据即可进入后台」的状态。

除认证外，每个视图还显式实现 ``is_accessible()``。SQLAdmin 0.31.1 修复了
GHSA-h6cj-3759-22v4：``is_accessible()`` 此前未作用于 ``@action`` /
``@expose`` 端点。两个钩子同时具备，才能保证内置页面、自定义动作与自定义
页面都受到同一套判定。
"""

import secrets

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

#: 登录表单的用户名占位值（本项目无用户表，仅作字段占位）。
ADMIN_USERNAME = "admin"

#: 会话中标记已通过运维登录的键。
SESSION_KEY = "dlv_admin_authenticated"


class AdminAuth(AuthenticationBackend):
    """基于既有 ADMIN_API_KEY 的会话式后台登录。"""

    def __init__(self, secret_key: str, admin_api_key: str | None) -> None:
        # secret_key 用于签名会话 cookie，取既有 settings.SECRET_KEY。
        super().__init__(secret_key=secret_key)
        self._admin_api_key = admin_api_key or ""

    # ------------------------------------------------------------------
    # 供视图复用的会话判定
    # ------------------------------------------------------------------
    @staticmethod
    def is_authenticated(request: Request) -> bool:
        """当前请求是否已通过运维登录。

        没有挂载 SessionMiddleware 时读取 ``request.session`` 会抛
        AssertionError；这里一律按「未认证」处理，避免把异常变成 500。
        """
        try:
            return bool(request.session.get(SESSION_KEY))
        except (AssertionError, KeyError):
            return False

    # ------------------------------------------------------------------
    # AuthenticationBackend
    # ------------------------------------------------------------------
    async def login(self, request: Request) -> bool:
        """校验登录表单；凭据无效时返回 False（SQLAdmin 会渲染错误提示）。"""
        form = await request.form()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")

        # fail closed：未配置运维凭据时任何人都不得登录
        if not self._admin_api_key:
            return False

        username_ok = secrets.compare_digest(username, ADMIN_USERNAME)
        password_ok = secrets.compare_digest(password, self._admin_api_key)
        if not (username_ok and password_ok):
            return False

        request.session.update({SESSION_KEY: True})
        return True

    async def logout(self, request: Request) -> bool:
        """退出登录：清空会话。"""
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        """每个请求都会调用的会话校验。"""
        return self.is_authenticated(request)
