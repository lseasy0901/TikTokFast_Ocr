# -*- coding: utf-8 -*-
"""
Phase 7.2-7 — 管理后台视图。

范围（V1）：
    - License Key 生成（一次性明文展示）
    - License Key 列表 / 详情
    - 兑换历史（派生视图，非事件日志）
    - 吊销（**仅允许 UNUSED**）
    - Authorization 只读查看

明确不在范围内：
    - Product 管理（现有 schema 没有 Product 表，本阶段不新增）
    - 授权（Authorization）吊销 / 收回已兑换设备的权益
      —— 现有 Business Layer 没有该能力，本阶段不新增业务逻辑

所有变更都通过既有 Business Layer 完成：
    ``services.license_service.LicenseService``
校验复用既有 ``schemas.LicenseCreate``。

关于明文 License Key：
    ``models.License`` **只保存 key_hash**，明文仅在 ``create_license``
    的内存对象上出现一次（见 models.py 中的说明）。因此：
    - 本后台**无法**列出、搜索或再次展示明文 Key，这是既有安全模型决定的，
      不是 UI 限制；
    - 生成后必须立即通过一次性弹窗复制。
    相应地，License 视图的 ``can_create`` 必须为 False：SQLAdmin 内置表单
    不会生成 key_hash，直接提交会写入 NULL。
"""

import datetime as _dt
import logging

from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy import distinct as sa_distinct, func as sa_func
from sqladmin import BaseView, Flash, ModelView, Secret, action, expose
from sqladmin.filters import StaticValuesFilter
from starlette.responses import RedirectResponse

import config
import database
from admin.auth import AdminAuth
from models import Authorization, DeviceDailyActive, License, LicenseState
from schemas import LicenseCreate
from services.license_service import LicenseService

logger = logging.getLogger(__name__)


# ======================================================================
# 展示层时区
#
# 数据库里所有 datetime 列都是 **naive UTC**：``models.py`` 的 ``DateTime`` 没有
# 声明 ``timezone=True``，SQLite 的绑定处理器直接取 ``value.year/month/...``
# 存储，不做时区归一；写入侧是 ``datetime.now(timezone.utc)`` 与 ``func.now()``
# （SQLite 展开为 ``CURRENT_TIMESTAMP``，同为 UTC）。
# 业务侧也遵循同一条约定 —— ``susi_security_service._to_rfc3339()`` 与
# ``redemption_service`` 都把 naive 值显式当作 UTC 解释。
#
# 后台是给人看的，运维在东八区；直接渲染 naive UTC 会让每个时间都早 8 小时。
# 因此只在展示层做一次换算，不改动存储值，也不改动任何业务计算。
# ======================================================================
DISPLAY_TZ_NAME = config.settings.BUSINESS_TZ_NAME

try:
    from zoneinfo import ZoneInfo

    _DISPLAY_TZ = ZoneInfo(DISPLAY_TZ_NAME)
except Exception as exc:  # noqa: BLE001 - 缺时区库不应让整个后台不可用
    # Windows 没有系统 IANA 时区库，需要 tzdata 包（见 requirements.txt）。
    # 这里刻意不降级成固定偏移去「看起来正确」：宁可显示带 UTC 标注的原值，
    # 也不让运维看到一个无法分辨真伪的北京时间。
    _DISPLAY_TZ = None
    logger.warning(
        "无法加载时区 %s，后台将按 UTC 展示并显式标注（原因：%s）。"
        "请在运行环境安装 tzdata。",
        DISPLAY_TZ_NAME,
        exc,
    )


