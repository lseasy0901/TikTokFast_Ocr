# -*- coding: utf-8 -*-
"""
Phase 7.2-7 — SQLAdmin 管理后台接入。

设计约束（本阶段硬性要求）：
    - ``admin_web/sqladmin/`` 是上游 SQLAdmin 0.31.1 的只读快照，必须保持逐字节
      一致。本包只使用 SQLAdmin 的公开扩展面接入：
      ``Admin`` / ``ModelView`` / ``BaseView`` / ``@action`` / ``@expose`` /
      ``AuthenticationBackend`` / ``Secret`` / ``StaticValuesFilter``。
      不修改其任何源码文件。
    - 所有许可证变更一律走既有 Business Layer（``app/services/*``）。
      本包不做任何业务规则判断，也不直接写 License / Authorization 表。
    - 不触碰 ``app/models.py`` / ``app/schemas.py`` / ``app/services/*``。

挂载方式：
    在 ``app/main.py`` 中创建 FastAPI 应用后调用一次 :func:`setup_admin`。
    管理后台位于 ``/admin``，与既有 ``/api/v1/admin/licenses`` 路由不冲突。
"""

import logging
import os
import sys

import config
import database

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 路径解析
# ----------------------------------------------------------------------
# __file__ = <repo>/license_server/app/admin/__init__.py
_ADMIN_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_ADMIN_PKG_DIR)              # <repo>/license_server/app
_LICENSE_SERVER_DIR = os.path.dirname(_APP_DIR)         # <repo>/license_server
_REPO_ROOT = os.path.dirname(_LICENSE_SERVER_DIR)       # <repo>

#: 上游 SQLAdmin 0.31.1 快照的包根目录（该目录下即为可导入的 ``sqladmin`` 包）。
VENDORED_SQLADMIN_DIR = os.path.join(_REPO_ROOT, "admin_web", "sqladmin")

#: 本项目自己的后台模板目录（自定义页面的覆盖层，不写入快照）。
TEMPLATES_DIR = os.path.join(_APP_DIR, "templates", "admin")

#: 上游快照的版本号，仅用于启动时自检。
EXPECTED_SQLADMIN_VERSION = "0.31.1"


def vendored_sqladmin_available() -> bool:
    """内置快照是否已投放。

    ``admin_web/`` 被 .gitignore 排除（见 .gitignore:47），属于随部署单独投放的
    本地组件，因此全新克隆的仓库里不会有它。管理后台是「本地可选的运营界面」，
    它的缺席不应该让许可证服务本身起不来 —— 见 :func:`setup_admin`。
    """
    return os.path.isdir(VENDORED_SQLADMIN_DIR)


def _register_vendored_sqladmin() -> None:
    """把内置 SQLAdmin 快照放到 ``sys.path`` 最前面（幂等，可重复调用）。

    刻意不使用 PyPI 上安装的 sqladmin：本项目以「内置快照」为准，
    这样版本可复现，且不会与快照产生两个副本。
    """
    if not os.path.isdir(VENDORED_SQLADMIN_DIR):
        return
    vendored = os.path.abspath(VENDORED_SQLADMIN_DIR)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != vendored]
    sys.path.insert(0, vendored)


# 在包导入时即完成注册，保证 `import admin.auth` 这类直接导入子模块的写法
# 也能拿到 sqladmin（Python 会先执行本 __init__）。
_register_vendored_sqladmin()


def _import_sqladmin():
    """导入内置快照，并自检它确实来自快照目录且版本正确。"""
    if not os.path.isdir(VENDORED_SQLADMIN_DIR):
        raise RuntimeError(
            f"未找到内置的 SQLAdmin 快照目录：{VENDORED_SQLADMIN_DIR}"
        )

    import sqladmin

    resolved = os.path.abspath(getattr(sqladmin, "__file__", "") or "")
    vendored = os.path.abspath(VENDORED_SQLADMIN_DIR) + os.sep
    if not resolved.startswith(vendored):
        raise RuntimeError(
            "sqladmin 并非从内置快照加载，已中止以避免与快照不一致的副本混用。\n"
            f"  期望前缀: {vendored}\n"
            f"  实际加载: {resolved}"
        )

    version = getattr(sqladmin, "__version__", None)
    if version is None:
        from importlib.metadata import version as _dist_version

        try:
            version = _dist_version("sqladmin")
        except Exception:  # noqa: BLE001 - 自检工具，取不到版本不应让启动失败
            version = EXPECTED_SQLADMIN_VERSION
    if version != EXPECTED_SQLADMIN_VERSION:
        raise RuntimeError(
            f"内置 SQLAdmin 版本不符：期望 {EXPECTED_SQLADMIN_VERSION}，实际 {version}"
        )

    return sqladmin


def setup_admin(app):
    """在既有 FastAPI 应用上挂载管理后台。

    快照未投放时（全新克隆、尚未部署 ``admin_web/``）**跳过挂载**并返回 ``None``，
    许可证 API 照常提供服务，只是 ``/admin`` 不可用。管理后台是本地可选的运营界面，
    其余路由与业务逻辑都不依赖它。

    这是唯一被容忍的跳过情形：快照**存在**但自检失败（加载到的不是快照、
    版本不符）仍然抛出，见 :func:`_import_sqladmin` —— 那种情况属于配置损坏，
    静默降级会把「装错了一份 sqladmin」伪装成「没装后台」。

    :param app: 已创建的 ``FastAPI`` 实例。
    :return: 已注册视图的 ``sqladmin.Admin`` 实例；快照缺席时返回 ``None``。
    """
    if not vendored_sqladmin_available():
        logger.warning(
            "内置 SQLAdmin 快照未投放，已跳过管理后台挂载（/admin 不可用）：%s\n"
            "许可证 API 不受影响。需要管理后台时，请把上游 SQLAdmin 快照投放到该目录。",
            VENDORED_SQLADMIN_DIR,
        )
        return None

    sqladmin = _import_sqladmin()

    from admin.auth import AdminAuth
    from admin.views import (
        AuthorizationAdmin,
        DauView,
        GenerateKeysView,
        LicenseAdmin,
        RedeemedHistoryView,
    )

    authentication_backend = AdminAuth(
        secret_key=config.settings.SECRET_KEY,
        admin_api_key=config.settings.ADMIN_API_KEY,
    )

    admin = sqladmin.Admin(
        app=app,
        engine=database.engine,
        base_url="/admin",
        title="Douyin Low Latency Viewer · 许可证后台",
        templates_dir=TEMPLATES_DIR,
        authentication_backend=authentication_backend,
    )

    admin.add_view(LicenseAdmin)
    admin.add_view(AuthorizationAdmin)
    admin.add_view(GenerateKeysView)
    admin.add_view(RedeemedHistoryView)
    admin.add_view(DauView)

    return admin
