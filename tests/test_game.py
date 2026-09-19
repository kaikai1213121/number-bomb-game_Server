"""游戏规则单元测试。

覆盖需求 2、3 的全部规则分支，以及边界收缩、轮次推进、断线重算等异常路径。
本测试不依赖网络与服务端，可在任意装有 pytest 的环境直接运行：

    pytest tests/test_game.py -v
"""

from __future__ import annotations

import random

import pytest

from app import config
from app.game import GameError, GameRoom
from app.models import ErrorCode, GuessResult, Phase


# --- 需求 2：人数与范围 -------------------------------------------------------


class TestRangeSelection:
    """人数阈值决定数字范围，且在开局瞬间快照锁定。"""

    @pytest.mark.parametrize("count", [1, 2, 3, 4])
    def test_small_range_when_four_or_fewer(self, count: int) -> None:
        """≤4 人：范围 0-100。"""
        room = GameRoom(rng=lambda lo, hi: 50)
        for i in range(count):
            room.join_lobby(f"p{i}", f"玩家{i}")
        room.start_game("p0")
        assert (room.left, room.right) == (0, 100)
        assert room.player_count_snapshot == count

    @pytest.mark.parametrize("count", [5, 6, 10])
    def test_large_range_when_more_than_four(self, count: int) -> None:
        """>4 人：范围 0-1000。"""
        room = GameRoom(rng=lambda lo, hi: 500)
        for i in range(count):
            room.join_lobby(f"p{i}", f"玩家{i}")
        room.start_game("p0")
        assert (room.left, room.right) == (0, 1000)
        assert room.player_count_snapshot == count

    def test_boundary_is_exactly_four(self) -> None:
        """4 人是小范围的上界，5 人立刻切换到大范围。"""
        assert config.range_for_player_count(4) == (0, 100)
        assert config.range_for_player_count(5) == (0, 1000)

    def test_snapshot_locked_against_later_disconnect(self) -> None:
        """开局后有人断线，本局范围不受影响（人数已快照）。"""
        room = GameRoom(rng=lambda lo, hi: 500)
        for i in range(5):
            room.join_lobby(f"p{i}", f"玩家{i}")
        room.start_game("p0")
        assert (room.left, room.right) == (0, 1000)

        room.on_disconnect("p4")
        assert (room.left, room.right) == (0, 1000)
        assert room.player_count_snapshot == 5

    def test_next_round_resnapshots_count(self) -> None:
        """Boom 回到大厅后再次开始，人数重新快照（中途有人离开则范围随之变化）。"""
        room = GameRoom(rng=lambda lo, hi: 500)
        for i in range(5):
            room.join_lobby(f"p{i}", f"玩家{i}")
        room.start_game("p0")
        assert (room.left, room.right) == (0, 1000)

        room.guess("p0", 500)  # 命中，进入 BOOM
        assert room.phase is Phase.BOOM
        room.finish_boom()

        room.on_disconnect("p4")  # 剩 4 人
        room.start_game("p0")
        assert (room.left, room.right) == (0, 100)


# --- 需求 2：游戏顺序 ---------------------------------------------------------


