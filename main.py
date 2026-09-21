# -*- coding: utf-8 -*-
"""
Douyin Low Latency Stream Viewer - 启动入口 (Phase 1 - Step 4)

功能：
    初始化 PySide6 QApplication，创建并显示 MainWindow
"""

import logging
import os
import sys
import threading

# 将项目根目录加入 sys.path，保证以任意方式启动均可正确导入包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _velopack_bootstrap() -> None:
    """Velopack 启动钩子。必须是本进程**最早**执行的一件事。

    Velopack 需要在这个位置处理它自己的启动约定，晚一步就会错过：
        - ``--veloapp-install / --veloapp-updated / --veloapp-obsolete /
          --veloapp-uninstall``：安装器 / 更新器回调本 exe 时传入，处理完
          Velopack 会直接结束进程（因此这些调用不会跑出 GUI）；
        - 待应用更新：如果本地已下载好一个更高版本的包且 auto-apply 打开，
          Velopack 会在这个调用里接手、应用并重启，同样不会再返回；
        - 首次安装 / 更新后重启的环境变量标记（VELOPACK_FIRSTRUN / _RESTART）；
        - 设置 Windows AppUserModelID（任务栏图标归组）。
    正常启动时它只是把上面这些处理完就返回，不联网、不做业务。

    放在 Qt 之前、放在任何业务导入之前 —— 它一旦决定接管（应用更新），
    进程就会在这里结束，之后的一切都不该发生。

    未安装运行（源码启动 / 旧 ZIP 解压运行）时 Velopack 的 locator 会失败，
    它自己会记一条日志后正常返回，不需要我们兜底；真正需要兜底的是
    velopack 包本身缺失（老环境），那种情况下更新功能整体关闭，但程序照常
    启动。

    不需要 restart 参数：LiveLens 没有真实的 argv 依赖（sys.argv 只用于
    QApplication），因此用默认的 apply_updates_and_restart 即可，
    不引入 restart-args 的泛化逻辑。
    """
    try:
        import velopack
    except Exception as e:  # noqa: BLE001 - 更新功能可关闭，程序必须能启动
        logging.getLogger("DouyinLowLatencyViewer").warning(
            "velopack 不可用，自动更新关闭: %s", e
        )
        return
    # set_args(sys.argv[1:])：Velopack 约定的 hook 参数在第一位，
    # 不传 argv[0]（exe 路径）才不会让参数整体错位。
    velopack.App().set_args(sys.argv[1:]).run()


def _start_e2e_autopilot(window) -> None:
    """端到端验证用的自动驾驶 —— 仅当 LIVELENS_E2E_UPDATE 环境变量非空时启用。

    这是给自动化验证用的钩子，**不是产品功能**：真实用户不会设置这个变量，
    默认启动路径完全不经过它。它替用户做的也只是「按顺序点按钮」：
    更新说明页点「立即更新」，下载完成页点「立即重启并更新」。

    刻意不绕过任何一步 —— 检查、下载、校验、应用、重启仍然全部由
    Velopack 与既有接线完成。之所以需要它，是因为打包后的 EXE 里没有
    python.exe，外部脚本无法在已安装实例内驱动 Qt 事件循环。
    """
    from PySide6.QtCore import QTimer

    logger = logging.getLogger("DouyinLowLatencyViewer.e2e")
    logger.info("E2E 自动驾驶已启用")

    def tick() -> None:
        dialog = getattr(window, "_update_dialog", None)
        if dialog is None:
            return
        if dialog.is_downloading():
            return
        if not dialog._start_btn.isHidden():
            logger.info("E2E: 点击「立即更新」")
            dialog._start_btn.click()
        elif not dialog._restart_btn.isHidden():
            logger.info("E2E: 点击「立即重启并更新」")
            dialog._restart_btn.click()

    timer = QTimer(window)
    timer.timeout.connect(tick)
    timer.start(200)


def _advance_update_state():
    """读取更新状态并推进状态机（Phase 4B：启动失败自动恢复）。

    刻意放在 Qt 之前：这件事只关乎「上一次更新到底成没成」，和界面无关。
    更重要的是——它必须在 PySide6 导入**之前**跑完：如果新版本本身就有问题
    （比如打包漏了文件、Qt 起不来），进程可能根本没机会走到 GUI。只有把
    `applying_new → booting_new` 这一步留在最前面，「新版启动过但没撑住」
    这个线索才会被记下来，下一次启动才能据此回滚。

    返回 (UpdateService, StartupDecision)；连版本号都取不到时返回 (None, None)。
    """
    from utils import update_state
    from utils.update_service import UpdateService

    logger = logging.getLogger("DouyinLowLatencyViewer")
    service = UpdateService()
    try:
        current = service.current_version()
    except Exception as e:  # noqa: BLE001 - 状态机不能拦住启动
        logger.warning("读取当前版本失败，跳过更新状态机: %s", e)
        return None, None

    previous = update_state.read_state()
    decision = update_state.resolve_startup(previous, current)
    update_state.write_state(decision.state)
    logger.info(
        "更新状态 %s → %s（当前版本 %s）",
        previous.state, decision.state.state, current,
    )
    return service, decision


