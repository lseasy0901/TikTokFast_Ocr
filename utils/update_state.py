# -*- coding: utf-8 -*-
"""
更新状态机 —— 启动失败自动恢复 (Phase 4B)。

解决的问题：
    更新到新版本 B 之后，如果 B **装得上但起不来**，Velopack 不会自动回退
    （安装/apply 阶段的失败它会恢复，但「已安装成功、启动即崩」它不管）。
    本模块用一份极小的状态文件，把「B 装好了但还没证明自己能跑」这件事记下来，
    于是 B 崩掉之后，下一次启动就能发现并回滚到 A。

为什么必须落盘而不能只靠内存：
    B 崩掉的那一刻进程就没了，没有地方能执行收尾代码。因此状态必须在
    **apply 之前**写盘，并且只能由「下一次启动」来推进 —— 不能依赖 finally、
    不能依赖 atexit、也不能依赖进程能优雅退出（用户强杀、断电都等同崩溃）。

五个字段就是全部，刻意不引入数据库/配置体系：
    state           normal / applying_new / booting_new / healthy / recovery
    from_version    从哪个版本迁出（A）
    to_version      迁向哪个版本；终态表示「当前处于哪个版本」
    blocked_version 被判定为启动失败的版本，正常更新检查时跳过它
    attempts        已经用掉的自动回滚次数（上限 1，防死循环）

位置：
    %LOCALAPPDATA%\\LiveLens\\update_state.json
    与应用数据同目录（该目录已由 Phase 3 E2E 证明能跨 Velopack 更新与卸载存活），
    且在 Velopack 的 current/ 之外 —— 更新替换安装目录不会碰到它。
    与 prefs.json / stream_history.json 并列但**互不写入**。
    目录解析复用 utils.stream_history.default_data_dir()：那里已经是全项目
    唯一的「本机数据目录」入口（LOCALAPPDATA），测试用环境变量覆盖即可隔离，
    本模块不再造第二个目录规则。
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from typing import Optional

logger = logging.getLogger("DouyinLowLatencyViewer.utils.update_state")

FILENAME = "update_state.json"

#: 健康窗口（毫秒）。新版启动后在这段时间内没有崩，就认为它启动成功。
#:
#: 定义是「Qt 事件循环已经稳定运行」——不等 OCR、不等连流、不等网络、
#: 不等任何业务成功。窗口太短会漏掉「启动后几秒才崩」，太长则用户在这个
#: 窗口里主动关掉程序也会被当成启动失败（见 mark_healthy_if_booting 的补写）。
HEALTH_WINDOW_MS = 12000

STATE_NORMAL = "normal"
STATE_APPLYING_NEW = "applying_new"
STATE_BOOTING_NEW = "booting_new"
STATE_HEALTHY = "healthy"
STATE_RECOVERY = "recovery"

# resolve_startup 的四种结论
ACTION_NORMAL = "normal"      # 什么都不用做，照常启动
ACTION_BOOT = "boot"          # 本次是「新版/回滚版」第一次启动 → 起健康窗口
ACTION_RECOVER = "recover"    # 上次启动没熬过健康窗口 → 执行一次回滚
ACTION_FAILED = "failed"      # 已用过回滚，或回滚本身没成功 → 只提示，不再自动动作

_VALID_STATES = (STATE_NORMAL, STATE_APPLYING_NEW, STATE_BOOTING_NEW,
                 STATE_HEALTHY, STATE_RECOVERY)


@dataclass
class UpdateState:
    """状态文件的全部内容。字段含义见模块 docstring。"""

    state: str = STATE_NORMAL
    from_version: Optional[str] = None
    to_version: Optional[str] = None
    blocked_version: Optional[str] = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "from": self.from_version,
            "to": self.to_version,
            "blocked_version": self.blocked_version,
            "attempts": self.attempts,
        }


@dataclass
class StartupDecision:
    """启动时的结论：做什么、状态变成什么、要不要给用户提示。"""

    action: str
    state: UpdateState
    notice: Optional[str] = None


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------
def state_path() -> str:
    """状态文件完整路径（不创建任何东西）。"""
    from utils.stream_history import default_data_dir

    return os.path.join(default_data_dir(), FILENAME)


def read_state(path: Optional[str] = None) -> UpdateState:
    """读状态。文件缺失、损坏、字段非法一律安全降级为默认 normal。

    宁可当成「没有正在进行的更新」，也不要因为一份坏 JSON 让程序起不来：
    这里面的信息只影响自动恢复，不影响任何正常功能。
    """
    target = path or state_path()
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return UpdateState()
    except (OSError, ValueError) as e:
        logger.warning("更新状态文件读取失败，按默认状态处理: %s", e)
        return UpdateState()

    if not isinstance(data, dict) or data.get("state") not in _VALID_STATES:
        logger.warning("更新状态文件内容非法，按默认状态处理: %s", target)
        return UpdateState()

    attempts = data.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        attempts = 0

    def _str_or_none(key: str) -> Optional[str]:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    return UpdateState(
        state=data["state"],
        from_version=_str_or_none("from"),
        to_version=_str_or_none("to"),
        blocked_version=_str_or_none("blocked_version"),
        attempts=attempts,
    )


def write_state(state: UpdateState, path: Optional[str] = None) -> None:
    """原子写状态：先写同目录临时文件再 os.replace 覆盖。

    为什么必须原子：断电/强杀可能正好落在写文件的中间。没有原子替换的话，
    下次启动会读到半截 JSON —— 对「判定要不要回滚」来说这是致命的（可能
    把 booting_new 读丢，于是坏版本永远回滚不了）。
    """
    target = path or state_path()
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".update_state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            # 临时文件残留会让目录变脏，但要保证异常原样抛出（调用方要知道失败）
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        # 写不进去不是致命错误：正常功能照常，只是这次少了自动恢复能力
        logger.warning("更新状态文件写入失败: %s", e)


# ----------------------------------------------------------------------
# 转换（都是纯函数，便于单测）
# ----------------------------------------------------------------------
def mark_applying(from_version: str, to_version: str) -> UpdateState:
    """用户确认更新、即将 apply 时调用。必须在 apply **之前**落盘。

    这是整个恢复机制的起点：从这一刻起，「B 还没证明自己能跑」这件事已经被
    记在磁盘上，哪怕 apply 之后进程被 Update.exe 直接结束也不会丢。
    """
    return UpdateState(
        state=STATE_APPLYING_NEW,
        from_version=from_version,
        to_version=to_version,
        blocked_version=None,
        attempts=0,
    )


def resolve_startup(state: UpdateState, current_version: str) -> StartupDecision:
    """启动时的状态机推进。纯函数：只根据 (旧状态, 当前版本) 得出新状态。

    完整转换（与设计文档一一对应）：

      applying_new, current == to
          → booting_new（本次就是新版第一次启动，开始健康窗口）
      applying_new, current != to
          → normal（更新最终没落地：apply 失败/包没了。不能把它当成启动失败，
            否则会把一个根本没换上去的版本「回滚」一遍）

      booting_new, current != to
          → normal（陈旧状态：版本已经变了）
      booting_new, current == to, attempts == 0
          → recovery{target: from, blocked: to, attempts: 1}
            （新版启动过但没撑到健康窗口）
      booting_new, current == to, attempts >= 1
          → failed（回滚目标自己也没起来。**不再自动动作**，防死循环）

      recovery, current == to
          → booting_new（回滚目标 A 正在跑，给它一次自己的健康窗口；
            attempts 保持 1 —— 这样 A 再崩就落到上面的 failed 分支，
            绝不会「A 崩了再回滚一次」）
      recovery, current != to
          → failed（回滚没能落地：无网络 / feed 缺旧版本 / 下载失败 / apply 失败。
            只提示，不自动重试 —— 重试会变成另一种死循环）

      healthy / normal
          → normal（顺带把版本刷新到实际值）
    """
    st = state
    if st.state == STATE_APPLYING_NEW:
        if st.to_version and current_version == st.to_version:
            return StartupDecision(
                ACTION_BOOT,
                UpdateState(STATE_BOOTING_NEW, st.from_version, st.to_version, None, 0),
            )
        logger.info("更新状态为 applying_new 但当前版本是 %s（期望 %s），按未落地处理",
                    current_version, st.to_version)
        return StartupDecision(
            ACTION_NORMAL,
            UpdateState(STATE_NORMAL, None, current_version, st.blocked_version, 0),
        )

    if st.state == STATE_BOOTING_NEW:
        if not st.to_version or current_version != st.to_version:
            logger.info("陈旧的 booting_new=%s（当前 %s），重置为 normal",
                        st.to_version, current_version)
            return StartupDecision(
                ACTION_NORMAL,
                UpdateState(STATE_NORMAL, None, current_version, st.blocked_version, 0),
            )
        if st.attempts >= 1:
            # 已经回滚过一次，回滚目标仍然没通过健康窗口：到此为止
            return StartupDecision(
                ACTION_FAILED,
                st,
                f"上次更新到 {st.to_version} 后程序未能正常启动，自动恢复也已尝试过，"
                f"但当前版本 {current_version} 仍未通过启动检查。\n\n"
                "请手动重新安装 LiveLens（用户数据不会丢失）。",
            )
        logger.info("检测到 %s 启动失败，准备回滚到 %s", st.to_version, st.from_version)
        return StartupDecision(
            ACTION_RECOVER,
            UpdateState(STATE_RECOVERY, None, st.from_version, st.to_version, 1),
            None,
        )

    if st.state == STATE_RECOVERY:
        if st.to_version and current_version == st.to_version:
            return StartupDecision(
                ACTION_BOOT,
                UpdateState(STATE_BOOTING_NEW, None, current_version,
                            st.blocked_version, st.attempts),
            )
        return StartupDecision(
            ACTION_FAILED,
            st,
            f"新版本启动失败后，自动恢复到 {st.to_version} 未能完成"
            "（可能是网络不可用，或更新源上已没有该版本的完整安装包）。\n\n"
            f"当前仍运行版本 {current_version}。请联网后重新启动程序，"
            "或手动重新安装 LiveLens。",
        )

    # normal / healthy
    return StartupDecision(
        ACTION_NORMAL,
        UpdateState(STATE_NORMAL, None, current_version, st.blocked_version, 0),
    )


def mark_healthy_if_booting(current_version: str) -> bool:
    """健康标记：只有「正在等待新版证明自己」时才写 healthy。

    严格条件（硬约束）：state == booting_new 且 current_version == to。

    为什么这条约束不能放松：apply 之后旧版本进程会被 Velopack 结束，那也会走
    到退出路径。如果退出时无条件写 healthy，就会把刚写下的 applying_new 覆盖
    掉 —— 新版一旦启动失败，恢复机制就再也看不到线索了。所以退出路径上的补写
    必须走这个同一个函数，而不是自己写状态。

    返回是否真的写了（供日志/测试观察）。
    """
    state = read_state()
    if state.state != STATE_BOOTING_NEW or state.to_version != current_version:
        return False
    write_state(replace(state, state=STATE_HEALTHY, from_version=None, attempts=state.attempts))
    logger.info("版本 %s 通过启动健康检查", current_version)
    return True


def clear_blocked_version() -> None:
    """解除封锁（更新源上出现了更高的版本时）。"""
    state = read_state()
    if state.blocked_version is None:
        return
    logger.info("解除版本 %s 的更新封锁", state.blocked_version)
    write_state(replace(state, blocked_version=None))
