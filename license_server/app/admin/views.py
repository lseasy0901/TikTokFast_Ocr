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

from pydantic import ValidationError
from sqladmin import BaseView, Flash, ModelView, Secret, action, expose
from sqladmin.filters import StaticValuesFilter
from starlette.responses import RedirectResponse

import database
from admin.auth import AdminAuth
from models import Authorization, License, LicenseState
from schemas import LicenseCreate
from services.license_service import LicenseService


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
