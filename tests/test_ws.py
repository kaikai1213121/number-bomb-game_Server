"""WebSocket 端到端流程测试。

用 FastAPI TestClient 建立真实 WebSocket 连接，覆盖方案 P4 阶段列出的全部验证场景。
运行前需要安装测试依赖（见 requirements.txt）：

    pip install -r requirements.txt
    pytest tests/test_ws.py -v

两个关键约定
------------
1. ``app.ws.game_room`` 是模块级单例，因此每个用例前后都要重置，避免相互污染。
2. ``TestClient`` 必须以**上下文管理器**方式使用。否则每个 ``websocket_connect``
   会各自创建独立的事件循环，而广播是跨连接操作，多连接场景会失败或挂起。共享
   单一 portal 后，所有连接运行在同一个事件循环中，Boom 定时任务与断线清理才能
   正常触发。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.models import Phase
from app.ws import cancel_boom_task, game_room, manager

#: 测试中固定的炸弹数字，使「命中」与「避开」两类分支都可确定性触发
BOMB = 42


# --- 测试夹具 ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_room() -> Iterator[None]:
    """每个用例前后重置全局房间，保证用例相互独立。"""
    game_room.reset_to_idle()
    game_room._rng = lambda lo, hi: BOMB  # noqa: SLF001 - 测试需要确定性炸弹
    yield
    cancel_boom_task()
    game_room.reset_to_idle()


@pytest.fixture
def fast_boom(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 Boom 停留时长压到 0.2 秒，避免测试等待真实 3 秒。"""
    monkeypatch.setattr(config, "BOOM_DURATION_MS", 200)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """以上下文管理器方式提供 TestClient，使所有连接共享同一事件循环。"""
    with TestClient(app) as test_client:
        yield test_client


# --- 消息收发辅助 -----------------------------------------------------------


def act(ws: Any, action: str, **fields: Any) -> None:
    """发送一条客户端消息。"""
    ws.send_json({"action": action, **fields})


def _read_until(ws: Any, wanted: str, limit: int) -> dict[str, Any]:
    """持续读取消息，直到拿到指定 type 的那一条。

    服务端在状态广播之外还会下发 error / boom / pong 等定向消息，顺序不固定，
    因此这里统一用「跳过无关消息」的方式取目标消息。
    """
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == wanted:
            return message
    raise AssertionError(f"未能收到 {wanted} 消息")


def next_state(ws: Any, limit: int = 15) -> dict[str, Any]:
    """读取消息直到拿到一条 state 广播。"""
    return _read_until(ws, "state", limit)


def next_error(ws: Any, limit: int = 8) -> dict[str, Any]:
    """读取消息直到拿到一条 error。"""
    return _read_until(ws, "error", limit)


def next_boom(ws: Any, limit: int = 8) -> dict[str, Any]:
    """读取消息直到拿到一条 boom 提示。"""
    return _read_until(ws, "boom", limit)