class TestTurnOrder:
    """按加入先后顺序轮流，循环往复。"""

    def test_first_turn_is_first_joiner(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.join_lobby("c", "丙")
        room.start_game("a")
        assert room.current_player().id == "a"

    def test_turn_advances_in_join_order(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.join_lobby("c", "丙")
        room.start_game("a")

        room.guess("a", 10)
        assert room.current_player().id == "b"
        room.guess("b", 90)
        assert room.current_player().id == "c"
        room.guess("c", 20)
        # 回到第一位，形成循环
        assert room.current_player().id == "a"

    def test_any_player_can_start(self) -> None:
        """需求：任意玩家点击开始按钮即进入游戏。"""
        room = GameRoom(rng=lambda lo, hi: 42)
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("b")  # 非房主也能开局
        assert room.phase is Phase.PLAYING


# --- 需求 3：边界收缩与判定 ---------------------------------------------------


class TestBoundaryShrink:
    """闭区间严格收缩，炸弹恒在 [left, right] 内。"""

    def test_guess_lower_raises_left(self, room: GameRoom) -> None:
        """猜的数小于炸弹 → 左边界抬到 guess + 1。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 10)
        assert room.left == 11
        assert room.right == 100

    def test_guess_higher_lowers_right(self, room: GameRoom) -> None:
        """猜的数大于炸弹 → 右边界压到 guess - 1。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 90)
        assert room.left == 0
        assert room.right == 89

    def test_bomb_stays_inside_range(self, room: GameRoom) -> None:
        """每次收缩后炸弹仍在闭区间内 —— 核心不变量。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        for value in (10, 90, 50, 45, 43):
            room.guess("a", value)
            assert room.left <= room.bomb <= room.right

    def test_boundary_values_are_guessable(self, room: GameRoom) -> None:
        """闭区间语义：left 与 right 本身都是合法输入。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")

        room.guess("a", 0)   # 猜左边界，合法
        assert room.left == 1
        room.guess("b", 100)  # 猜右边界，合法
        assert room.right == 99

    def test_interval_shrinks_at_least_one_per_turn(self, room: GameRoom) -> None:
        """每次合法猜测至少让区间缩小 1，保证游戏必然结束。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        before = room.right - room.left
        room.guess("a", 0)
        assert (room.right - room.left) < before

    def test_game_terminates_within_finite_turns(self) -> None:
        """随机策略最多 N+1 回合内必然踩雷（区间每次至少缩 1）。"""
        rng = random.Random(20260915)
        for _ in range(200):
            room = GameRoom()
            room.join_lobby("a", "甲")
            room.start_game("a")
            span = room.right - room.left
            turns = 0
            while room.phase is Phase.PLAYING and turns <= span + 1:
                room.guess("a", rng.randint(room.left, room.right))
                turns += 1
            assert room.phase is Phase.BOOM, f"游戏未在有限回合内结束（{turns} 回合）"
            assert turns <= span + 1


# --- 需求 3：非法输入不消耗回合 -----------------------------------------------


class TestInvalidInput:
    """越界与非数字一律拒绝，且不推进回合。"""

    @pytest.mark.parametrize("value", [-1, 101, 999, -1000])
    def test_out_of_range_rejected(self, room: GameRoom, value: int) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")

        with pytest.raises(GameError) as info:
            room.guess("a", value)
        assert info.value.code is ErrorCode.OUT_OF_RANGE
        # 回合未推进，边界未变化
        assert room.current_player().id == "a"
        assert (room.left, room.right) == (0, 100)
        assert room.turn_count == 0

    def test_out_of_range_message_shows_current_bounds(self, room: GameRoom) -> None:
        """错误提示中的边界随收缩动态更新，与前端显示一致。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 90)  # right → 89
        with pytest.raises(GameError) as info:
            room.guess("a", 95)
        assert "0" in info.value.message and "89" in info.value.message

    @pytest.mark.parametrize("value", ["abc", "", None, 12.5, "12.5", [], {}])
    def test_non_integer_rejected(self, room: GameRoom, value: object) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")

        with pytest.raises(GameError) as info:
            room.guess("a", value)
        assert info.value.code is ErrorCode.INVALID_NUMBER
        assert room.current_player().id == "a"
        assert room.turn_count == 0

    @pytest.mark.parametrize("value", ["50", 50.0, " 50 ", True])
    def test_lenient_integer_forms_accepted_or_rejected(self, room: GameRoom, value: object) -> None:
        """整数字符串与整数浮点被接受；bool 被显式拒绝（避免 True 被当成 1）。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        if isinstance(value, bool):
            with pytest.raises(GameError) as info:
                room.guess("a", value)
            assert info.value.code is ErrorCode.INVALID_NUMBER
        else:
            room.guess("a", value)  # 不应抛错
            assert room.turn_count == 1

    def test_not_current_player_rejected(self, room: GameRoom) -> None:
        """非当前回合玩家提交被拒，且不影响回合。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")

        with pytest.raises(GameError) as info:
            room.guess("b", 50)
        assert info.value.code is ErrorCode.NOT_YOUR_TURN
        assert room.current_player().id == "a"

    def test_non_member_rejected(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        with pytest.raises(GameError) as info:
            room.guess("ghost", 50)
        assert info.value.code is ErrorCode.NOT_IN_ROOM

    def test_guess_before_start_rejected(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        with pytest.raises(GameError) as info:
            room.guess("a", 50)
        assert info.value.code is ErrorCode.INVALID_PHASE


# --- 需求 3：命中炸弹 ---------------------------------------------------------


class TestBoom:
    """命中后进入 BOOM，公布炸弹，回大厅时保留玩家与顺序。"""

    def test_hit_bomb_enters_boom_phase(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        outcome = room.guess("a", 42)

        assert outcome.result is GuessResult.BOOM
        assert outcome.bomb == 42
        assert outcome.boom_nickname == "甲"
        assert outcome.boom_player_id == "a"
        assert room.phase is Phase.BOOM

    def test_bomb_hidden_before_boom(self, room: GameRoom) -> None:
        """游戏进行中，状态快照不得泄露炸弹数字。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        room.guess("a", 10)

        for viewer in ("a", "b"):
            snapshot = room.snapshot(viewer)
            assert snapshot.game.bomb is None

    def test_bomb_revealed_at_boom(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 42)
        snapshot = room.snapshot("a")
        assert snapshot.game.bomb == 42
        assert snapshot.game.boom_nickname == "甲"

    def test_back_to_lobby_keeps_players_and_resets(self, room: GameRoom) -> None:
        """3 秒后回大厅：玩家列表与顺序保留，边界/炸弹/轮次重置。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        room.guess("a", 42)
        room.finish_boom()

        assert room.phase is Phase.LOBBY
        assert [p.id for p in room.players] == ["a", "b"]
        assert room.bomb is None
        assert room.current_index == 0
        assert room.last_guess is None
        assert room.turn_count == 0

        # 下一局仍从第一位玩家开始
        room.start_game("a")
        assert room.current_player().id == "a"

    def test_guess_during_boom_rejected(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 42)
        with pytest.raises(GameError) as info:
            room.guess("a", 10)
        assert info.value.code is ErrorCode.INVALID_PHASE

    def test_finish_boom_idempotent_when_not_boom(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.finish_boom()  # 非 BOOM 阶段调用应为空操作
        assert room.phase is Phase.PLAYING


# --- 需求 1：大厅状态机 -------------------------------------------------------


class TestLobbyStateMachine:
    """加入 / 开始 / 关闭大厅的状态流转与拒绝规则。"""

    def test_first_joiner_opens_lobby(self, room: GameRoom) -> None:
        assert room.phase is Phase.IDLE
        room.join_lobby("a", "甲")
        assert room.phase is Phase.LOBBY

    def test_join_rejected_while_playing(self, room: GameRoom) -> None:
        """需求：游戏开始后新的玩家点击加入会被拒绝。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        with pytest.raises(GameError) as info:
            room.join_lobby("new", "新人")
        assert info.value.code is ErrorCode.GAME_IN_PROGRESS
        assert room.get_player("new") is None

    def test_join_rejected_while_boom(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 42)
        with pytest.raises(GameError) as info:
            room.join_lobby("new", "新人")
        assert info.value.code is ErrorCode.GAME_IN_PROGRESS

    def test_close_lobby_returns_everyone_to_idle(self, room: GameRoom) -> None:
        """需求：点击关闭大厅则所有人退到最初主界面。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.close_lobby("b")

        assert room.phase is Phase.IDLE
        assert room.players == []
        assert room.snapshot("a").phase is Phase.IDLE
        assert room.snapshot("a").you.in_room is False

    def test_close_lobby_allowed_during_playing(self, room: GameRoom) -> None:
        """游戏中也能关闭，作为卡死兜底。"""
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.close_lobby("a")
        assert room.phase is Phase.IDLE

    def test_close_lobby_allowed_during_boom(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 42)
        room.close_lobby("a")
        assert room.phase is Phase.IDLE

    def test_close_lobby_requires_membership(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        with pytest.raises(GameError) as info:
            room.close_lobby("ghost")
        assert info.value.code is ErrorCode.NOT_IN_ROOM

    def test_start_requires_membership(self, room: GameRoom) -> None:
        with pytest.raises(GameError) as info:
            room.start_game("ghost")
        assert info.value.code is ErrorCode.NOT_IN_ROOM

    def test_start_requires_lobby_phase(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        with pytest.raises(GameError) as info:
            room.start_game("a")
        assert info.value.code is ErrorCode.INVALID_PHASE

    def test_rejoin_is_idempotent(self, room: GameRoom) -> None:
        """重复点击加入不会把自己加两次。"""
        room.join_lobby("a", "甲")
        room.join_lobby("a", "甲")
        assert len(room.players) == 1

    def test_rejoin_can_update_nickname(self, room: GameRoom) -> None:
        room.join_lobby("a", "旧名")
        room.join_lobby("a", "新名")
        assert len(room.players) == 1
        assert room.get_player("a").nickname == "新名"


# --- 昵称校验 -----------------------------------------------------------------


class TestNicknameValidation:
    """昵称去空格、非空、长度限制与重名处理。"""

    def test_nickname_trimmed(self, room: GameRoom) -> None:
        room.join_lobby("a", "  甲  ")
        assert room.get_player("a").nickname == "甲"

    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_invalid_nickname_rejected(self, room: GameRoom, bad: object) -> None:
        with pytest.raises(GameError) as info:
            room.join_lobby("a", bad)
        assert info.value.code is ErrorCode.INVALID_NICKNAME

    def test_too_long_nickname_rejected(self, room: GameRoom) -> None:
        with pytest.raises(GameError) as info:
            room.join_lobby("a", "甲" * (config.NICKNAME_MAX_LEN + 1))
        assert info.value.code is ErrorCode.INVALID_NICKNAME

    def test_max_length_nickname_accepted(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲" * config.NICKNAME_MAX_LEN)
        assert len(room.get_player("a").nickname) == config.NICKNAME_MAX_LEN

    def test_duplicate_nickname_allowed_by_default(self, room: GameRoom) -> None:
        """微信昵称本可能重名，默认允许，内部用 playerId 区分。"""
        room.join_lobby("a", "小明")
        room.join_lobby("b", "小明")
        assert len(room.players) == 2

    def test_duplicate_display_name_gets_ordinal(self, room: GameRoom) -> None:
        """重名时界面展示追加序号，避免玩家分不清彼此。"""
        room.join_lobby("a", "小明")
        room.join_lobby("b", "小明")
        room.join_lobby("c", "小红")
        assert room.display_name("a") == "小明(1)"
        assert room.display_name("b") == "小明(2)"
        assert room.display_name("c") == "小红"

    def test_duplicate_can_be_forbidden_by_config(self, room: GameRoom) -> None:
        config.ALLOW_DUPLICATE_NICK = False
        room.join_lobby("a", "小明")
        with pytest.raises(GameError) as info:
            room.join_lobby("b", "小明")
        assert info.value.code is ErrorCode.DUPLICATE_NICKNAME

    def test_room_full_rejected_when_limit_set(self, room: GameRoom) -> None:
        config.MAX_PLAYERS = 2
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        with pytest.raises(GameError) as info:
            room.join_lobby("c", "丙")
        assert info.value.code is ErrorCode.ROOM_FULL


# --- 断线处理 -----------------------------------------------------------------


class TestDisconnect:
    """断线移除玩家并重算轮次索引；全员断线则房间归零。"""

    def test_disconnect_removes_player(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        assert room.on_disconnect("b") is True
        assert room.get_player("b") is None

    def test_disconnect_skips_current_player_turn(self, room: GameRoom) -> None:
        """当前回合玩家断线 → 自动轮到下一位。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.join_lobby("c", "丙")
        room.start_game("a")
        assert room.current_player().id == "a"

        room.on_disconnect("a")
        assert room.current_player().id == "b"

    def test_disconnect_before_current_keeps_same_player(self, room: GameRoom) -> None:
        """断线者在当前玩家之前 → 索引左移，仍指向同一位玩家。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.join_lobby("c", "丙")
        room.start_game("a")
        room.guess("a", 10)   # 轮到 b
        room.guess("b", 90)   # 轮到 c
        assert room.current_player().id == "c"

        room.on_disconnect("a")  # 移除 c 之前的玩家
        assert room.current_player().id == "c"

    def test_disconnect_after_current_keeps_same_player(self, room: GameRoom) -> None:
        """断线者在当前玩家之后 → 索引不变。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.join_lobby("c", "丙")
        room.start_game("a")
        assert room.current_player().id == "a"

        room.on_disconnect("c")
        assert room.current_player().id == "a"

    def test_disconnect_wraps_index(self, room: GameRoom) -> None:
        """末位玩家断线时索引取模，不越界。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        room.guess("a", 10)  # 轮到 b（索引 1）
        room.on_disconnect("b")  # 只剩 a
        assert room.current_index == 0
        assert room.current_player().id == "a"

    def test_all_disconnected_returns_to_idle(self, room: GameRoom) -> None:
        """全员断线 → 房间回到主界面状态。"""
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        room.on_disconnect("a")
        room.on_disconnect("b")
        assert room.phase is Phase.IDLE
        assert room.players == []

    def test_disconnect_never_joined_returns_false(self, room: GameRoom) -> None:
        """连接存在但从未加入房间，断开时无需广播。"""
        room.join_lobby("a", "甲")
        assert room.on_disconnect("ghost") is False

    def test_disconnect_during_lobby(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.on_disconnect("a")
        assert room.phase is Phase.LOBBY
        assert len(room.players) == 1


# --- 状态快照 -----------------------------------------------------------------


class TestSnapshot:
    """快照按接收方个性化，且各阶段字段完整。"""

    def test_idle_snapshot(self, room: GameRoom) -> None:
        snapshot = room.snapshot("a")
        assert snapshot.phase is Phase.IDLE
        assert snapshot.room is None
        assert snapshot.game is None
        assert snapshot.you.in_room is False

    def test_lobby_snapshot_lists_players_in_order(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        snapshot = room.snapshot("b")
        assert snapshot.phase is Phase.LOBBY
        assert [p.id for p in snapshot.room.players] == ["a", "b"]
        assert snapshot.room.host_id == "a"
        assert snapshot.you.in_room is True

    def test_is_you_flag_per_viewer(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        snap_a = room.snapshot("a")
        snap_b = room.snapshot("b")
        assert [p.is_you for p in snap_a.room.players] == [True, False]
        assert [p.is_you for p in snap_b.room.players] == [False, True]

    def test_is_your_turn_flag_per_viewer(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        assert room.snapshot("a").game.is_your_turn is True
        assert room.snapshot("b").game.is_your_turn is False

    def test_playing_snapshot_has_no_current_after_boom(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.start_game("a")
        room.guess("a", 42)
        snapshot = room.snapshot("a")
        assert snapshot.phase is Phase.BOOM
        assert snapshot.game.current_player_id is None
        assert snapshot.game.is_your_turn is False

    def test_last_guess_recorded(self, room: GameRoom) -> None:
        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        room.guess("a", 10)
        last = room.snapshot("b").game.last_guess
        assert last.nickname == "甲"
        assert last.value == 10
        assert last.result is GuessResult.SAFE

    def test_serializes_to_camel_case(self, room: GameRoom) -> None:
        """前端约定 camelCase，确认关键字段名正确。"""
        from app.models import to_json

        room.join_lobby("a", "甲")
        room.join_lobby("b", "乙")
        room.start_game("a")
        room.guess("a", 10)
        data = to_json(room.snapshot("b"))

        assert data["phase"] == "playing"
        assert "isYourTurn" in data["game"]
        assert "currentPlayerNickname" in data["game"]
        assert "playerCount" in data["game"]
        assert "turnCount" in data["game"]
        assert "inRoom" in data["you"]
        assert "hostId" in data["room"]
        assert data["room"]["players"][0]["isYou"] is False


# --- 配置 ---------------------------------------------------------------------


class TestConfig:
    """参数可通过环境变量覆盖，解析失败时安全回退。"""

    def test_env_int_pair_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOMB_RANGE_SMALL", "5,50")
        assert config._env_int_pair("BOMB_RANGE_SMALL", (0, 100)) == (5, 50)

    @pytest.mark.parametrize("raw", ["", "abc", "1,2,3", "50,10", "x,y"])
    def test_env_int_pair_falls_back(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("BOMB_RANGE_SMALL", raw)
        assert config._env_int_pair("BOMB_RANGE_SMALL", (0, 100)) == (0, 100)

    @pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False)])
    def test_env_bool_parsing(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
        monkeypatch.setenv("BOMB_ALLOW_DUPLICATE_NICK", raw)
        assert config._env_bool("BOMB_ALLOW_DUPLICATE_NICK", True) is expected

    def test_env_optional_int_zero_means_unlimited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOMB_MAX_PLAYERS", "0")
        assert config._env_optional_int("BOMB_MAX_PLAYERS", None) is None

    def test_default_ranges_are_consistent(self) -> None:
        """默认配置下阈值与区间必须自洽。"""
        assert config.SMALL_ROOM_RANGE[0] <= config.SMALL_ROOM_RANGE[1]
        assert config.LARGE_ROOM_RANGE[0] <= config.LARGE_ROOM_RANGE[1]
        assert config.SMALL_ROOM_MAX_PLAYERS >= 1
        assert config.BOOM_DURATION_MS > 0
