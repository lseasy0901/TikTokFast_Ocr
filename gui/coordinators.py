# -*- coding: utf-8 -*-
"""MainWindow 协调器(Phase 2 拆分,自 gui/main_window.py 外迁)。

搬迁模式:方法体整体移动,窗口引用经 self._w;协调器持有领域状态字段。
MainWindow 通过 __getattr__ 把对这些字段/方法的访问转发到协调器——
tests 直接引用 window._license_manager / window._apply_license_result 等,
转发层保证既有断言零改动(方案:docs/refactor/main_window-split-plan.md)。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from utils import update_state
from gui.activation_dialog import LicenseWorker
from gui.onboarding import (DIALOG_START_FOR, GUIDE_ADVANCE, GUIDE_ALWAYS,
                            STEPS, SPOTLIGHT_STEPS,
                            GuideToast)
from gui.update_dialog import UpdateDialog, UpdateError
from gui.widgets import set_tone

#: 更新检查的启动延迟(自 main_window 外迁):避开启动高峰抢资源
UPDATE_CHECK_DELAY_MS = 5000

logger = logging.getLogger("DouyinLowLatencyViewer.gui.coordinators")


def _mw():
    """运行时取 gui.main_window 模块。

    测试通过 `mw.OnboardingDialog = _Stub` 这类**窗口模块级打桩**替换 UI
    类(见 test_first_run_onboarding._run_dialog_with);实现侧必须在调用
    点经窗口模块命名空间解析这些类,桩才能生效。延迟到调用时取模块也
    避免循环导入(coordinators 被 main_window 导入)。
    """
    import sys

    return sys.modules["gui.main_window"]

from utils.access_status import AccessStatus
from utils.license_manager import LicenseManager, LicenseResult, LicenseStatus


class _CoordinatorBase:
    """共同骨架:窗口引用 + 跨协调器访问的受限转发。

    跨组引用真实存在(引导流程读授权状态、激活成功触发引导),转发规则:
    本协调器未命中的名字 → 窗口的**常规属性**(object.__getattribute__,
    刻意不触发窗口 __getattr__,否则与窗口→协调器的转发形成无限回环)
    → 兄弟协调器。全部未命中才 AttributeError。
    """

    def __init__(self, window):
        self._w = window

    def __getattr__(self, name):
        w = self.__dict__.get("_w")
        if w is None:
            raise AttributeError(name)
        try:
            return object.__getattribute__(w, name)
        except AttributeError:
            pass
        for coord_name in ("_license_coord", "_guide_coord", "_update_coord"):
            coord = w.__dict__.get(coord_name)
            if coord is None or coord is self:
                continue
            try:
                return object.__getattribute__(coord, name)
            except AttributeError:
                continue
        raise AttributeError(
            f"{type(self).__name__} has no attribute {name!r}")


class LicenseCoordinator(_CoordinatorBase):
    """领域协调器(职责见各方法 docstring)。"""

    def __init__(self, window):
        super().__init__(window)
        # 字段落**窗口实例字典**(不放协调器):测试会整体替换
        # window._first_run_store / window._license_manager 等并直接断言,
        # 字段在窗口上,协调器方法经基类受限转发读到的才是同一份。
        w = self._w
        w._access_status = AccessStatus()
        w._access_timer = QTimer(w)
        w._access_timer.timeout.connect(self._on_access_timer_timeout)
        w._access_status.set_update_timer(w._access_timer)
        w._access_status.set_callbacks(self._on_access_status_changed,
                                       self._on_access_expired)
        from utils.heartbeat_schedule import HeartbeatScheduler

        w._license_manager = LicenseManager()
        w._license_load_worker = None
        w._heartbeat_worker = None
        w._heartbeat_scheduler = HeartbeatScheduler()
        w._heartbeat_timer = QTimer(w)
        w._heartbeat_timer.setSingleShot(True)
        w._heartbeat_timer.timeout.connect(self._on_heartbeat_timer_timeout)
        w._expiry_toast_shown = False


    def _on_access_timer_timeout(self):
        """访问状态专用定时器回调（低频）。

        用于发现「应用运行中到期」：状态到期后无需重启即可生效。
        """
        self._access_status.refresh()


    def _on_access_status_changed(self):
        """访问状态变化回调：更新文案，并把限制反映到控件可用状态。

        只影响「能否发起新动作」，绝不主动断开已建立的视频连接 ——
        到期后仍保留画面与「断开」入口，避免把用户困在播放中。
        """
        status_text = self._access_status.get_status_text()
        self._w._access_status_label.setText(status_text)

        denied = self._access_status.is_expired()

        # 访问状态是「能不能用」的总开关，语气必须比普通读数更醒目：
        # 已到期用危险色，临近到期用警告色，正常则保持低调。
        days_left = self._access_status.remaining_days()
        if denied:
            tone = "error"
        elif "即将到期" in status_text:
            tone = "warn"
        elif days_left is not None and days_left <= 7:
            tone = "warn"          # 天级续费提醒(2026-10-25):临期一周变黄
        else:
            tone = "ok"
        set_tone(self._w._access_status_label, tone)
        if tone == "warn" and days_left is not None:
            self._w._access_status_label.setToolTip(
                f"授权将在 {days_left} 天后到期,请及时续费")
            # 每次启动只提醒一次(toast),避免状态轮询反复弹
            if not getattr(self, "_expiry_toast_shown", False):
                self._w._expiry_toast_shown = True
                self._w.show_toast(
                    f"授权剩余 {days_left} 天,请及时续费", "warn")
        else:
            self._w._access_status_label.setToolTip("")

        # 激活入口始终可用：任何状态下都应允许用户激活许可证
        self._w._license_btn.setEnabled(True)

        if not self._w._connected:
            # 未连接：把受限的入口可见地关掉
            self._w._connect_btn.setEnabled(not denied)
            self._w._url_input.setEnabled(not denied)
        elif denied and self._w._ocr_running:
            # 已连接：到期后停止正在运行的 OCR（不主动断开视频流）
            logger.info("访问已到期，停止正在运行的 OCR")
            self._w._stop_ocr()

        self._w._roi_btn.setEnabled(self._w._connected and not denied)
        self._w._update_ocr_button_state()

        # 未激活遮罩跟随同一份状态（只影响显示）
        self._sync_activation_overlay()

        # 未激活动态水印（Phase 5）：与遮罩消费**同一个** denied 布尔量，
        # 不新建平行的授权判断，也不在别处推断激活状态。
        self._sync_watermark(denied)


    def _sync_watermark(self, denied: bool | None = None) -> None:
        """按既有授权状态同步水印显隐（只影响显示，不改变任何状态）。

        与 :meth:`_sync_activation_overlay` 同一套语义：状态从
        ``_access_status`` 读取，水印组件只负责显示与运动。
        """
        overlay = getattr(self, "_watermark", None)
        if overlay is None:
            return
        if denied is None:
            denied = self._access_status.is_expired()
        overlay.set_denied(denied)


    def _on_access_expired(self):
        """访问过期回调"""
        # 显示过期对话框
        dialog = _mw().ExpiredDialog(self._w)
        dialog.exec()

    # ------------------------------------------------------------------
    # 许可证激活（Phase 7.2-6.5）
    # ------------------------------------------------------------------
    def _start_license_load(self):
        """后台加载并本地验证已保存的许可证，不阻塞 GUI。"""
        self._w._license_load_worker = LicenseWorker(
            task=self._license_manager.load_stored,
            on_done=self._w._license_loaded_sig.emit,
            name="license-startup",
        )
        self._license_load_worker.start()


    def _on_license_loaded(self, result: LicenseResult):
        """启动恢复结果（经 Qt 排队回到 GUI 线程）。"""
        logger.info("启动许可证校验结果: %s", result.status.value)
        self._apply_license_result(result)
        # 恢复出有效授权 → 这才是「已授权用户启动」这条路径的判定点。
        # 早于此刻调用只会读到"尚未读到凭据"，因此引导被推迟到这里。
        self.maybe_show_first_run_guide()


    def _on_license_appended(self, result: LicenseResult):
        """设置中心里追加授权成功后：与主入口同一套状态应用 + 解锁过渡。"""
        self._apply_license_result(result)
        self._play_unlock_transition()
        # 追加授权同样意味着"进入可使用状态"，未授权用户走这条路也该看到引导
        self.maybe_show_first_run_guide()

    # ------------------------------------------------------------------
    # 未激活遮罩 / 解锁过渡
    # ------------------------------------------------------------------
    def _sync_activation_overlay(self) -> None:
        """按**既有授权状态**决定是否显示未激活遮罩（不改变任何状态）。"""
        if getattr(self, "_activation_overlay", None) is None:
            return
        denied = self._access_status.is_expired()
        if denied:
            self._w._activation_overlay.reset_prompt()
        else:
            # 遮罩要收起了 —— 遮罩入口打开的卡片不能留在屏幕上
            self._w._close_contact_popup()
        self._w._activation_overlay.setVisible(denied)
        if denied:
            self._w._activation_overlay.raise_()


    def _play_unlock_transition(self) -> None:
        """成功激活后的轻量解锁过渡：约 1.4 秒后恢复普通预览。

        刻意**不**自动连接直播、不启动 OCR —— 只做视觉过渡。
        """
        if getattr(self, "_activation_overlay", None) is None:
            return
        # 授权已经成功：卡片若还开着就立刻收掉，别让它浮在解锁过渡之上
        self._w._close_contact_popup()
        self._w._activation_overlay.setVisible(True)
        self._w._activation_overlay.show_unlocked()
        QTimer.singleShot(1400, self._finish_unlock_transition)


    def _finish_unlock_transition(self) -> None:
        self._sync_activation_overlay()
        # 与 _on_access_status_changed 同一套顺序：遮罩先同步（它会 raise_()），
        # 水印随后同步（同样 raise_()），保证水印不会被遮罩盖住。
        self._sync_watermark()


    def _apply_license_result(self, result: LicenseResult):
        """把许可证结果映射到顶部状态显示。"""
        if result.status is LicenseStatus.NOT_ACTIVATED:
            # 本地没有许可证：未激活即拒绝使用（免费试用已移除）
            self._access_status.clear_license_state()
        elif result.status is LicenseStatus.ACTIVATED:
            # 有效期直接来自服务器签名的凭据，客户端不做时长计算
            self._access_status.set_license_active(result.expires_at)
        else:
            self._access_status.set_license_inactive(result.display_text)

    # ------------------------------------------------------------------
    # 启动心跳（Phase 7.4，DAU 统计）
    #
    # 这是一条纯遥测链路，与授权流程严格隔离：
    #   - 上报逻辑在 LicenseManager.heartbeat()，它永不抛异常、永不改状态；
    #   - 调度逻辑在 utils.heartbeat_schedule.HeartbeatScheduler（纯本地计算）；
    #   - 这里只负责起后台线程与摆弄定时器，绝不调用 _apply_license_result，
    #     也绝不触碰 AccessStatus。
    # 因此心跳无论如何失败，都不会影响用户看到的授权状态。
    #
    # 节奏：启动立即上报一次；之后对齐**下一个业务日边界**，且每次触发后
    # 重新计算下一次延时。为什么不是固定周期，见 heartbeat_schedule 模块说明。
    # ------------------------------------------------------------------
    def start_heartbeat(self):
        """开启心跳上报。由应用入口 main.py 调用。

        为什么不在 __init__ 里自动开始：
            心跳会向许可证服务器发起**真实网络请求**，而「构造一个 MainWindow」
            在测试里是极常见的动作（多个 GUI 测试直接实例化窗口）。若把它挂在
            构造流程上，每次跑测试都会向生产服务器发一次心跳，污染真实 DAU。
            遥测属于「应用启动了」这件事，不属于「窗口对象被构造了」这件事。

        授权流程不受影响：本方法只依赖 LicenseManager.heartbeat()，它永不抛异常、
        永不改状态；结果槽也只用于推进调度状态。

        重复调用是安全的：上一次上报仍在进行时不会堆叠线程。
        """
        self._start_heartbeat()


    def _start_heartbeat(self):
        """后台上报一次心跳，不阻塞 GUI。"""
        # 上一次仍在跑就跳过，避免网络慢时堆叠线程
        if self._heartbeat_worker is not None and self._heartbeat_worker.is_running():
            return
        self._w._heartbeat_worker = LicenseWorker(
            task=self._license_manager.heartbeat,
            on_done=self._w._heartbeat_done_sig.emit,
            name="license-heartbeat",
        )
        self._heartbeat_worker.start()


    def _schedule_next_heartbeat(self):
        """按调度器算出的延时重新装上定时器。

        每次都现算，而不是沿用固定周期 —— 这是"不累积 QTimer 漂移"的落点。
        """
        delay_ms = self._heartbeat_scheduler.next_delay_ms(datetime.now(timezone.utc))
        if delay_ms is None:
            # 业务时区不可用：保留启动那一次上报，但不做周期补发。
            # 刻意不猜一个固定偏移 —— 猜错会把心跳记到服务端的另一天，
            # 比"不补发"更糟。此处只在启动上报结束时走到一次，不会刷屏。
            logger.warning("心跳周期调度不可用（业务时区无法解析），已停止补发")
            return
        self._heartbeat_timer.start(delay_ms)


    def _on_heartbeat_timer_timeout(self):
        """定时器到点：业务日若已推进就补报，然后重新对齐下一个边界。

        watchdog 会周期性地把这里叫醒（见 WATCHDOG_MAX_MS），因此有两种情况：
        当前业务日已上报过 -> 什么都不做，只重新对齐；
        业务日已推进（含休眠跨天后唤醒）-> 补报这一天。
        """
        now = datetime.now(timezone.utc)

        if not self._heartbeat_scheduler.should_report(now):
            self._schedule_next_heartbeat()
            return

        if self._heartbeat_worker is not None and self._heartbeat_worker.is_running():
            # 上一次还没回来：稍后再来，不堆叠线程
            self._schedule_next_heartbeat()
            return

        self._start_heartbeat()


    def _on_heartbeat_done(self, credited_day):
        """心跳结束回调（经 Qt 排队回到 GUI 线程）。

        只做两件事：推进调度状态、重新装定时器。绝不触碰授权状态显示 ——
        心跳是遥测，失败不该有任何界面反馈。

        ``credited_day`` 是**服务端**返回的 ``active_date``（见
        LicenseServerClient.heartbeat），即服务端本次实际记账的业务日。

        刻意**不**用"收到响应时的本地日期"来推断已上报的业务日：请求贴着
        业务日边界发出、响应跨过边界时两端会差一天，那样会把下一天误记为
        已上报，而服务端只记了前一天 —— 下一天于是永远不会被上报。
        记账权威在服务端，这里只原样转交。

        类型判断而不是真值判断：LicenseWorker 在任务抛异常时会回调一个
        LicenseResult 兜底对象，它既不是 None 也不是 date，绝不能被当成
        "某一天已上报"；非 date 一律视为未上报，保持 watchdog 重试。

        显式排除 ``datetime``：它是 ``date`` 的子类，会通过 isinstance 检查，
        但一旦混进 ``_reported_day``，"业务日是否已上报"的比较就永远为不等，
        调度器会退化成每 15 分钟无限重试。只接受纯 date。
        """
        if isinstance(credited_day, date) and not isinstance(credited_day, datetime):
            self._heartbeat_scheduler.mark_reported_day(credited_day)
        self._schedule_next_heartbeat()

class GuideCoordinator(_CoordinatorBase):
    """领域协调器(职责见各方法 docstring)。"""

    def __init__(self, window):
        super().__init__(window)
        from utils.first_run import FirstRunStore

        w = self._w
        w._first_run_store = FirstRunStore()
        w._first_run_guide_done = False
        w._guide_spotlight = None
        w._guide_panel_restore_to = None
        w._guide_panel_touched = False
        w._guide_toast = None
        w._guide_review = False

    # ------------------------------------------------------------------
    # 首次启动引导 / 帮助
    # ------------------------------------------------------------------
    def maybe_show_first_run_guide(self) -> bool:
        """在「已获得有效授权且尚未看过引导」时弹出使用引导。

        触发时机（Phase UI 微调 08）：
            - 未授权启动：**不弹**。先让用户完成授权，引导的意义是介绍一个
              他能真正使用的软件，对着未激活界面讲操作流程是无效信息。
            - 启动时已是授权用户：由 main.py 在主窗口显示后调用这里；
              授权状态由后台线程读盘得出，所以先等它落地再判定。
            - 激活 / 追加授权成功：由 _on_license_clicked / _on_license_appended
              在解锁过渡之后再次调用，这是未授权用户真正该看到引导的时刻。

        仍然刻意由入口而不是 __init__ 调用：「构造一个 MainWindow」在测试里很
        常见，构造期弹模态对话框会让无头测试永久阻塞。

        重入与重复防护（三道，缺一不可）：
            1. _onboarding_dialog 非空 → 嵌套事件循环里的重入
            2. _first_run_guide_done → 同一会话里多入口重复触发
            3. should_show_guide() → 用户明确选择过「不再自动显示」
        注意第 3 条挡不住第 2 条描述的情况：用户在最后一页取消勾选时 prefs
        不会写入，只靠它会让同一个会话弹出第二次。

        :return: True 表示本次确实弹出了向导
        """
        if self._w._onboarding_dialog is not None:
            return False
        if self._first_run_guide_done:
            return False
        if not self._first_run_store.should_show_guide():
            return False

        # 授权状态可能还在后台线程里读盘。等它落地再判定，否则「已授权用户
        # 启动」这条路径会因为此刻还没读到凭据而被误判成未授权。
        self._wait_for_license_load()

        # 等待期间会泵事件，其它入口（_on_license_loaded / 激活成功）完全可能
        # 在这个窗口内把引导弹掉。必须把上面的守卫**重新判一遍**，否则同一会话
        # 里会弹出第二个向导 —— 这正是需求 6 要防的重复。
        if self._w._onboarding_dialog is not None or self._first_run_guide_done:
            return False
        if self._access_status.is_expired():
            logger.info("[ONBOARDING] 当前无有效授权，暂不显示引导（待授权成功后触发）")
            return False

        self._w._first_run_guide_done = True
        logger.info("[ONBOARDING] 显示使用引导")
        self._open_onboarding(first_run=True)
        return True


    def _wait_for_license_load(self, timeout_ms: int = 3000) -> None:
        """启动期短暂等待许可证判定落地（本地读盘 + 验签，通常几毫秒）。

        为什么必须有界：入口 main.py 紧接着要武装「5 秒后弹更新对话框」的
        定时器，而引导本身也是模态的 —— 引导必须在那个定时器触发前弹完，
        否则两个模态框会叠在一起。因此这里只做有界等待，超时就按当前状态判定。
        """
        worker = getattr(self, "_license_load_worker", None)
        if worker is None or not worker.is_running():
            return
        deadline = time.monotonic() + timeout_ms / 1000.0
        while worker.is_running() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        logger.info("[ONBOARDING] 等待许可证判定结束（仍在跑=%s）", worker.is_running())


    def _open_onboarding(self, first_run: bool) -> None:
        """打开引导。

        首次引导的前两步是**主界面上的聚光灯**（非模态，见 GuideSpotlight），
        走完之后才把第 3 步及之后交给原来的模态对话框；回顾模式直接进对话框。
        因此首次这条路径不能在这里 exec() —— 它是异步的，后续阶段在
        _on_guide_spotlight_finished 里推进。
        """
        # 两条路径都用同一套四步聚光灯：首次引导从第 1 步走真实交互，
        # 标题栏 ? 的回顾模式则在 review_mode 下只做只读说明。
        self._start_guide_spotlight(SPOTLIGHT_STEPS[0], review=not first_run)


    def _spotlight_spec(self, step_index: int):
        """某一步聚光灯要说什么、高亮哪些真实控件。

        标题与正文一律取自 STEPS —— 聚光灯与旧版对话框讲的是同一份四步引导，
        各写一份迟早会改歪一份。
        """
        if step_index == 0:
            return STEPS[0][0], STEPS[0][1][0], [self._w._url_input, self._w._connect_btn]
        if step_index == 1:
            return STEPS[1][0], STEPS[1][1][0], [self._w._ocr_game_combo]
        if step_index == 2:
            # 第三步高亮右侧面板里的「选择识别区域」；正文含辅助提示那一段，
            # 因此用换行把两段拼起来。
            return STEPS[2][0], "\n".join(STEPS[2][1]), [self._w._roi_btn]
        if step_index == 3:
            return (STEPS[3][0], "\n".join(STEPS[3][1]), [self._w._ocr_btn],
                    self._ocr_blocked_reason())
        return None


    def _start_guide_spotlight(self, step_index: int, review: bool = False) -> None:
        """显示第 step_index 步的聚光灯。

        :param review: True = 标题栏 ? 的回顾模式（只读说明）。它复用同一套
            聚光灯，但被 GuideSpotlight 用 review_mode 隔离掉所有"真实操作
            自动推进"的路径，主窗口这边也不写任何首次引导状态。
        """
        if self._guide_spotlight is not None:
            return
        spec = self._spotlight_spec(step_index)
        if spec is None:
            return
        title, body, targets = spec[0], spec[1], spec[2]
        blocked_reason = spec[3] if len(spec) > 3 else None
        # 后三步高亮的都是右侧 OCR 面板里的控件：卡片必须放到左边的视频区，
        # 否则一张 340px 的卡片会把 310px 的面板整个盖住。
        prefers_left = step_index in (1, 2, 3)
        if prefers_left:
            self._ensure_panel_for_game_step()
        last = step_index == SPOTLIGHT_STEPS[-1]
        logger.info("[ONBOARDING] 聚光灯第 %d 步：%s", step_index + 1, title)
        spotlight = _mw().GuideSpotlight(
            self._w, targets, title, body, step_index, len(STEPS),
            # 第一步没有上一步可回
            show_prev=step_index > SPOTLIGHT_STEPS[0],
            prefers_left=prefers_left,
            # 最后一步的主按钮是「完成」——它只结束引导，不会替用户点 OCR
            next_text="完成" if last else "下一步",
            review_mode=review)
        if blocked_reason:
            spotlight.set_body(f"{body}\n{blocked_reason}")
        spotlight.finished.connect(
            lambda outcome, i=step_index, r=review:
                self._on_guide_spotlight_finished(i, outcome, r))
        spotlight.back_requested.connect(
            lambda i=step_index, r=review: self._on_guide_spotlight_back(i, r))
        self._w._guide_spotlight = spotlight
        self._w._guide_review = review
        spotlight.start()


    def _on_guide_spotlight_finished(self, step_index: int, outcome: str,
                                     review: bool = False) -> None:
        """某一步聚光灯结束：继续下一步，或按模式决定怎么收场。"""
        self._w._guide_spotlight = None

        if review:
            # 回顾模式：只做清理，既不写首次引导状态，也不弹完成提示
            self._w._guide_review = False
            if outcome == GUIDE_ADVANCE and step_index != SPOTLIGHT_STEPS[-1]:
                QTimer.singleShot(
                    0, lambda: self._start_guide_spotlight(step_index + 1, review=True))
                return
            logger.info("[ONBOARDING] 回顾模式结束（第 %d 步）", step_index + 1)
            self._restore_guide_panel()
            return

        if outcome != GUIDE_ADVANCE:
            # 关闭前已经用确认框问过用户了，这里只负责按答案落盘
            keep = outcome == GUIDE_ALWAYS
            logger.info("[ONBOARDING] 第 %d 步被提前关闭，下次继续提醒=%s",
                        step_index + 1, keep)
            self._first_run_store.set_should_show_guide(keep)
            self._restore_guide_panel()
            return

        if step_index == SPOTLIGHT_STEPS[-1]:
            # 最后一步走完 = 整份引导完成
            logger.info("[ONBOARDING] 引导全部完成")
            self._first_run_store.set_should_show_guide(False)
            self._restore_guide_panel()
            self._show_guide_done_toast()
            return

        next_index = step_index + 1
        # 延到下一个事件循环再切下一步：本回调可能是从 _apply_connected_ui
        # 或游戏档切换中间发出来的，就地换层会让那一版界面停在半途。
        if next_index in SPOTLIGHT_STEPS:
            QTimer.singleShot(
                0, lambda: self._start_guide_spotlight(next_index))
            return
        start_step = DIALOG_START_FOR.get(next_index)
        if start_step is None:
            return
        logger.info("[ONBOARDING] 聚光灯结束，衔接对话框第 %d 步", start_step + 1)
        QTimer.singleShot(
            0, lambda: self._run_onboarding_dialog(
                first_run=True, start_step=start_step))


    def _on_guide_spotlight_back(self, step_index: int, review: bool = False) -> None:
        """「上一步」：退回上一步的聚光灯。

        刻意只做"再显示一次上一步"这一件事 —— 不碰连接、不清地址输入框。
        用户是在读引导，后退一步不该产生任何业务副作用。
        """
        self._w._guide_spotlight = None
        previous = step_index - 1
        if previous < 0:
            return
        logger.info("[ONBOARDING] 聚光灯回退到第 %d 步", previous + 1)
        QTimer.singleShot(
            0, lambda: self._start_guide_spotlight(previous, review=review))


    def _ocr_blocked_reason(self) -> str | None:
        """OCR 现在为什么点不动；能点则返回 None。

        只**描述**缺少的前置条件，不做任何补救 —— 引导绝不会替用户连接直播
        或框选区域，那既绕过了业务门禁，也把"用户自己完成"这件事抹掉了。
        """
        if self._access_status.is_expired():
            return "当前授权不可用，请先完成激活。"
        if not self._w._connected:
            return "尚未连接直播：请先完成第 1 步。"
        if not self._w._roi_manager.has_roi:
            return "尚未框选房间码：请先完成第 3 步。"
        return None


    def _ensure_panel_for_game_step(self) -> None:
        """需要高亮面板内控件时，若面板处于收起状态则临时展开。

        只记"引导把面板从收起改成展开"这一种情形 —— 本来就是展开的就不动它，
        结束时也就没有需要还原的东西。
        """
        if self._w._ocr_panel.isVisible():
            # 已经可见就什么都不做 —— 尤其**不要**把已记下的原状态覆盖成 None：
            # 第 1 步可能已经把面板从"收起"临时展开了，后面几步看到的是引导
            # 自己展开的面板，覆盖掉原状态就再也还原不回去了。
            return
        logger.info("[ONBOARDING] 面板处于收起状态，为聚光灯临时展开")
        self._w._guide_panel_restore_to = False
        self._w._guide_panel_touched = False
        # 走底层 setter，不走 _on_expand_clicked —— 后者会把它记成"用户的选择"
        self._w._set_ocr_panel_visible(True)


    def _restore_guide_panel(self) -> None:
        """引导结束：把面板还回引导开始前的状态。"""
        target = self._guide_panel_restore_to
        self._w._guide_panel_restore_to = None
        if target is None:
            return
        if self._guide_panel_touched:
            # 用户在引导期间自己点过收起/展开 —— 他的新选择优先
            logger.info("[ONBOARDING] 用户在引导期间自己动过面板，保持现状")
            return
        logger.info("[ONBOARDING] 引导结束，面板恢复到展开=%s", target)
        self._w._set_ocr_panel_visible(target)


    def _show_guide_done_toast(self) -> None:
        """引导完成后的轻量提示：约 2 秒后自动消失。"""
        self._w._guide_toast = GuideToast()
        self._guide_toast.show_over(self._w, 2000)


    def _reposition_guide_spotlight(self) -> None:
        """让聚光灯跟上真实控件：OCR 面板展开/收起、分隔条拖动都会走到这里。"""
        spotlight = self._guide_spotlight
        if spotlight is not None:
            spotlight.reposition()


    def _run_onboarding_dialog(self, first_run: bool, start_step: int = 0) -> None:
        """构造并模态运行向导；关闭后按用户表态落盘 —— 这里是唯一的写盘点。

        结论在 exec() 返回之后再取，而不是接 accepted/rejected 信号：
        QDialog::done() 先发 finished 再发 accepted/rejected，靠信号回调的先后
        顺序去读状态，容易读到已经被清掉的引用。
        """
        dialog = _mw().OnboardingDialog(self._w, first_run=first_run,
                                      start_step=start_step)
        # 必须在 exec() 之前赋值，否则重入保护不起作用
        self._w._onboarding_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._w._onboarding_dialog = None

        # 只有首次引导才会写状态：从帮助里重新打开看一眼，不该改掉用户的设置
        if not first_run:
            return
        # 走到这里整份首次引导就结束了（走完或中途关掉），把面板还回去
        self._restore_guide_panel()
        if dialog.completed():
            # 正常走完 → 一律不再自动显示。这是"已完成"的唯一定义，
            # 与中途关闭的三选项（见 ask_guide_exit）互不重叠。
            logger.info("[ONBOARDING] 引导已走完，不再自动显示")
            self._first_run_store.set_should_show_guide(False)
        elif dialog.exit_choice() is not None:
            # 提前关闭：关闭前已经在确认框里问过用户了
            keep = dialog.exit_choice() == GUIDE_ALWAYS
            logger.info("[ONBOARDING] 引导被提前关闭，下次继续提醒=%s", keep)
            self._first_run_store.set_should_show_guide(keep)

class UpdateCoordinator(_CoordinatorBase):
    """领域协调器(职责见各方法 docstring)。"""

    def __init__(self, window):
        super().__init__(window)
        from utils.update_service import UpdateService

        w = self._w
        w._update_service = UpdateService()
        w._update_worker = None
        w._update_dialog = None
        w._update_timer = QTimer(w)
        w._update_timer.setSingleShot(True)
        w._update_timer.timeout.connect(self._on_update_timer_timeout)


    def start_update_check(self):
        """安排一次启动后的更新检查。由应用入口 main.py 调用。

        为什么不在 __init__ 里自动开始：
            与 start_heartbeat 同因 —— 它会向更新源发起**真实网络请求**，而
            「构造一个 MainWindow」在测试里极常见。若挂在构造流程上，每次跑
            测试都会打一次真实源。

        重复调用是安全的：只会重置那个单次定时器。
        """
        self._update_timer.start(UPDATE_CHECK_DELAY_MS)


    def _on_update_timer_timeout(self):
        """延时到点：在后台线程发起检查。

        Velopack 的 check_for_updates 是阻塞调用（Rust 侧释放 GIL 但不返回
        事件循环），放 GUI 线程会冻住界面，因此借用既有的 LicenseWorker
        线程模式 —— 它只要求「无参任务 + 单参完成回调」，恰好合适。
        """
        # 上一次仍在跑就跳过，避免网络慢时堆叠线程
        if self._update_worker is not None and self._update_worker.is_running():
            return
        self._w._update_worker = LicenseWorker(
            task=self._update_service.check,
            on_done=self._w._update_check_done_sig.emit,
            name="update-check",
        )
        self._update_worker.start()


    def _on_update_check_done(self, info):
        """检查结束回调（经 Qt 排队回到 GUI 线程）。

        ``info`` 为 None 表示「没有可用的更新」—— 包括已是最新、更新源不可
        用（正式源尚未部署 / 网络不通）、以及本机不是 Velopack 安装版本
        （源码启动、旧 ZIP 解压运行）。这些一律静默返回，不提示、不改状态：
        后台版本检查的失败不该在用户面前产生动静。

        版本比较由 Velopack 自己做（check_for_updates 已经把「有更新」判定
        完了），我们不再实现第二套比较逻辑。

        结构判断而不是真值判断：LicenseWorker 在任务抛异常时会回调一个
        LicenseResult 兜底对象，它既不是 None 也没有 TargetFullRelease，
        绝不能被当成一份有效更新信息去取版本号。
        """
        if info is None or not hasattr(info, "TargetFullRelease"):
            return
        logger.info(
            "发现新版本 %s（当前 %s）",
            info.TargetFullRelease.Version,
            self._update_service.current_version(),
        )
        # 持有引用：下载可以在窗口收起后继续跑完，对话框不能随 exec() 返回被回收
        self._w._update_dialog = UpdateDialog(self._update_service, info, self)
        self._update_dialog.apply_requested.connect(self._on_update_apply_requested)
        self._update_dialog.exec()


    def _on_update_apply_requested(self, info):
        """用户确认「立即重启并更新」。

        Velopack 的 apply_updates_and_restart 会启动 Update.exe 等本进程退出、
        然后**直接结束当前进程**（Rust 侧 exit(0)），Qt 的窗口关闭流程不会再
        走一遍。因此 ffmpeg / 播放线程必须在这里先显式停掉，否则会留下孤儿
        进程 —— 这正是先调 _disconnect() 的原因。
        """
        logger.info("用户确认应用更新，停止当前会话后交给 Velopack")
        self._w._disconnect()
        # 先落盘「正在换到新版本」再 apply（Phase 4B）。顺序不能反：apply 之后
        # 本进程会被 Velopack 直接结束，没有机会再写任何东西。这条记录是新版
        # 启动失败时唯一的线索 —— 下一次启动据此判定要不要回滚。
        update_state.write_state(
            update_state.mark_applying(
                self._w.current_version(), info.TargetFullRelease.Version
            )
        )
        try:
            self._update_service.apply_and_restart(info)
        except UpdateError as e:
            # 只有「Velopack 没接管、进程还活着」才会走到这里
            logger.warning("应用更新失败: %s", e)
            QMessageBox.warning(self._w, "更新失败", str(e))