def _datetime_formatter(value):
    """后台 datetime 列格式化：naive UTC → 北京时间（仅展示层换算）。

    只读：返回新字符串，``value`` 本身不被修改。

    与 SQLAdmin 内置 ``datetime_formatter`` 的差异：
        - 内置版直接 ``value.strftime(...)``，把 naive 值按字面量渲染，
          因而在东八区看起来早 8 小时。
        - 本函数先把 naive 值显式解释为 UTC，再 ``astimezone`` 到
          ``Asia/Shanghai``；已带时区的值按其自身偏移换算（不会二次平移）。
        - 非 datetime 值（``datetime.date``、``str`` 等）原样返回，
          与内置行为一致；``None`` 仍由 ``empty_formatter`` 处理。
    """
    if value is None:
        return Markup("")

    if not isinstance(value, _dt.datetime):
        # 保持原有行为：date / str / int 等一概不做处理
        return value

    if _DISPLAY_TZ is None:
        stamp = value.strftime("%d %B %Y %H:%M:%S")
        return Markup(
            f"<span "
            f'class="my-1 py-1 px-2 badge bg-secondary text-light '
            f'lead d-inline-block text-truncate" '
            f'data-bs-toggle="tooltip" '
            f'data-bs-html="true" '
            f'data-bs-placement="bottom" '
            f'title="{stamp} UTC"'
            f">"
            f'<i class="fa-solid fa-calendar-days"></i> '
            f"{stamp} UTC"
            f"</span>"
        )

    # naive 一律按 UTC 解释 —— 与 redemption_service / _to_rfc3339 同一约定
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)

    local = value.astimezone(_DISPLAY_TZ)
    stamp = local.strftime("%d %B %Y %H:%M:%S")

    return Markup(
        f"<span "
        f'class="my-1 py-1 px-2 badge bg-secondary text-light '
        f'lead d-inline-block text-truncate" '
        f'data-bs-toggle="tooltip" '
        f'data-bs-html="true" '
        f'data-bs-placement="bottom" '
        f'title="{stamp} {DISPLAY_TZ_NAME}"'
        f">"
        f'<i class="fa-solid fa-calendar-days"></i> '
        f"{stamp} {DISPLAY_TZ_NAME}"
        f"</span>"
    )


#: 类型格式化表。必须 ``dict(...)`` 复制一份：``ModelView.column_type_formatters``
#: 就是 ``sqladmin.formatters.BASE_FORMATTERS`` 这个**共享对象**，就地写入会污染
#: 上游快照的全局状态。
#:
#: 为什么要显式登记：内置的 ``BASE_FORMATTERS`` 只有 ``type(None)`` 与 ``bool``
#: 两个键，并不包含 ``datetime`` —— 上游自带的 ``datetime_formatter`` 因此从未
#: 被调用过，datetime 列会走 ``_default_formatter`` 的父类回退并被原样 ``str()``，
#: 这正是本次修复前「早 8 小时」的直接成因。
#:
#: 挂在 ``column_type_formatters`` 上即同时作用于列表页与详情页：SQLAdmin 在
#: ``column_type_formatters_detail`` 保持默认时会自动沿用本表。
DISPLAY_TZ_FORMATTERS = dict(ModelView.column_type_formatters)
DISPLAY_TZ_FORMATTERS[_dt.datetime] = _datetime_formatter


def _require_operator(request) -> bool:
    """所有后台视图共用的授权判定。

    SQLAdmin 自身不做鉴权假设；这里与 AdminAuth 的会话判定保持一致，
    使内置页面、@action、@expose 三条路径受到同一套控制。
    """
    return AdminAuth.is_authenticated(request)


def _truncate(value, length: int = 16) -> str:
    """截断长标识用于列表展示（不用于任何机密明文）。"""
    text = value or ""
    return text if len(text) <= length else text[:length] + "…"


