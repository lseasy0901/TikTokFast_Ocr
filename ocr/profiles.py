# -*- coding: utf-8 -*-
"""
游戏配置档 (OCR V2.1 - Format-Aware OCR)

GameProfile 把「游戏」映射到「一个或多个 CodeFormat」。
OCR Core 只依赖 CodeFormat；游戏差异全部收敛在这里，
因此引擎里没有任何 if/elif 游戏分支，只有查表。

扩展方式（无需改 OCR 引擎）：
    - 新游戏   -> 在这里加一个 GameProfile 常量并注册进 PROFILES
    - 同游戏多格式 -> GameProfile(formats=(fmt_a, fmt_b), ...)，识别时按顺序尝试
    - 新格式   -> CodeFormat.from_spec("LLLDDDDDD")

关于默认档：
    DEFAULT_PROFILE_ID 是 auto（同时尝试两款游戏的格式），
    它是【底层】的兜底能力，不代表正式路径。正式 OCR 必须由用户在界面
    选择具体游戏：OCRWorker 会拒绝 None/auto（见 ocr/worker.py 的
    _require_explicit_game），因此「没选游戏」不会被静默当成 auto 处理。
"""

from typing import Dict, Optional, Tuple

from .code_format import CodeFormat

__all__ = [
    "GameProfile",
    "PROFILES",
    "DEFAULT_PROFILE_ID",
    "get_profile",
    "profile_ids",
    "GAME_PROFILE_IDS",
    "game_profiles",
    "DELTA_FORCE",
    "VALORANT",
    "AUTO",
]


class GameProfile:
    """
    一个游戏（或一个使用场景）对应的房间号格式集合。

    参数：
        profile_id: 稳定标识（配置/UI 用它来选中，不随展示名变化）
        display_name: 展示名
        formats: 该游戏支持的所有格式，第一个为主格式
        description: 说明文字
    """

    __slots__ = ("_profile_id", "_display_name", "_formats", "_description")

    def __init__(
        self,
        profile_id: str,
        display_name: str,
        formats: Tuple[CodeFormat, ...],
        description: str = "",
    ):
        if not profile_id:
            raise ValueError("GameProfile 需要非空的 profile_id")
        if not formats:
            raise ValueError(f"GameProfile({profile_id}) 至少需要一个 CodeFormat")
        for fmt in formats:
            if not isinstance(fmt, CodeFormat):
                raise ValueError(f"formats 只能包含 CodeFormat，收到 {fmt!r}")

        self._profile_id = profile_id
        self._display_name = display_name or profile_id
        self._formats = tuple(formats)
        self._description = description

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def formats(self) -> Tuple[CodeFormat, ...]:
        return self._formats

    @property
    def primary_format(self) -> CodeFormat:
        return self._formats[0]

    def format_by_name(self, name: str) -> Optional[CodeFormat]:
        for fmt in self._formats:
            if fmt.name == name:
                return fmt
        return None

    def __repr__(self) -> str:
        specs = ", ".join(f.describe() for f in self._formats)
        return f"<GameProfile {self._profile_id}: {specs}>"


# ----------------------------------------------------------------------
# 已支持的游戏
# ----------------------------------------------------------------------
# 三角洲行动 / Delta Force：7 位，前 3 位字母 + 后 4 位数字（^[A-Z]{3}[0-9]{4}$）
DELTA_FORCE = GameProfile(
    profile_id="delta_force",
    display_name="三角洲行动 (Delta Force)",
    formats=(CodeFormat.from_spec("delta_force_7", "LLLDDDD"),),
    description="7 位：3 位大写字母 + 4 位数字",
)

# 无畏契约 / VALORANT：6 位，前 3 位字母 + 后 3 位数字（^[A-Z]{3}[0-9]{3}$）
VALORANT = GameProfile(
    profile_id="valorant",
    display_name="无畏契约 (VALORANT)",
    formats=(CodeFormat.from_spec("valorant_6", "LLLDDD"),),
    description="6 位：3 位大写字母 + 3 位数字",
)

# 默认档：本迭代还没有游戏选择 UI，而「选错档」的代价是彻底识别不出
# （6 位码在 7 位格式下永远无解），所以默认档同时覆盖两个已支持的格式。
# 多格式之间按「长度恰好合适的整段优先」裁决（见 room_code 的候选质量），
# 因此 7 位码不会被 6 位格式截断成 6 位。
# 等 UI 提供选择后，显式指定某一款游戏可以得到更严格的唯一解。
AUTO = GameProfile(
    profile_id="auto",
    display_name="自动（三角洲行动 + 无畏契约）",
    formats=(DELTA_FORCE.primary_format, VALORANT.primary_format),
    description="同时尝试 7 位与 6 位格式，长度恰好匹配的整段优先",
)

PROFILES: Dict[str, GameProfile] = {
    profile.profile_id: profile for profile in (AUTO, DELTA_FORCE, VALORANT)
}

#: 用户能显式选择的【具体游戏】档（不含自动档）。
#:
#: 「选错游戏」的判定只在这些档之间进行：auto 同时覆盖两款游戏的格式，
#: 它不代表用户选了某一款，因此谈不上选错。
GAME_PROFILE_IDS: Tuple[str, ...] = (
    DELTA_FORCE.profile_id,
    VALORANT.profile_id,
)

#: 没有显式选择时使用的档
DEFAULT_PROFILE_ID = AUTO.profile_id


def game_profiles() -> Tuple[GameProfile, ...]:
    """全部具体游戏档（不含 auto）。"""
    return tuple(PROFILES[profile_id] for profile_id in GAME_PROFILE_IDS)


def profile_ids() -> Tuple[str, ...]:
    """已注册的档标识（供未来的 UI 列表使用）。"""
    return tuple(PROFILES)


def get_profile(profile_id: Optional[str] = None) -> GameProfile:
    """
    按标识取档；None 取默认档。

    未知标识直接抛错而不是静默回退到默认档——
    静默回退会让「选错游戏」表现为「识别不出」，难以排查。
    """
    if profile_id is None:
        return PROFILES[DEFAULT_PROFILE_ID]
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise ValueError(
            f"未知的游戏档 {profile_id!r}，已注册: {', '.join(PROFILES)}"
        ) from None
