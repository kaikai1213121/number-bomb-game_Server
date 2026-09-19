"""游戏内核：纯逻辑状态机，不含任何 IO。

设计原则
--------
本模块不导入 FastAPI、不碰网络、不碰全局变量。所有规则计算（阶段转换、人数
快照定范围、闭区间严格收缩、轮次推进、断线重算）都表现为对 :class:`GameRoom`
的一次同步方法调用。这样规则可以用 pytest 直接覆盖，无需起服务。

线程/并发安全
-------------
所有方法都是同步且不含 ``await``。在 FastAPI 的事件循环中，一次方法调用会原子
执行完毕，不会被其他连接的请求打断，因此无需额外加锁。``ws.py`` 在调用本模块
前后不得插入 ``await``。

核心不变量
----------
炸弹数字 ``bomb`` 恒落在闭区间 ``[left, right]`` 内。每次合法且未命中的猜测都会
让区间至少缩小 1（``left = guess + 1`` 或 ``right = guess - 1``），因此游戏必然
在有限回合内结束，不会死循环；同时前端提示的「请输入从 left - right」与实际
合法输入范围完全一致，不会出现按提示输入却被判非法的情况。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import config
from .models import (
    ErrorCode,
    GameView,
    GuessResult,
    LastGuessView,
    Phase,
    PlayerView,
    RoomView,
    StateMessage,
    YouView,
)

RandomFn = Callable[[int, int], int]


class GameError(Exception):
    """业务规则错误，携带错误码与用户可见的中文提示。

    ``ws.py`` 捕获本异常后转成定向 :class:`~app.models.ErrorMessage` 只发给
    操作者，不广播，避免污染全局状态。
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Player:
    """房间内的一名玩家。

    ``id`` 由服务端在连接建立时分配，是玩家身份的唯一依据；昵称允许重复，
    因此界面展示统一走 :meth:`GameRoom.display_name`。
    """

    id: str
    nickname: str
    online: bool = True


@dataclass
class GuessOutcome:
    """一次合法猜测的判定结果。

    ``ws.py`` 据此决定是否需要额外下发 BoomMessage 并安排回大厅的定时任务。
    """

    result: GuessResult
    left: int
    right: int
    guess: int
    #: 仅在 result == BOOM 时有值
    boom_nickname: str | None = None
    bomb: int | None = None
    #: 命中后本局结束时，踩雷玩家在原列表中的位置，便于前端定位
    boom_player_id: str | None = None