class Conn:
    """WebSocket 测试会话的幂等包装。

    断线类用例需要**主动**关闭某个连接来模拟掉线，而测试收尾时又要统一关闭全部
    连接。若不做幂等保护，同一个会话会被关闭两次，第二次发送 disconnect 时服务端
    已退出、无人消费，调用会永久阻塞。这里用 ``closed`` 标记确保每个会话只关一次。

    其余属性（``send_json`` / ``receive_json`` / ``send_text``）通过 ``__getattr__``
    透明委托给底层会话，测试代码无需感知包装的存在。
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def close(self) -> None:
        """关闭底层会话，重复调用为空操作。"""
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            self._session.close()


def close_all(connections: list[Conn]) -> None:
    """统一关闭一组连接，顺序与是否已关闭都安全。"""
    for connection in connections:
        connection.close()


def open_connection(client: TestClient, pool: list[Conn]) -> Conn:
    """建立一个连接并消费掉握手时的 welcome + state 两条消息。

    新连接会被登记到 ``pool``，由调用方通过 :func:`close_all` 统一收尾。
    """
    session = client.websocket_connect("/ws").__enter__()
    connection = Conn(session)
    pool.append(connection)

    welcome = connection.receive_json()
    assert welcome["type"] == "welcome"
    assert welcome["playerId"]
    initial = connection.receive_json()
    assert initial["type"] == "state"
    return connection


@contextlib.contextmanager
def one_connection(client: TestClient) -> Iterator[Conn]:
    """单连接场景的便捷封装。"""
    pool: list[Conn] = []
    try:
        yield open_connection(client, pool)
    finally:
        close_all(pool)


@contextlib.contextmanager
def connections(client: TestClient, count: int) -> Iterator[list[Conn]]:
    """建立 count 个连接但**不加入大厅**，退出时统一关闭。

    用于需要「旁观者」视角的场景：连接存在、但尚未成为房间成员。
    """
    pool: list[Conn] = []
    try:
        for _ in range(count):
            open_connection(client, pool)
        yield pool
    finally:
        close_all(pool)


@contextlib.contextmanager
def lobby(client: TestClient, names: list[str]) -> Iterator[list[Conn]]:
    """建立多个连接并依次加入大厅，退出时统一关闭。

    每次加入都会向所有已有连接广播一次状态，这里逐个消费掉以保持消息队列干净。
    """
    pool: list[Conn] = []
    try:
        for nickname in names:
            connection = open_connection(client, pool)
            act(connection, "join_lobby", nickname=nickname)
            # 已存在的连接各收到一次广播，逐个消费
            for existing in pool[:-1]:
                next_state(existing)
            next_state(connection)
        yield pool
    finally:
        close_all(pool)


# --- 连接与握手 -------------------------------------------------------------


def test_connect_receives_welcome_and_state(client: TestClient) -> None:
    with one_connection(client) as ws:
        # open_connection 已断言 welcome 与首条 state，这里验证心跳应答
        act(ws, "ping")
        pong = _read_until(ws, "pong", 5)
        assert pong["serverTime"] > 0


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["phase"] == "idle"
    assert body["playerCount"] == 0


def test_state_endpoint_never_leaks_bomb(client: TestClient) -> None:
    """调试用的 /api/state 不得暴露炸弹数字。"""
    with one_connection(client) as ws:
        act(ws, "join_lobby", nickname="甲")
        next_state(ws)
        act(ws, "start_game")
        next_state(ws)

        body = client.get("/api/state").json()
        assert body["phase"] == "playing"
        assert body["game"]["bomb"] is None
        assert body["game"]["left"] == 0


def test_config_endpoint(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body["smallRoomMaxPlayers"] == config.SMALL_ROOM_MAX_PLAYERS
    assert body["smallRoomRange"] == list(config.SMALL_ROOM_RANGE)
    assert body["largeRoomRange"] == list(config.LARGE_ROOM_RANGE)
    assert body["boomDurationMs"] == config.BOOM_DURATION_MS


def test_index_page_served(client: TestClient) -> None:
    """前端单页可由后端直接托管。"""
    response = client.get("/")
    assert response.status_code == 200
    assert "数字炸弹" in response.text


def test_unknown_action_rejected(client: TestClient) -> None:
    with one_connection(client) as ws:
        act(ws, "do_something_weird")
        error = next_error(ws)
        assert error["code"] == "INVALID_ACTION"


def test_malformed_json_rejected(client: TestClient) -> None:
    with one_connection(client) as ws:
        ws.send_text("this is not json")
        error = next_error(ws)
        assert error["code"] == "MALFORMED_MESSAGE"


def test_non_object_json_rejected(client: TestClient) -> None:
    with one_connection(client) as ws:
        ws.send_text("[1, 2, 3]")
        error = next_error(ws)
        assert error["code"] == "MALFORMED_MESSAGE"


# --- 需求 1：大厅流转 -------------------------------------------------------


def test_join_lobby_broadcasts_to_all(client: TestClient) -> None:
    """加入大厅后所有连接都能看到玩家列表，但 isYou 按接收方个性化。"""
    with connections(client, 2) as (a, b):
        act(a, "join_lobby", nickname="甲")
        state_a = next_state(a)
        state_b = next_state(b)

        assert state_a["phase"] == "lobby"
        assert [p["nickname"] for p in state_a["room"]["players"]] == ["甲"]
        assert state_b["phase"] == "lobby"
        assert len(state_b["room"]["players"]) == 1
        assert state_a["room"]["players"][0]["isYou"] is True
        assert state_b["room"]["players"][0]["isYou"] is False
        assert state_b["you"]["inRoom"] is False


def test_start_game_moves_everyone_to_playing(client: TestClient) -> None:
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        state_a = next_state(a)
        state_b = next_state(b)
        assert state_a["phase"] == "playing"
        assert state_b["phase"] == "playing"
        assert state_a["game"]["isYourTurn"] is True
        assert state_b["game"]["isYourTurn"] is False
        assert state_a["game"]["currentPlayerNickname"] == "甲"


def test_non_host_can_start_game(client: TestClient) -> None:
    """需求：任意玩家点击开始按钮即进入游戏。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(b, "start_game")  # 乙不是房主也能开局
        assert next_state(a)["phase"] == "playing"
        assert next_state(b)["phase"] == "playing"


