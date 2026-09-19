"""可调参数集中管理。

所有参数均支持通过环境变量覆盖，便于在服务器上测试时调整规则而无需改代码。
环境变量命名规则：数字炸弹 + 参数名，例如 ``BOMB_RANGE_SMALL=0,100``。
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    """读取整数型环境变量，解析失败时回退到默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量，接受 1/0、true/false、yes/no（大小写不敏感）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int_pair(name: str, default: tuple[int, int]) -> tuple[int, int]:
    """读取形如 ``"0,100"`` 的区间环境变量，解析失败时回退到默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        return default
    try:
        low, high = int(parts[0]), int(parts[1])
    except ValueError:
        return default
    if low > high:
        return default
    return low, high


def _env_optional_int(name: str, default: int | None) -> int | None:
    """读取可为空的整数型环境变量，``0`` 或负数视为不限制（None）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else None


# --- 人数与数字范围 ---------------------------------------------------------

#: 人数阈值：玩家数 <= 该值时采用小范围
SMALL_ROOM_MAX_PLAYERS: int = _env_int("BOMB_SMALL_ROOM_MAX_PLAYERS", 4)

#: 小范围数字区间（闭区间）
SMALL_ROOM_RANGE: tuple[int, int] = _env_int_pair("BOMB_RANGE_SMALL", (0, 100))

#: 大范围数字区间（闭区间）
LARGE_ROOM_RANGE: tuple[int, int] = _env_int_pair("BOMB_RANGE_LARGE", (0, 1000))


def range_for_player_count(player_count: int) -> tuple[int, int]:
    """根据开局瞬间的玩家人数返回本局数字范围。

    人数在点击「开始游戏」那一刻快照锁定，游戏过程中人数变化不再影响范围。
    """
    if player_count <= SMALL_ROOM_MAX_PLAYERS:
        return SMALL_ROOM_RANGE
    return LARGE_ROOM_RANGE


# --- 节奏与昵称 -------------------------------------------------------------

#: Boom 界面停留时长（毫秒），到时后回到大厅
BOOM_DURATION_MS: int = _env_int("BOMB_BOOM_DURATION_MS", 3000)

#: 昵称最大长度（按去首尾空格后的字符数计）
NICKNAME_MAX_LEN: int = _env_int("BOMB_NICKNAME_MAX_LEN", 20)

#: 是否允许昵称重复。允许时内部仍以 playerId 区分身份，界面显示为「昵称(2)」
ALLOW_DUPLICATE_NICK: bool = _env_bool("BOMB_ALLOW_DUPLICATE_NICK", True)


# --- 房间容量 ---------------------------------------------------------------

#: 房间人数上限，None 表示不限制
MAX_PLAYERS: int | None = _env_optional_int("BOMB_MAX_PLAYERS", None)