def _persist_flashes(request) -> None:
    """确保本次请求写入的 Flash 消息真的会发给浏览器。

    上游 sqladmin 0.31.1 的 ``flash()`` 是这样写的::

        if "_messages" not in request.session:
            request.session["_messages"] = []
        request.session["_messages"].append({...})

    第二次写入是**就地 append**，不经过 ``Session.__setitem__``。
    Starlette 的 ``Session`` 只在 ``__setitem__`` / ``__delitem__`` / ``pop`` /
    ``update`` / ``setdefault`` 上置 ``modified``；``SessionMiddleware`` 又只在
    ``session.modified`` 为真时才下发 Set-Cookie。

    于是在「请求开始时 ``_messages`` 已存在」这种情况下（上一次响应没有渲染页面
    因而没被 ``get_flashed_messages`` 取走），新消息不会写回 Cookie，用户就再也
    看不到这次操作的反馈 —— 包括「该 Key 已兑换、未吊销」这类必须传达的提示。

    这里把列表重新赋值一次，等价于触发 ``__setitem__``，数据内容不变。
    只使用 Starlette 会话与 SQLAdmin ``Flash`` 的公开行为，不修改内置快照。
    """
    session = request.scope.get("session")
    if session is not None and "_messages" in session:
        session["_messages"] = list(session["_messages"])


#: License 状态 → 面向运维的语义说明。
#: 直接写进列表/详情，确保「能否吊销」在 UI 上不会被误读。
LICENSE_STATE_LABEL = {
    LicenseState.UNUSED: "未使用 · 可吊销",
    LicenseState.REDEEMED: "已兑换 · 不可吊销（吊销不会收回既有授权）",
    LicenseState.REVOKED: "已吊销",
}


def _format_license_state(obj, prop) -> str:
    """列表/详情中的状态列文案（SQLAdmin 允许 formatter 返回纯文本）。"""
    return LICENSE_STATE_LABEL.get(obj.state, str(obj.state))


#: V1 约定的有效期预设（天）。生成页只提供这几个选项；
#: 服务端按同一份白名单校验，因此绕过表单提交任意时长也不会生效。
DURATION_PRESETS = (1, 30, 180, 365)

#: V1 单次批量生成的上限。防止误操作/恶意提交触发一次超大写入，
#: 也让一次请求的明文结果仍可人工分发。服务端强校验，不依赖表单 max 属性。
MAX_BATCH_QUANTITY = 1000