def test_close_lobby_returns_everyone_to_idle(client: TestClient) -> None:
    """需求：点击关闭大厅则所有人退到最初主界面。"""
    with lobby(client, ["甲", "乙", "丙"]) as (a, b, c):
        act(b, "close_lobby")  # 任意玩家都能关闭
        for ws in (a, b, c):
            state = next_state(ws)
            assert state["phase"] == "idle"
            assert state["you"]["inRoom"] is False
            assert state["room"] is None
        assert game_room.players == []


def test_close_lobby_during_playing(client: TestClient) -> None:
    """游戏中也能关闭大厅，作为卡死兜底。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(b, "close_lobby")
        assert next_state(a)["phase"] == "idle"
        assert next_state(b)["phase"] == "idle"


def test_join_rejected_while_playing(client: TestClient) -> None:
    """需求：游戏开始后新玩家点击加入会被拒绝。"""
    with lobby(client, ["甲", "乙"]) as (a, b), one_connection(client) as late:
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(late, "join_lobby", nickname="迟到者")
        error = next_error(late)
        assert error["code"] == "GAME_IN_PROGRESS"
        assert "游戏进行中" in error["message"]

        # 房间内人数未变化，迟到者未被加入
        assert len(game_room.players) == 2
        assert all(p.nickname != "迟到者" for p in game_room.players)


def test_join_rejected_while_boom(client: TestClient, fast_boom: None) -> None:
    """Boom 展示期间同样拒绝新玩家加入。"""
    with lobby(client, ["甲"]) as (a,), one_connection(client) as late:
        act(a, "start_game")
        next_state(a)
        act(a, "guess", value=BOMB)
        next_boom(a)

        act(late, "join_lobby", nickname="迟到者")
        error = next_error(late)
        assert error["code"] == "GAME_IN_PROGRESS"


def test_nickname_validation_over_ws(client: TestClient) -> None:
    with one_connection(client) as ws:
        act(ws, "join_lobby", nickname="   ")
        error = next_error(ws)
        assert error["code"] == "INVALID_NICKNAME"
        assert game_room.phase is Phase.IDLE

        act(ws, "join_lobby", nickname="甲" * (config.NICKNAME_MAX_LEN + 1))
        error = next_error(ws)
        assert error["code"] == "INVALID_NICKNAME"


def test_start_game_requires_membership(client: TestClient) -> None:
    """未加入大厅直接点开始会被拒绝。"""
    with one_connection(client) as ws:
        act(ws, "start_game")
        error = next_error(ws)
        assert error["code"] == "NOT_IN_ROOM"


# --- 需求 2：人数决定范围 ---------------------------------------------------


@pytest.mark.parametrize("names,expected", [
    (["甲"], (0, 100)),
    (["甲", "乙", "丙", "丁"], (0, 100)),
    (["甲", "乙", "丙", "丁", "戊"], (0, 1000)),
    (["甲", "乙", "丙", "丁", "戊", "己"], (0, 1000)),
])
def test_range_by_player_count_over_ws(
    client: TestClient, names: list[str], expected: tuple[int, int]
) -> None:
    """≤4 人范围 0-100，>4 人范围 0-1000。"""
    with lobby(client, names) as sockets:
        act(sockets[0], "start_game")
        state = next_state(sockets[0])
        assert (state["game"]["left"], state["game"]["right"]) == expected
        assert state["game"]["playerCount"] == len(names)


def test_join_order_defines_turn_order(client: TestClient) -> None:
    """玩家列表顺序即加入先后顺序，也即游戏顺序。"""
    with lobby(client, ["甲", "乙", "丙", "丁", "戊"]) as sockets:
        act(sockets[0], "start_game")
        state = next_state(sockets[0])
        names = [p["nickname"] for p in state["room"]["players"]]
        assert names == ["甲", "乙", "丙", "丁", "戊"]
        assert state["room"]["hostId"] == state["room"]["players"][0]["id"]
        assert state["game"]["currentPlayerNickname"] == "甲"


# --- 需求 3：猜数字与边界收缩 -----------------------------------------------


def test_guess_updates_bounds_and_turn(client: TestClient) -> None:
    """合法猜测后边界收缩、回合推进，全员状态同步。"""
    with lobby(client, ["甲", "乙", "丙"]) as (a, b, c):
        act(a, "start_game")
        for ws in (a, b, c):
            next_state(ws)

        act(a, "guess", value=10)  # 10 < 42 → left = 11
        state_a = next_state(a)
        state_b = next_state(b)
        state_c = next_state(c)

        assert (state_a["game"]["left"], state_a["game"]["right"]) == (11, 100)
        assert state_a["game"]["currentPlayerNickname"] == "乙"
        assert state_a["game"]["isYourTurn"] is False
        assert state_b["game"]["isYourTurn"] is True
        assert state_c["game"]["isYourTurn"] is False
        assert state_a["game"]["lastGuess"]["nickname"] == "甲"
        assert state_a["game"]["lastGuess"]["value"] == 10
        assert state_a["game"]["lastGuess"]["result"] == "safe"
        assert state_a["game"]["turnCount"] == 1
        assert state_a["game"]["bomb"] is None  # 游戏过程中不泄露炸弹


def test_boundary_values_are_accepted_over_ws(client: TestClient) -> None:
    """闭区间语义：left 与 right 本身都是合法输入。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(a, "guess", value=0)
        assert next_state(a)["game"]["left"] == 1

        act(b, "guess", value=100)
        assert next_state(b)["game"]["right"] == 99