class GameRoom:
    """全局单房间状态机。

    同一时刻只存在一个大厅或一局游戏，对应需求中「当下没有游戏进行时」的描述。
    """

    def __init__(self, rng: RandomFn | None = None) -> None:
        #: 随机源，可注入以便测试复现特定炸弹数字
        self._rng: RandomFn = rng or random.randint
        self.phase: Phase = Phase.IDLE
        self.players: list[Player] = []
        #: 当前回合玩家在 self.players 中的索引，仅 PLAYING 阶段有意义
        self.current_index: int = 0
        self.left: int = 0
        self.right: int = 0
        self.bomb: int | None = None
        #: 开局瞬间的人数快照，决定本局数字范围
        self.player_count_snapshot: int = 0
        self.last_guess: tuple[str, int, GuessResult] | None = None
        self.boom_nickname: str | None = None
        self.boom_player_id: str | None = None
        #: 本局累计回合数，便于前端展示与测试断言
        self.turn_count: int = 0

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    @property
    def in_room(self) -> bool:
        """房间是否处于非主界面状态（即存在玩家）。"""
        return self.phase is not Phase.IDLE and bool(self.players)

    def get_player(self, player_id: str) -> Player | None:
        """按 id 查找玩家，不存在返回 None。"""
        for player in self.players:
            if player.id == player_id:
                return player
        return None

    def current_player(self) -> Player | None:
        """当前回合玩家，仅 PLAYING 阶段返回有效值。"""
        if self.phase is not Phase.PLAYING or not self.players:
            return None
        if not 0 <= self.current_index < len(self.players):
            return None
        return self.players[self.current_index]

    def display_name(self, player_id: str) -> str:
        """玩家的界面展示名。

        昵称重复时追加序号，例如「小明」「小明(2)」「小明(3)」；不重复时原样返回。
        """
        target = self.get_player(player_id)
        if target is None:
            return ""
        same_nick = [p for p in self.players if p.nickname == target.nickname]
        if len(same_nick) <= 1:
            return target.nickname
        ordinal = same_nick.index(target) + 1
        return f"{target.nickname}({ordinal})"

    # ------------------------------------------------------------------
    # 昵称与输入校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_nickname(nickname: Any) -> str:
        """校验昵称，返回去空格后的字符串，非法时抛 GameError。"""
        if not isinstance(nickname, str):
            raise GameError(ErrorCode.INVALID_NICKNAME, "请输入微信昵称")
        cleaned = nickname.strip()
        if not cleaned:
            raise GameError(ErrorCode.INVALID_NICKNAME, "昵称不能为空，请输入微信昵称")
        if len(cleaned) > config.NICKNAME_MAX_LEN:
            raise GameError(
                ErrorCode.INVALID_NICKNAME,
                f"昵称过长，最多 {config.NICKNAME_MAX_LEN} 个字符",
            )
        return cleaned

    @staticmethod
    def _coerce_int(value: Any) -> int:
        """把上行 value 转为整数，非法时抛 GameError(INVALID_NUMBER)。

        显式拒绝 ``bool``（Python 中 ``isinstance(True, int)`` 为真），拒绝非整数
        浮点（如 50.5），接受整数字符串与整数浮点（"50"、50.0）以兼容前端传参差异。
        """
        if isinstance(value, bool):
            raise GameError(ErrorCode.INVALID_NUMBER, "请输入一个整数")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise GameError(ErrorCode.INVALID_NUMBER, "请输入一个整数，不能带小数")
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise GameError(ErrorCode.INVALID_NUMBER, "请输入一个数字")
            try:
                return int(text)
            except ValueError:
                pass
            try:
                number = float(text)
            except ValueError:
                raise GameError(ErrorCode.INVALID_NUMBER, "请输入一个整数") from None
            if number.is_integer():
                return int(number)
            raise GameError(ErrorCode.INVALID_NUMBER, "请输入一个整数，不能带小数")
        raise GameError(ErrorCode.INVALID_NUMBER, "请输入一个整数")

    def _require_in_room(self, player_id: str) -> Player:
        """要求操作者已在房间内，否则抛 GameError。"""
        player = self.get_player(player_id)
        if player is None:
            raise GameError(ErrorCode.NOT_IN_ROOM, "请先输入昵称加入游戏大厅")
        return player

    # ------------------------------------------------------------------
    # 状态转移：加入大厅
    # ------------------------------------------------------------------

    def join_lobby(self, player_id: str, nickname: Any) -> None:
        """玩家加入大厅。

        - 游戏进行中（PLAYING / BOOM）拒绝新玩家加入；
        - IDLE 阶段首个加入者把房间推进到 LOBBY，并成为房主（即 players[0]）；
        - 已在房间内的玩家重复调用为幂等空操作，避免前端重复点击造成异常。
        """
        if self.phase in (Phase.PLAYING, Phase.BOOM):
            raise GameError(ErrorCode.GAME_IN_PROGRESS, "游戏进行中，暂时无法加入大厅")

        cleaned = self._validate_nickname(nickname)

        # 已在房间内：幂等处理，允许顺带更新昵称
        existing = self.get_player(player_id)
        if existing is not None:
            existing.nickname = cleaned
            return

        if config.MAX_PLAYERS is not None and len(self.players) >= config.MAX_PLAYERS:
            raise GameError(ErrorCode.ROOM_FULL, "大厅人数已满，请稍后再试")

        if not config.ALLOW_DUPLICATE_NICK:
            for player in self.players:
                if player.nickname == cleaned:
                    raise GameError(ErrorCode.DUPLICATE_NICKNAME, "该昵称已被使用，请换一个")

        self.players.append(Player(id=player_id, nickname=cleaned))
        if self.phase is Phase.IDLE:
            self.phase = Phase.LOBBY

    # ------------------------------------------------------------------
    # 状态转移：开始游戏
    # ------------------------------------------------------------------

    def start_game(self, player_id: str) -> None:
        """任意房间内玩家点击开始，进入对局。

        人数在此刻快照锁定并据此选定数字范围；游戏开始后的断线不再改变范围。
        玩家顺序即加入先后顺序，第一局从 players[0] 开始。
        """
        self._require_in_room(player_id)
        if self.phase is not Phase.LOBBY:
            raise GameError(ErrorCode.INVALID_PHASE, "当前不在大厅，无法开始游戏")

        self.player_count_snapshot = len(self.players)
        self.left, self.right = config.range_for_player_count(self.player_count_snapshot)
        self.bomb = self._rng(self.left, self.right)
        self.current_index = 0
        self.last_guess = None
        self.turn_count = 0
        self.boom_nickname = None
        self.boom_player_id = None
        self.phase = Phase.PLAYING

    # ------------------------------------------------------------------
    # 状态转移：猜数字
    # ------------------------------------------------------------------

    def guess(self, player_id: str, value: Any) -> GuessOutcome:
        """当前回合玩家提交猜测。

        非法输入（非整数 / 越界 / 非当前玩家）一律抛 GameError 且**不推进回合**，
        同一玩家可继续输入。合法输入后判定：命中则进入 BOOM；未命中则收缩边界
        并把回合交给下一位玩家。
        """
        if self.phase is Phase.BOOM:
            raise GameError(ErrorCode.INVALID_PHASE, "本局已结束，请等待回到大厅")
        if self.phase is not Phase.PLAYING:
            raise GameError(ErrorCode.INVALID_PHASE, "游戏尚未开始")

        # 校验操作者确实在房间内，使其得到 NOT_IN_ROOM 而非 NOT_YOUR_TURN
        self._require_in_room(player_id)
        current = self.current_player()
        if current is None or current.id != player_id:
            raise GameError(ErrorCode.NOT_YOUR_TURN, "还没轮到你，请耐心等待")

        number = self._coerce_int(value)
        if number < self.left or number > self.right:
            raise GameError(
                ErrorCode.OUT_OF_RANGE,
                f"请输入 {self.left} - {self.right} 之间的整数",
            )

        # 炸弹不变量校验放在计数自增之前，避免抛错时留下被污染的 turn_count。
        # 这里用 RuntimeError 而非 assert：assert 在 python -O 下会被剥离，
        # 届时将退化成 int 与 None 比较，抛出难以定位的 TypeError。
        bomb = self.bomb
        if bomb is None:
            raise RuntimeError("内部状态异常：PLAYING 阶段炸弹未生成")

        self.turn_count += 1

        if number == bomb:
            self.phase = Phase.BOOM
            self.boom_nickname = self.display_name(player_id)
            self.boom_player_id = player_id
            self.last_guess = (self.boom_nickname, number, GuessResult.BOOM)
            return GuessOutcome(
                result=GuessResult.BOOM,
                left=self.left,
                right=self.right,
                guess=number,
                boom_nickname=self.boom_nickname,
                bomb=bomb,
                boom_player_id=player_id,
            )

        # 未命中：闭区间严格收缩，保证 bomb 始终落在 [left, right] 内
        if number < bomb:
            self.left = number + 1
        else:
            self.right = number - 1

        self.last_guess = (self.display_name(player_id), number, GuessResult.SAFE)
        self._advance_turn()

        return GuessOutcome(
            result=GuessResult.SAFE,
            left=self.left,
            right=self.right,
            guess=number,
        )

    def _advance_turn(self) -> None:
        """把回合交给下一位玩家，按加入顺序循环。"""
        if not self.players:
            return
        self.current_index = (self.current_index + 1) % len(self.players)

    # ------------------------------------------------------------------
    # 状态转移：Boom 结束 / 关闭大厅
    # ------------------------------------------------------------------

    def finish_boom(self) -> None:
        """Boom 展示时长结束后回到大厅。

        保留玩家列表与顺序，重置边界、炸弹与轮次索引，下一局仍从第 1 位玩家开始。
        人数范围将在下次 start_game 时重新快照，因此中途进出会影响下一局范围。
        """
        if self.phase is not Phase.BOOM:
            return
        self.phase = Phase.LOBBY
        self.bomb = None
        self.left = 0
        self.right = 0
        self.current_index = 0
        self.last_guess = None
        self.turn_count = 0
        self.boom_nickname = None
        self.boom_player_id = None
        self.player_count_snapshot = 0

    def close_lobby(self, player_id: str) -> None:
        """关闭大厅，所有人退回最初的主界面。

        允许在 LOBBY / PLAYING / BOOM 任意阶段触发，作为异常卡死的兜底出口。
        玩家列表被清空，但 WebSocket 连接保持存活，玩家可重新输入昵称加入。
        """
        self._require_in_room(player_id)
        self.reset_to_idle()

    def reset_to_idle(self) -> None:
        """无条件把房间重置回主界面状态（全员断线、关闭大厅等场景共用）。"""
        self.phase = Phase.IDLE
        self.players.clear()
        self.current_index = 0
        self.left = 0
        self.right = 0
        self.bomb = None
        self.last_guess = None
        self.turn_count = 0
        self.boom_nickname = None
        self.boom_player_id = None
        self.player_count_snapshot = 0

    # ------------------------------------------------------------------
    # 状态转移：断线
    # ------------------------------------------------------------------

    def on_disconnect(self, player_id: str) -> bool:
        """处理玩家断线：从房间移除并重算轮次索引。

        返回 ``True`` 表示本次移除对对局产生了影响（需要广播新状态）。

        索引重算规则（移除元素后列表左移）：
        - 移除的是当前回合玩家 → 索引保持不变，即自动轮到下一位；
        - 移除的在当前玩家之前 → 索引减 1，保证仍指向同一位玩家；
        - 移除的在当前玩家之后 → 索引不变。
        最后按玩家数取模，防止越界。全员断线时房间回到 IDLE。
        """
        index = next(
            (i for i, p in enumerate(self.players) if p.id == player_id),
            None,
        )
        if index is None:
            # 连接存在但从未加入房间，无需广播
            return False

        was_current = self.phase is Phase.PLAYING and index == self.current_index
        self.players.pop(index)

        if not self.players:
            self.reset_to_idle()
            return True

        if self.phase is Phase.PLAYING:
            if index < self.current_index:
                self.current_index -= 1
            self.current_index %= len(self.players)

        return True

    # ------------------------------------------------------------------
    # 状态快照（供 ws.py 广播）
    # ------------------------------------------------------------------

    def snapshot(self, viewer_id: str, server_time: int | None = None) -> StateMessage:
        """为指定连接生成状态广播消息。

        ``is_you`` / ``isYourTurn`` 等字段按 viewer 计算，因此每个连接收到的
        内容略有不同，但整体状态一致。炸弹数字仅在 BOOM 阶段公布。
        """
        stamp = int(time.time() * 1000) if server_time is None else server_time

        viewer = self.get_player(viewer_id)
        you = YouView(
            id=viewer_id,
            nickname=viewer.nickname if viewer else None,
            in_room=viewer is not None,
        )

        room: RoomView | None = None
        game: GameView | None = None

        if self.phase is not Phase.IDLE and self.players:
            room = RoomView(
                players=[
                    PlayerView(
                        id=p.id,
                        nickname=self.display_name(p.id),
                        online=p.online,
                        is_you=(p.id == viewer_id),
                    )
                    for p in self.players
                ],
                host_id=self.players[0].id,
            )

        if self.phase in (Phase.PLAYING, Phase.BOOM):
            current = self.current_player() if self.phase is Phase.PLAYING else None
            last: LastGuessView | None = None
            if self.last_guess is not None:
                nick, value, result = self.last_guess
                last = LastGuessView(nickname=nick, value=value, result=result)

            game = GameView(
                left=self.left,
                right=self.right,
                player_count=self.player_count_snapshot,
                turn_count=self.turn_count,
                current_player_id=current.id if current else None,
                current_player_nickname=self.display_name(current.id) if current else None,
                is_your_turn=bool(current and current.id == viewer_id),
                last_guess=last,
                boom_nickname=self.boom_nickname if self.phase is Phase.BOOM else None,
                # 炸弹只在 Boom 时公布，游戏过程中恒为 None，防止前端作弊
                bomb=self.bomb if self.phase is Phase.BOOM else None,
            )

        return StateMessage(
            phase=self.phase,
            you=you,
            room=room,
            game=game,
            server_time=stamp,
        )