def _parse_int(raw: str) -> int | None:
    """把表单字段解析为 int；解析不出来返回 None（调用方按无效处理）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _create_license_batch(
    license_data: "LicenseCreate",
    quantity: int,
    features: list[str],
) -> tuple[list[str], int | None, str | None]:
    """在一个事务内生成 quantity 个 License Key。

    返回 ``(明文列表, 天数, 错误信息)``。成功时错误信息为 None；
    失败时明文列表为空 —— 绝不返回半批结果。

    每个 Key 都通过既有 ``LicenseService.create_license`` 生成，
    因此哈希方式、初始状态、存储规则与单生成完全一致。
    """
    keys: list[str] = []
    db = database.SessionLocal()
    try:
        service = LicenseService(db)
        for _ in range(quantity):
            # commit=False：只 flush，事务留到整批结束后统一提交。
            license_obj = service.create_license(
                license_data, features=features or None, commit=False
            )
            keys.append(license_obj.license_key)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 需要回滚整批并给运维一个可读提示
        db.rollback()
        # 只带出异常类型名：不回显明文，也不把哈希写进页面。
        return [], None, (
            "批量生成失败，本次没有写入任何 License Key（整个事务已回滚）。"
            f"（错误类型：{type(exc).__name__}）"
        )
    finally:
        db.close()

    return keys, license_data.duration_days, None


# ======================================================================
# License Key
# ======================================================================
class LicenseAdmin(ModelView, model=License):
    """License Key 的列表 / 详情 / 吊销。

    刻意关闭 create/edit/delete：
        - create：key_hash 由 Business Layer 生成，内置表单不会生成它；
        - edit  ：Key 的状态机只应由兑换 / 吊销驱动，不应手工改写；
        - delete：审计上不应抹掉签发记录。
    """

    name = "License Key"
    name_plural = "License Keys"
    icon = "fa-solid fa-key"
    category = "Licensing"

    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    can_export = True

    #: created_at / redeemed_at 的东八区展示（存储与业务计算仍为 UTC）。
    column_type_formatters = DISPLAY_TZ_FORMATTERS

    #: 列表列。明文 license_key 不是数据库列，故此处只能展示 key_hash 指纹。
    column_list = [
        "id",
        "key_hash",
        "duration_days",
        "state",
        "created_at",
        "redeemed_at",
        "authorization_id",
    ]
    column_details_list = [
        "id",
        "key_hash",
        "duration_days",
        "state",
        "features",
        "created_at",
        "redeemed_at",
        "authorization_id",
    ]

    column_labels = {
        License.id: "ID",
        License.key_hash: "Key 指纹 (SHA-256)",
        License.duration_days: "时长（天）",
        License.state: "状态",
        License.features: "功能 (JSON)",
        License.created_at: "创建时间",
        License.redeemed_at: "兑换时间",
        License.authorization_id: "Authorization ID",
    }

    #: formatter 的调用约定是 (obj, prop)（见 sqladmin/models.py 的 _call_formatter）。
    column_formatters = {
        License.key_hash: lambda obj, prop: _truncate(obj.key_hash),
        License.state: _format_license_state,
    }
    column_formatters_detail = {
        License.state: _format_license_state,
    }

    #: 明文 Key 无法反查：检索只能按指纹前缀进行。
    column_searchable_list = ["key_hash"]
    column_default_sort = [(License.created_at, True)]

    #: 状态过滤。使用 SQLAdmin 自带的 StaticValuesFilter（无 EnumFilter）。
    column_filters = [
        StaticValuesFilter(
            License.state,
            values=[
                (LicenseState.UNUSED.value, "未使用"),
                (LicenseState.REDEEMED.value, "已兑换"),
                (LicenseState.REVOKED.value, "已吊销"),
            ],
            title="状态",
        )
    ]

    def is_accessible(self, request) -> bool:
        return _require_operator(request)

    # ------------------------------------------------------------------
    # 吊销（V1：仅 UNUSED）
    # ------------------------------------------------------------------
    @action(
        "revoke",
        label="吊销所选 Key",
        confirmation_message=(
            "仅「未使用」的 License Key 会被吊销。"
            "已兑换的 Key 不会被吊销，其设备上的既有授权也不会被收回。是否继续？"
        ),
        add_in_list=True,
        add_in_detail=True,
    )
    async def revoke(self, request):
        """吊销所选 License Key。

        业务规则（既有 Business Layer 决定，本视图不重复判断）：
            - UNUSED  → ``LicenseService.revoke_license`` 置为 REVOKED，
                        该 Key 之后无法再被兑换；
            - REDEEMED→ 不处理。吊销一个已兑换的 Key 不会收回设备上已经
                        生效的 Authorization，因此 UI 不得暗示它会；
            - REVOKED → 已是终态，不重复处理。
        """
        raw_pks = [
            pk.strip()
            for pk in request.query_params.get("pks", "").split(",")
            if pk.strip()
        ]
        if not raw_pks:
            Flash.warning(request, "未选择任何 License Key，未做任何改动。")
            return self._back(request)

        revoked: list[str] = []
        not_revocable: list[str] = []
        already_revoked: list[str] = []
        missing: list[str] = []

        with database.SessionLocal() as db:
            service = LicenseService(db)
            for raw_pk in raw_pks:
                if not raw_pk.isdigit():
                    missing.append(raw_pk)
                    continue

                license_obj = (
                    db.query(License).filter(License.id == int(raw_pk)).first()
                )
                if license_obj is None:
                    missing.append(raw_pk)
                    continue

                if license_obj.state is LicenseState.UNUSED:
                    service.revoke_license(license_obj.id)
                    revoked.append(f"#{license_obj.id}")
                elif license_obj.state is LicenseState.REDEEMED:
                    not_revocable.append(f"#{license_obj.id}")
                else:
                    already_revoked.append(f"#{license_obj.id}")

        if revoked:
            Flash.success(request, f"已吊销：{'、'.join(revoked)}。")
        if not_revocable:
            Flash.warning(
                request,
                f"未吊销：{'、'.join(not_revocable)} —— 这些 Key 已兑换。"
                "吊销已兑换的 Key 不会收回设备上已经生效的授权，"
                "本后台也不提供授权收回功能。",
                title="已兑换的 Key 不可吊销",
            )
        if already_revoked:
            Flash.info(request, f"已是吊销状态，未重复处理：{'、'.join(already_revoked)}。")
        if missing:
            Flash.error(request, f"未找到对应记录：{'、'.join(missing)}。")

        return self._back(request)

    def _back(self, request):
        """动作结束后回到来源页，否则回到本视图列表页。

        显式使用 303（See Other）：动作虽然是 GET，但语义上是「做完一件事、
        去看结果」，303 能确保浏览器改用 GET 取目标页，避免重放该动作。
        """
        _persist_flashes(request)
        referer = request.headers.get("Referer")
        target = referer or request.url_for("admin:list", identity=self.identity)
        return RedirectResponse(target, status_code=303)


# ======================================================================
# 兑换历史
# ======================================================================
# 诚实说明：
#   现有 schema **没有**独立的事件日志表。「兑换历史」只能由现有的
#   ``License`` 行派生 —— 即 state == REDEEMED 的那些行，其 ``redeemed_at``
#   是兑换时刻，``authorization_id`` 指向被授权的设备。
#   它不记录客户端的每次校验、续期或其它事件；真正的不可变事件日志需要新表，
#   属于后续阶段。
#
# 为什么这里不是第二个 ModelView：
#   ``ModelViewMeta`` 无条件用模型名推导 identity（sqladmin/models.py:111
#   ``cls.identity = slugify_class_name(model.__name__)``），会覆盖类属性。
#   因此同一个模型上无法挂两个 ModelView —— 路由与 ``admin:list`` 端点名会冲突。
#   在 BaseView 里重写一遍列表，则会重复 SQLAdmin 已有的分页/排序/导出。
#   所以这里只提供一个导航入口，重定向到既有的 License 列表并预先套用状态过滤。
#   过滤参数名由 ``get_parameter_name(License.state)`` 决定，即列名 ``state``。
class RedeemedHistoryView(BaseView):
    """「兑换历史」导航入口 —— 指向已按 state=REDEEMED 过滤的 License 列表。"""

    name = "兑换历史"
    icon = "fa-solid fa-clock-rotate-left"
    category = "Licensing"

    def is_accessible(self, request) -> bool:
        return _require_operator(request)

    @expose("/redeemed-history")
    async def redeemed_history(self, request):
        list_url = str(request.url_for("admin:list", identity="license"))
        return RedirectResponse(
            f"{list_url}?state={LicenseState.REDEEMED.value}",
            status_code=302,
        )


# ======================================================================
# Authorization（只读）
# ======================================================================
class AuthorizationAdmin(ModelView, model=Authorization):
    """设备当前的 Authorization —— 只读。

    V1 不提供吊销 / 收回：现有 Business Layer 没有任何代码路径会设置
    ``AuthorizationState.REVOKED``（``redeem_license`` 只在读取时拒绝它）。
    新增该能力属于新增业务逻辑，不在本阶段范围内。
    """

    name = "Authorization"
    name_plural = "Authorizations"
    icon = "fa-solid fa-desktop"
    category = "Licensing"

    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    can_export = True

    #: expires_at / created_at / activated_at 的东八区展示
    #: （存储与业务计算仍为 UTC）。
    column_type_formatters = DISPLAY_TZ_FORMATTERS

    column_list = [
        "id",
        "device_id",
        "state",
        "expires_at",
        "created_at",
        "activated_at",
    ]
    #: 详情页额外展示 ``licenses`` 关系 —— 该设备兑换过的 Key 列表，
    #: 即「按设备看的兑换历史」。SQLAdmin 识别 mapper 上的 relationship
    #: （sqladmin/models.py:788），无需额外代码。
    column_details_list = [
        "id",
        "device_id",
        "state",
        "expires_at",
        "created_at",
        "activated_at",
        "licenses",
    ]

    column_labels = {
        Authorization.id: "ID",
        Authorization.device_id: "设备标识",
        Authorization.state: "状态",
        Authorization.expires_at: "到期时间",
        Authorization.created_at: "创建时间",
        Authorization.activated_at: "激活时间",
        Authorization.licenses: "该设备兑换过的 License",
    }

    column_formatters = {
        Authorization.device_id: lambda obj, prop: _truncate(obj.device_id),
    }

    column_searchable_list = ["device_id"]
    column_default_sort = [(Authorization.expires_at, True)]

    def is_accessible(self, request) -> bool:
        return _require_operator(request)


# ======================================================================
# License Key 生成
# ======================================================================
class GenerateKeysView(BaseView):
    """生成新的 License Key（支持一次生成多个）。

    为什么不用 SQLAdmin 内置的 create 表单：
        明文 Key 由 Business Layer 生成，且**从不落库**（只存 SHA-256）。
        内置表单不会生成 ``key_hash``，提交会写入 NULL 而失败。
        因此生成必须走本页，并复用既有 ``LicenseService.create_license``。

    单一实现：数量为 1 时同样走批量代码路径（quantity=1），
    因此不存在「单生成」与「批生成」两套逻辑。

    事务性：
        整个批次在**一个事务**内写入（``create_license(commit=False)`` 只 flush），
        循环结束后统一 commit；任何一步失败就 rollback，
        不会在库里留下半批数据。

    明文展示：
        明文只在本请求的内存对象与本次响应体内存在。不入会话、不写 cookie、
        不落库、不写日志；响应带 no-store 头。
        （V1 起改为页面内联结果面板，因为一次性弹窗只能承载单个值。）
    """

    name = "生成 License Key"
    icon = "fa-solid fa-plus"
    category = "Licensing"

    def is_accessible(self, request) -> bool:
        return _require_operator(request)

    @expose("/generate-keys", methods=["GET", "POST"])
    async def generate_keys(self, request):
        error = None
        generated_days = None
        generated_keys: list[str] = []
        submitted_quantity = 1

        #: 服务端**实际收到**的 quantity 原始字符串（未经解析/未做默认值替换）。
        #: 只用于在结果页如实回显「浏览器到底发了什么」，便于把
        #: 「前端发错」与「后端生成错」两类问题当场区分开。
        received_quantity_raw = None

        if request.method == "POST":
            form = await request.form()
            raw_days = str(form.get("duration_days") or "").strip()
            raw_quantity = str(form.get("quantity") or "").strip()
            raw_features = str(form.get("features") or "").strip()
            received_quantity_raw = raw_quantity

            days = _parse_int(raw_days)
            quantity = _parse_int(raw_quantity)
            if quantity is not None:
                submitted_quantity = quantity

            features = [f.strip() for f in raw_features.split(",") if f.strip()]

            if days not in DURATION_PRESETS:
                error = (
                    "请选择有效期："
                    + "、".join(str(d) for d in DURATION_PRESETS)
                    + " 天。"
                )
            elif quantity is None or quantity < 1:
                error = f"数量必须是 1 到 {MAX_BATCH_QUANTITY} 之间的整数。"
            elif quantity > MAX_BATCH_QUANTITY:
                error = f"单次最多生成 {MAX_BATCH_QUANTITY} 个 License Key。"
            else:
                try:
                    # 仍以既有 schema 构造入参，业务层保持权威校验。
                    license_data = LicenseCreate(duration_days=days)
                except ValidationError:
                    error = (
                        "有效期不在允许范围内："
                        + "、".join(str(d) for d in DURATION_PRESETS)
                        + " 天。"
                    )
                else:
                    generated_keys, generated_days, error = _create_license_batch(
                        license_data, quantity, features
                    )
                    # 一致性自检：请求数量与实际写入数量必须逐条相等。
                    # _create_license_batch 已经是「要么全写、要么全不写」，
                    # 这里把该不变量显式断言出来 —— 一旦将来有人改动批处理路径
                    # 引入静默降级（例如只写 1 条），结果页会直接报错，
                    # 而不是显示一个看起来正常的数字。
                    if error is None and len(generated_keys) != quantity:
                        error = (
                            f"内部一致性错误：本次请求生成 {quantity} 个 Key，"
                            f"但实际只写入了 {len(generated_keys)} 个。"
                            "请立即检查数据库状态。"
                        )

        response = await self.templates.TemplateResponse(
            request,
            "generate_keys.html",
            {
                "title": "生成 License Key",
                "subtitle": "明文仅显示一次",
                "error": error,
                "duration_presets": DURATION_PRESETS,
                "selected_days": generated_days or 30,
                "max_quantity": MAX_BATCH_QUANTITY,
                "selected_quantity": submitted_quantity,
                "received_quantity_raw": received_quantity_raw,
                "generated_keys": generated_keys,
                # 结果框高度：够看几行，最多 20 行，避免整页被撑开。
                "result_rows": min(max(len(generated_keys), 4), 20),
            },
        )
        # 自定义视图需自行加防缓存头（内置 create/edit 由 SQLAdmin 自动处理）。
        Secret.apply_no_store_headers(response)
        return response


# ======================================================================
# DAU（只读聚合，Phase 7.4）
# ======================================================================
#: 本页展示的最近天数。按日聚合后每天一行，30 天足够看趋势；
#: 更早的历史随时可用 SQL 直接查 device_daily_active，不在本页提供。
DAU_PAGE_DAYS = 30


class DauView(BaseView):
    """每日活跃设备数 —— 只读。

    为什么不是 ModelView：``device_daily_active`` 每行是「一台设备的一天」，
    而 DAU 要的是「一天有多少台设备」—— 这需要 GROUP BY 聚合，而 ModelView
    只会逐行渲染模型对象，做不到。所以沿用本项目已有的 ``BaseView`` + 自带
    模板写法（与 ``GenerateKeysView`` 同构）。

    本页没有任何写入口，也没有吊销/删除能力：DAU 是纯统计视图。

    口径：``device_id`` 是设备机器码，因此这里统计的是**活跃设备数**，
    不是自然人数。同一人多台设备会各计一次。
    """

    name = "DAU"
    icon = "fa-solid fa-chart-line"
    category = "Licensing"

    def is_accessible(self, request) -> bool:
        return _require_operator(request)

    @expose("/dau")
    async def dau(self, request):
        with database.SessionLocal() as db:
            # 按业务时区日历日聚合。归属已经在写入时定好（见 heartbeat_service），
            # 这里只是计数，不做任何时区换算。
            rows = (
                db.query(
                    DeviceDailyActive.active_date.label("active_date"),
                    sa_func.count().label("devices"),
                    sa_func.sum(DeviceDailyActive.launch_count).label("launches"),
                )
                .group_by(DeviceDailyActive.active_date)
                .order_by(DeviceDailyActive.active_date.desc())
                .limit(DAU_PAGE_DAYS)
                .all()
            )

            # 累计去重设备数：与上面的按日计数不同，这里跨天去重。
            total_devices = db.query(
                sa_func.count(sa_distinct(DeviceDailyActive.device_id))
            ).scalar()

        response = await self.templates.TemplateResponse(
            request,
            "dau.html",
            {
                "title": "DAU",
                "subtitle": "每日活跃设备数",
                "rows": rows,
                "total_devices": total_devices or 0,
                "days": DAU_PAGE_DAYS,
                "tz_name": DISPLAY_TZ_NAME,
            },
        )
        Secret.apply_no_store_headers(response)
        return response