def test_out_of_range_does_not_advance_turn(client: TestClient) -> None:
    """越界输入被拒，回合不推进，边界不变；纠正后可继续。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(a, "guess", value=999)
        error = next_error(a)
        assert error["code"] == "OUT_OF_RANGE"
        assert "0 - 100" in error["message"]

        # 服务端回合仍在甲手上，边界与计数均未变化
        assert game_room.current_player().nickname == "甲"
        assert game_room.turn_count == 0
        assert (game_room.left, game_room.right) == (0, 100)

        # 甲纠正后可以正常提交
        act(a, "guess", value=10)
        state = next_state(a)
        assert state["game"]["left"] == 11
        assert state["game"]["turnCount"] == 1


def test_error_message_tracks_shrunk_bounds(client: TestClient) -> None:
    """越界提示中的边界随收缩动态更新，与前端显示的 x - y 一致。"""
    with lobby(client, ["甲"]) as (a,):
        act(a, "start_game")
        next_state(a)

        act(a, "guess", value=90)  # right → 89
        next_state(a)

        act(a, "guess", value=95)
        error = next_error(a)
        assert error["code"] == "OUT_OF_RANGE"
        assert "0 - 89" in error["message"]


def test_not_your_turn_rejected(client: TestClient) -> None:
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(b, "guess", value=10)  # 还没轮到乙
        error = next_error(b)
        assert error["code"] == "NOT_YOUR_TURN"
        assert game_room.turn_count == 0
        assert game_room.current_player().nickname == "甲"


def test_non_integer_rejected_over_ws(client: TestClient) -> None:
    with lobby(client, ["甲"]) as (a,):
        act(a, "start_game")
        next_state(a)

        act(a, "guess", value="abc")
        error = next_error(a)
        assert error["code"] == "INVALID_NUMBER"
        assert game_room.turn_count == 0


def test_string_integer_accepted_over_ws(client: TestClient) -> None:
    """前端传字符串数字也能被正确解析（容错）。"""
    with lobby(client, ["甲"]) as (a,):
        act(a, "start_game")
        next_state(a)

        act(a, "guess", value="10")
        state = next_state(a)
        assert state["game"]["left"] == 11
        assert state["game"]["turnCount"] == 1


def test_guess_before_start_rejected(client: TestClient) -> None:
    with lobby(client, ["甲"]) as (a,):
        act(a, "guess", value=10)  # 还在大厅，未开始
        error = next_error(a)
        assert error["code"] == "INVALID_PHASE"


# --- 需求 3：Boom 与回大厅 --------------------------------------------------


def test_boom_broadcasts_and_returns_to_lobby(client: TestClient, fast_boom: None) -> None:
    """命中炸弹 → 全员收到 Boom 提示 → 倒计时结束后回到大厅且玩家保留。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(a, "guess", value=BOMB)  # 甲踩雷

        boom_a = next_boom(a)
        boom_b = next_boom(b)
        assert boom_a["nickname"] == "甲"
        assert boom_a["bomb"] == BOMB
        assert boom_a["backToLobbyInMs"] == config.BOOM_DURATION_MS
        assert boom_b["nickname"] == "甲"

        # Boom 阶段的状态里才公布炸弹
        state_a = next_state(a)
        state_b = next_state(b)
        assert state_a["phase"] == "boom"
        assert state_b["phase"] == "boom"
        assert state_a["game"]["bomb"] == BOMB
        assert state_a["game"]["boomNickname"] == "甲"
        assert state_a["game"]["isYourTurn"] is False

        # 倒计时结束后全员回到大厅，玩家列表与顺序保留
        lobby_a = next_state(a)
        lobby_b = next_state(b)
        assert lobby_a["phase"] == "lobby"
        assert lobby_b["phase"] == "lobby"
        assert [p["nickname"] for p in lobby_a["room"]["players"]] == ["甲", "乙"]
        assert lobby_a["game"] is None
        assert game_room.bomb is None
        assert game_room.turn_count == 0