def _start_recovery(service, target_version: str) -> None:
    """在后台执行一次回滚（可能要先从更新源下载旧版本全量包）。

    三个刻意的选择：
      - 放后台线程：恢复要联网、要下载，绝不能拖住启动或冻住 GUI；
      - **非 daemon**：新版坏到连 Qt 都起不来时，main() 会很快 return，如果
        这个线程是 daemon 就会随进程一起被杀掉，回滚永远做不完 —— 于是「坏版本
        启动失败」永远修不好，这正是恢复机制最需要覆盖的情况。非 daemon 让解释器
        在退出前等它跑完；
      - 不等待结果：成功时 Velopack 会结束本进程并拉起旧版本，这个线程不会
        有「成功返回」这一说。失败时只记日志 —— 状态文件仍停在 recovery，
        下一次启动会用 ACTION_FAILED 给用户一个明确提示（不自动重试，避免
        在没有网络/源上没有旧版本时变成另一种死循环）。
    """

    def run() -> None:
        logger = logging.getLogger("DouyinLowLatencyViewer")
        try:
            service.downgrade_to(target_version)
        except Exception as e:  # noqa: BLE001 - 失败不影响当前进程继续可用
            logger.warning("自动恢复失败（回滚到 %s）: %s", target_version, e)

    threading.Thread(target=run, name="update-recovery", daemon=False).start()


def main() -> int:
    # Velopack 必须在最前：见 _velopack_bootstrap 的说明（它可能在这里
    # 接手更新并结束进程）。
    _velopack_bootstrap()

    # 配置日志（DEBUG 级别，便于排查 GUI 黑屏）
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s %(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("DouyinLowLatencyViewer")
    logger.info("启动 Douyin Low Latency Stream Viewer")

    # 更新状态机（Phase 4B）。必须在 Qt 之前，理由见 _advance_update_state。
    from utils import update_state

    update_service, decision = _advance_update_state()
    if decision is None:
        decision = update_state.StartupDecision(update_state.ACTION_NORMAL, update_state.UpdateState())
    if decision.action == update_state.ACTION_RECOVER and update_service is not None:
        logger.info("检测到上一次更新启动失败，开始自动恢复到 %s", decision.state.to_version)
        _start_recovery(update_service, decision.state.to_version)

    # 延迟导入 PySide6，给出明确的依赖缺失提示
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("错误：未安装 PySide6，请执行 pip install PySide6")
        return 1

    from gui import theme
    from gui.main_window import MainWindow

    # 高 DPI 舍入策略必须在 QApplication 构造之前设置
    theme.configure_high_dpi()

    # 创建应用（QApplication 需要 argv 列表）
    app = QApplication(sys.argv)
    app.setApplicationName("Douyin Low Latency Stream Viewer")

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    # 启动即记录版本：已安装运行时这个值来自 Velopack 的安装清单，是排查
    # 「更新到哪一版了 / 更新到底有没有生效」的第一手信息。
    logger.info("当前版本 %s", window.current_version())

    # 启动心跳上报（Phase 7.4，DAU 统计）。
    # 刻意放在这里而不是 MainWindow.__init__：它会发起真实网络请求，
    # 而测试里会大量直接构造 MainWindow。详见 MainWindow.start_heartbeat。
    window.start_heartbeat()

    # 首次启动引导（只在第一次运行时自动弹出；已关掉过就什么都不做）。
    # 与心跳同理，刻意由入口调用而不是挂在 MainWindow.__init__：
    # 测试会大量直接构造 MainWindow，构造期弹模态框会卡死无头测试。
    # 必须排在 start_update_check() 之前：更新检查是「延时 5 秒后 exec() 一个
    # 模态框」，而引导本身也是模态的，嵌套事件循环里那个定时器照常触发 ——
    # 先武装更新检查，两个模态框就会叠在一起。心跳放在它前面，
    # 是为了不让用户阅读引导的时间推迟首次上报。
    window.maybe_show_first_run_guide()

    # 启动更新检查（Phase 3，Velopack）。
    # 与心跳同理：这里只是把那个「延时若干秒」的单次定时器装上，真正的网络
    # 请求在后台线程里发生，启动与事件循环都不会因此等待。
    window.start_update_check()

    # 健康窗口（Phase 4B）：本次是「新版 / 回滚版」的第一次启动，撑过
    # HEALTH_WINDOW_MS 就认为它启动成功，把状态写成 healthy。
    # 判据只有一条 —— Qt 事件循环已经稳定运行；不等 OCR、不等连流、不等网络、
    # 不等任何业务成功。
    #
    # aboutToQuit 是「尽力而为」的补写，用于减少误判（用户在窗口内主动关掉
    # 程序不算启动失败）。它走的是同一个 mark_healthy_if_booting，因此
    # **不可能**覆盖 applying_new —— 那正是 apply 之后旧进程退出的那条路径，
    # 一旦被误写成 healthy，新版启动失败就再也恢复不了了。
    if decision.action == update_state.ACTION_BOOT:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(
            update_state.HEALTH_WINDOW_MS,
            lambda: update_state.mark_healthy_if_booting(window.current_version()),
        )
        app.aboutToQuit.connect(
            lambda: update_state.mark_healthy_if_booting(window.current_version())
        )

    # 恢复无法完成时，在下一次正常 GUI 启动时给一个最小、明确的提示
    # （不新建恢复专用 UI，也不自动重试）
    if decision.notice:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning(window, "LiveLens 更新恢复", decision.notice)

    # 端到端验证用的自动驾驶（只认环境变量，默认不启用）
    if os.environ.get("LIVELENS_E2E_UPDATE"):
        _start_e2e_autopilot(window)

    # 进入事件循环
    exit_code = app.exec()
    logger.info("应用退出 (code=%d)", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
