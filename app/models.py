"""通信协议的数据模型与枚举定义。

设计约定：
- 字段在 Python 侧使用 snake_case，序列化时通过 ``to_camel`` 别名生成器输出
  camelCase，与前端约定一致（``isYou`` / ``hostId`` / ``serverTime``）。
- 客户端入参中 ``value`` 使用 ``Any`` 而非 ``int``，是为了让「非整数」这类
  非法输入能由游戏内核返回业务错误码（``INVALID_NUMBER``），而不是被 Pydantic
  提前拦成 422，从而保证前端能收到统一格式的错误提示。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

# --- 枚举 -------------------------------------------------------------------


class Phase(str, Enum):
    """全局房间状态机的四个阶段，前端据此切换界面。"""

    IDLE = "idle"        # 主界面：当下没有游戏进行
    LOBBY = "lobby"      # 大厅：等待玩家开始游戏
    PLAYING = "playing"  # 游戏中：按加入顺序轮流猜数
    BOOM = "boom"        # 有人踩雷：展示 Boom 界面，倒计时后回大厅


class ErrorCode(str, Enum):
    """定向错误消息的错误码，只发给操作者，不广播。"""

    INVALID_ACTION = "INVALID_ACTION"        # 未知的 action
    INVALID_NICKNAME = "INVALID_NICKNAME"    # 昵称为空或超长
    DUPLICATE_NICKNAME = "DUPLICATE_NICKNAME"  # 昵称重复（仅在不允许重名时）
    GAME_IN_PROGRESS = "GAME_IN_PROGRESS"    # 游戏进行中，拒绝加入大厅
    ROOM_FULL = "ROOM_FULL"                  # 房间人数已达上限
    NOT_IN_ROOM = "NOT_IN_ROOM"              # 尚未加入大厅就操作
    NOT_YOUR_TURN = "NOT_YOUR_TURN"          # 还没轮到你
    OUT_OF_RANGE = "OUT_OF_RANGE"            # 数字越界
    INVALID_NUMBER = "INVALID_NUMBER"        # 不是合法整数
    INVALID_PHASE = "INVALID_PHASE"          # 当前阶段不允许该操作
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"  # 消息不是合法 JSON 对象


class GuessResult(str, Enum):
    """一次合法猜测的判定结果。"""

    SAFE = "safe"  # 未命中，边界收缩，回合推进
    BOOM = "boom"  # 命中炸弹，本局结束


class ClientAction(str, Enum):
    """客户端可发送的 action 白名单。"""

    JOIN_LOBBY = "join_lobby"
    START_GAME = "start_game"
    CLOSE_LOBBY = "close_lobby"
    GUESS = "guess"
    PING = "ping"


# --- 消息基类 ---------------------------------------------------------------


class _CamelModel(BaseModel):
    """统一使用 camelCase 序列化的基类。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


def to_json(model: BaseModel) -> dict[str, Any]:
    """把模型序列化为可直接 ``json.dumps`` 的 dict（camelCase + JSON 兼容类型）。"""
    return model.model_dump(by_alias=True, mode="json", exclude_none=False)


# --- 客户端 → 服务端 ---------------------------------------------------------


class ClientMessage(_CamelModel):
    """客户端上行消息。

    ``action`` 之外的字段均为可选，由具体 action 决定是否使用。
    """

    action: str
    nickname: str | None = None
    value: Any = None

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, v: Any) -> Any:
        """action 去首尾空格并转小写，容错前端大小写差异。"""
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("nickname", mode="before")
    @classmethod
    def _normalize_nickname(cls, v: Any) -> Any:
        """昵称仅做去首尾空格处理，长度与非空校验交由游戏内核判定，
        以便返回业务错误码而非 422。"""
        if isinstance(v, str):
            return v.strip()
        return v

    def known_action(self) -> ClientAction | None:
        """返回白名单内的 action 枚举，未知 action 返回 None。"""
        try:
            return ClientAction(self.action)
        except ValueError:
            return None


# --- 服务端 → 客户端 ---------------------------------------------------------


class YouView(_CamelModel):
    """当前连接自身的身份信息。"""

    id: str
    nickname: str | None = None
    in_room: bool = False


class PlayerView(_CamelModel):
    """玩家列表中单个玩家的视图。"""

    id: str
    nickname: str
    online: bool = True
    is_you: bool = False


class RoomView(_CamelModel):
    """房间视图，仅在 LOBBY / PLAYING / BOOM 阶段有值。"""

    players: list[PlayerView] = Field(default_factory=list)
    host_id: str | None = None


class LastGuessView(_CamelModel):
    """上一次合法猜测，用于前端展示「谁猜了多少」。"""

    nickname: str
    value: int
    result: GuessResult


class GameView(_CamelModel):
    """对局视图，仅在 PLAYING / BOOM 阶段有值。"""

    left: int
    right: int
    player_count: int
    #: 本局累计的合法猜测次数（非法输入不计入）
    turn_count: int = 0
    current_player_id: str | None = None
    current_player_nickname: str | None = None
    is_your_turn: bool = False
    last_guess: LastGuessView | None = None
    boom_nickname: str | None = None
    #: 炸弹数字，仅在 BOOM 阶段公布；游戏过程中恒为 None，防止前端作弊
    bomb: int | None = None


class StateMessage(_CamelModel):
    """全局状态广播，每次状态变化推给全部连接。"""

    type: Literal["state"] = "state"
    phase: Phase
    you: YouView
    room: RoomView | None = None
    game: GameView | None = None
    server_time: int = 0


class ErrorMessage(_CamelModel):
    """定向错误消息，只发给触发错误的连接。"""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str


class BoomMessage(_CamelModel):
    """Boom 提示消息，前端据此展示界面并做倒计时。"""

    type: Literal["boom"] = "boom"
    nickname: str
    bomb: int
    back_to_lobby_in_ms: int = 3000


class PongMessage(_CamelModel):
    """心跳应答。"""

    type: Literal["pong"] = "pong"
    server_time: int = 0


class WelcomeMessage(_CamelModel):
    """连接建立后的首条消息，下发 playerId 与当前状态。"""

    type: Literal["welcome"] = "welcome"
    player_id: str
    server_time: int = 0