def test_boom_reveals_nickname_of_actual_loser(client: TestClient) -> None:
    """需求：命中炸弹时提示踩雷者的昵称，而不是别人。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(a, "guess", value=10)  # 甲安全，轮到乙
        next_state(a)
        next_state(b)

        act(b, "guess", value=BOMB)  # 乙踩雷
        boom = next_boom(a)
        assert boom["nickname"] == "乙"


def test_next_round_restarts_from_first_player(client: TestClient, fast_boom: None) -> None:
    """Boom 回大厅后再次开始，仍从第一位玩家起，并重新生成炸弹与范围。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)
        act(a, "guess", value=BOMB)
        next_boom(a)
        next_boom(b)
        next_state(a)  # boom state
        next_state(b)
        lobby_a = next_state(a)  # 回到大厅
        next_state(b)
        assert lobby_a["phase"] == "lobby"

        act(b, "start_game")
        state_a = next_state(a)
        state_b = next_state(b)
        assert state_a["phase"] == "playing"
        assert state_a["game"]["currentPlayerNickname"] == "甲"
        assert (state_a["game"]["left"], state_a["game"]["right"]) == (0, 100)
        assert state_a["game"]["turnCount"] == 0
        assert state_b["game"]["isYourTurn"] is False


def test_close_lobby_during_boom_cancels_reset(client: TestClient, fast_boom: None) -> None:
    """Boom 倒计时期间关闭大厅，定时任务不得把状态又改回大厅。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(a, "guess", value=BOMB)
        next_boom(a)
        next_boom(b)
        next_state(a)
        next_state(b)

        act(b, "close_lobby")
        assert next_state(a)["phase"] == "idle"
        assert next_state(b)["phase"] == "idle"

        # 等过倒计时时长，确认定时任务没有覆盖状态
        time.sleep(0.5)
        assert game_room.phase is Phase.IDLE
        assert game_room.players == []


def test_guess_after_boom_rejected(client: TestClient, fast_boom: None) -> None:
    """Boom 展示期间不接受新的猜测。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)
        act(a, "guess", value=BOMB)
        next_boom(a)
        next_boom(b)
        next_state(a)
        next_state(b)

        act(b, "guess", value=10)
        error = next_error(b)
        assert error["code"] == "INVALID_PHASE"


