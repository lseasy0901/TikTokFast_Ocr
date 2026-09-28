# -*- coding: utf-8 -*-
"""
admin2 — LiveLens Admin 2.0 独立后台模块（Phase 2）。

设计约束：
* 与既有 SQLAdmin 后台（/admin）**并行**运行，经典后台保留为回退入口；
* 认证完全复用既有体系：登录凭据 = ``settings.ADMIN_API_KEY``（fail closed），
  会话键复用 ``admin.auth.SESSION_KEY``，因此两个后台共享同一登录态；
* 所有授权业务一律走既有 Business Layer（``services.license_service`` /
  ``services.redemption_service``），本模块不新增任何授权状态变更语义；
* 统计口径唯一定义在 ``services.stats_service``。
"""

import logging
import os

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # <repo>/license_server/app
LICENSE_SERVER_DIR = os.path.dirname(_APP_DIR)                            # <repo>/license_server

#: admin2 专属模板与静态资源目录
TEMPLATES_DIR = os.path.join(_APP_DIR, "templates", "admin2")
STATIC_DIR = os.path.join(_APP_DIR, "static", "admin2")


def setup_admin2(app):
    """在既有 FastAPI 应用上挂载 admin2（页面 + JSON API + 静态资源）。

    会话：vendored SQLAdmin 的 SessionMiddleware 只挂在它自己的 /admin 子应用上
    （application.py:145 ``self.admin = Starlette(middleware=...)``），主应用上
    没有会话中间件。admin2 运行在主应用上，因此这里显式补挂一份
    SessionMiddleware，密钥同为 ``settings.SECRET_KEY`` —— 签名密钥一致，
    Cookie 与经典后台互通，登录态共享。对既有 API 路由无语义影响
    （中间件只在会话被修改时才写 Set-Cookie）。

    调用时机：``setup_admin(app)`` 之后。模板/静态目录缺失时跳过挂载并告警，
    不影响其余路由（与 admin1 同策略）。
    """
    from starlette.middleware.sessions import SessionMiddleware

    import config

    app.add_middleware(SessionMiddleware, secret_key=config.settings.SECRET_KEY)

    if not os.path.isdir(TEMPLATES_DIR):
        logger.warning("admin2 模板目录缺失，跳过挂载：%s", TEMPLATES_DIR)
        return None

    from admin2.routes import register_admin2
    register_admin2(app)
    logger.info("admin2 已挂载：/admin2（页面）与 /admin2/api/*（认证 JSON API）")
    return True