def test_duplicate_nickname_shows_ordinal(client: TestClient) -> None:
    """重名玩家在下发列表中显示为「昵称(1)」「昵称(2)」以便区分。"""
    with lobby(client, ["小明", "小明", "小红"]) as sockets:
        act(sockets[0], "start_game")
        state = next_state(sockets[0])
        names = [p["nickname"] for p in state["room"]["players"]]
        assert names == ["小明(1)", "小明(2)", "小红"]
        assert state["game"]["currentPlayerNickname"] == "小明(1)"


# --- 断线与轮次 -------------------------------------------------------------


def test_disconnect_of_current_player_skips_turn(client: TestClient) -> None:
    """当前回合玩家断线 → 自动轮到下一位，其他人收到新状态。"""
    with lobby(client, ["甲", "乙", "丙"]) as (a, b, c):
        act(a, "start_game")
        for ws in (a, b, c):
            next_state(ws)

        # 甲是当前回合玩家，主动断开（Conn.close 幂等，收尾时不会重复关闭）
        a.close()

        state_b = next_state(b)
        state_c = next_state(c)
        assert len(state_b["room"]["players"]) == 2
        assert state_b["game"]["currentPlayerNickname"] == "乙"
        assert state_b["game"]["isYourTurn"] is True
        assert state_c["game"]["isYourTurn"] is False
        assert all(p["nickname"] != "甲" for p in state_b["room"]["players"])


def test_disconnect_before_current_keeps_turn(client: TestClient) -> None:
    """断线者在当前玩家之前 → 索引左移，仍指向同一位玩家。"""
    with lobby(client, ["甲", "乙", "丙"]) as (a, b, c):
        act(a, "start_game")
        for ws in (a, b, c):
            next_state(ws)

        act(a, "guess", value=10)  # 轮到乙
        for ws in (a, b, c):
            next_state(ws)
        act(b, "guess", value=90)  # 轮到丙
        for ws in (a, b, c):
            next_state(ws)
        assert game_room.current_player().nickname == "丙"

        a.close()  # 移除丙之前的玩家

        state_b = next_state(b)
        next_state(c)
        assert state_b["game"]["currentPlayerNickname"] == "丙"
        assert len(state_b["room"]["players"]) == 2


def test_disconnect_after_current_keeps_turn(client: TestClient) -> None:
    """断线者在当前玩家之后 → 索引不变。"""
    with lobby(client, ["甲", "乙", "丙"]) as (a, b, c):
        act(a, "start_game")
        for ws in (a, b, c):
            next_state(ws)

        c.close()  # 丙在当前玩家甲之后

        state_a = next_state(a)
        next_state(b)
        assert state_a["game"]["currentPlayerNickname"] == "甲"
        assert len(state_a["room"]["players"]) == 2


def test_last_player_disconnect_wraps_index(client: TestClient) -> None:
    """末位玩家断线时索引取模，不越界。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)

        act(a, "guess", value=10)  # 轮到乙（索引 1）
        next_state(a)
        next_state(b)

        b.close()  # 只剩甲

        state_a = next_state(a)
        assert len(state_a["room"]["players"]) == 1
        assert state_a["game"]["currentPlayerNickname"] == "甲"
        assert state_a["game"]["isYourTurn"] is True
        assert game_room.current_index == 0


def test_all_disconnect_resets_room(client: TestClient) -> None:
    """全员断线 → 房间回到主界面状态。"""
    with one_connection(client) as ws:
        act(ws, "join_lobby", nickname="甲")
        next_state(ws)
        assert game_room.phase is Phase.LOBBY

    # 连接关闭后服务端应完成清理
    for _ in range(100):
        if game_room.phase is Phase.IDLE:
            break
        time.sleep(0.02)
    assert game_room.phase is Phase.IDLE
    assert game_room.players == []
    assert manager.connection_count == 0


def test_all_disconnect_during_playing_resets_room(client: TestClient) -> None:
    """游戏进行中全员断线，房间同样归零。"""
    with lobby(client, ["甲", "乙"]) as (a, b):
        act(a, "start_game")
        next_state(a)
        next_state(b)
        assert game_room.phase is Phase.PLAYING

    for _ in range(100):
        if game_room.phase is Phase.IDLE:
            break
        time.sleep(0.02)
    assert game_room.phase is Phase.IDLE
    assert game_room.players == []


def test_reconnect_after_close_can_rejoin(client: TestClient) -> None:
    """关闭大厅后连接仍存活，玩家可重新输入昵称加入。"""
    with one_connection(client) as ws:
        act(ws, "join_lobby", nickname="甲")
        next_state(ws)
        act(ws, "close_lobby")
        next_state(ws)
        assert game_room.phase is Phase.IDLE

        act(ws, "join_lobby", nickname="甲")
        state = next_state(ws)
        assert state["phase"] == "lobby"
        assert len(state["room"]["players"]) == 1
        assert state["you"]["inRoom"] is True


def test_disconnect_of_never_joined_connection_is_silent(client: TestClient) -> None:
    """连接存在但从未加入房间，断开时不影响房间状态、也不产生多余广播。"""
    with lobby(client, ["甲"]) as (a,):
        with one_connection(client) as ghost:
            pass  # 只连接不加入，随即断开

        assert game_room.phase is Phase.LOBBY
        assert len(game_room.players) == 1

        # 让 a 触发一次确定性响应：当前在大厅阶段提交猜测会得到 error。
        # 若 ghost 的断开错误地引发了广播，a 队列里的第一条消息就会是 state。
        act(a, "guess", value=10)
        first = a.receive_json()
        assert first["type"] == "error", f"ghost 断开不应产生广播，却收到 {first['type']}"
        assert first["code"] == "INVALID_PHASE"


# --- 完整对局 ---------------------------------------------------------------


def test_full_game_reaches_boom(client: TestClient, fast_boom: None) -> None:
    """端到端跑完一整局：加入 → 开始 → 多轮收缩 → 踩雷 → 回大厅。"""
    with lobby(client, ["甲", "乙", "丙"]) as sockets:
        act(sockets[0], "start_game")
        for ws in sockets:
            next_state(ws)

        # 炸弹固定为 42，按二分策略轮流逼近
        scripted = [
            (0, 50),    # > 42 → right = 49
            (1, 20),    # < 42 → left = 21
            (2, 40),    # < 42 → left = 41
            (0, 45),    # > 42 → right = 44
            (1, 41),    # < 42 → left = 42
            (2, 43),    # > 42 → right = 42
            (0, BOMB),  # 命中
        ]
        for index, value in scripted[:-1]:
            act(sockets[index], "guess", value=value)
            for ws in sockets:
                state = next_state(ws)
                assert state["phase"] == "playing"
            # 核心不变量：炸弹始终落在 [left, right] 内
            assert game_room.left <= BOMB <= game_room.right
            assert game_room.turn_count >= 1

        loser_index, value = scripted[-1]
        act(sockets[loser_index], "guess", value=value)
        for ws in sockets:
            next_boom(ws)
        assert game_room.phase is Phase.BOOM

        # 倒计时结束后全员回到大厅，三位玩家都还在
        for ws in sockets:
            next_state(ws)  # boom state
        for ws in sockets:
            state = next_state(ws)
            assert state["phase"] == "lobby"
            assert len(state["room"]["players"]) == 3


def test_full_game_large_room_reaches_boom(client: TestClient, fast_boom: None) -> None:
    """5 人局（范围 0-1000）同样能正常收敛到 Boom。"""
    with lobby(client, ["甲", "乙", "丙", "丁", "戊"]) as sockets:
        act(sockets[0], "start_game")
        for ws in sockets:
            state = next_state(ws)
            assert state["game"]["right"] == 1000

        scripted = [
            (0, 500),   # > 42 → right = 499
            (1, 100),   # > 42 → right = 99
            (2, 20),    # < 42 → left = 21
            (3, 60),    # > 42 → right = 59
            (4, 30),    # < 42 → left = 31
            (0, 50),    # > 42 → right = 49
            (1, 40),    # < 42 → left = 41
            (2, 45),    # > 42 → right = 44
            (3, 43),    # > 42 → right = 42
            (4, BOMB),  # 命中
        ]
        for index, value in scripted[:-1]:
            act(sockets[index], "guess", value=value)
            for ws in sockets:
                next_state(ws)
            assert game_room.left <= BOMB <= game_room.right

        loser_index, value = scripted[-1]
        act(sockets[loser_index], "guess", value=value)
        for ws in sockets:
            boom = next_boom(ws)
            assert boom["nickname"] == "戊"
        assert game_room.phase is Phase.BOOM
