"""WebSocket 通信层：连接管理、消息路由与广播。

职责边界
--------
本模块只负责「收发与广播」，所有规则计算都委托给 :mod:`app.game`。两者严格分离，
规则可以用 pytest 直接覆盖而不必起服务。

并发约定
--------
``game_room`` 的所有方法都是同步的，调用前后不得插入 ``await``，以保证一次状态
转移在事件循环中原子完成。广播本身是异步的，必须在状态转移**之后**进行。

部署约束
--------
游戏状态存于本进程的 ``game_room`` 单例中，因此**只能单 worker 运行**。多 worker
会把玩家分散到不同进程、互相不可见。横向扩展需引入 Redis 共享状态 + 发布订阅。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from . import config
from .game import GameError, GameRoom
from .models import (
    BoomMessage,
    ClientAction,
    ClientMessage,
    ErrorCode,
    ErrorMessage,
    GuessResult,
    Phase,
    PongMessage,
    WelcomeMessage,
    to_json,
)

logger = logging.getLogger("bomb.ws")

router = APIRouter()

#: 全局单房间状态机
game_room = GameRoom()


class ConnectionManager:
    """维护活跃 WebSocket 连接，提供单播与广播能力。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        #: 自增的 playerId 生成器，进程内唯一
        self._id_counter = itertools.count(1)
        #: WebSocket -> playerId 映射
        self._ids: dict[WebSocket, str] = {}

    def next_player_id(self) -> str:
        """生成新的 playerId。"""
        return f"p_{next(self._id_counter):04d}"

    async def connect(self, websocket: WebSocket) -> str:
        """接受连接并分配 playerId。"""
        await websocket.accept()
        player_id = self.next_player_id()
        self._connections.add(websocket)
        self._ids[websocket] = player_id
        return player_id

    def disconnect(self, websocket: WebSocket) -> str | None:
        """移除连接，返回其 playerId（若曾分配）。"""
        self._connections.discard(websocket)
        return self._ids.pop(websocket, None)

    def player_id_of(self, websocket: WebSocket) -> str | None:
        """查询连接对应的 playerId。"""
        return self._ids.get(websocket)

    @property
    def connection_count(self) -> int:
        """当前活跃连接数。"""
        return len(self._connections)

    async def send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        """向单个连接发送 JSON，失败时静默丢弃（连接可能已关闭）。"""
        try:
            await websocket.send_json(payload)
        except Exception:  # noqa: BLE001 - 单连接发送失败不应中断整体流程
            logger.debug("send to single connection failed", exc_info=True)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """向所有活跃连接发送同一份 JSON。"""
        if not self._connections:
            return
        # 复制一份，避免发送过程中集合被修改
        for websocket in list(self._connections):
            await self.send_json(websocket, payload)

    async def broadcast_state(self) -> None:
        """为每个连接生成个性化状态快照并广播。

        ``is_you`` / ``isYourTurn`` 等字段按接收方计算，因此不能复用同一份 payload。
        """
        if not self._connections:
            return
        now = int(time.time() * 1000)
        for websocket in list(self._connections):
            player_id = self._ids.get(websocket)
            if player_id is None:
                continue
            message = game_room.snapshot(player_id, server_time=now)
            await self.send_json(websocket, to_json(message))


manager = ConnectionManager()

#: Boom 后回到大厅的定时任务，避免重复创建
_boom_task: asyncio.Task[None] | None = None


def cancel_boom_task() -> None:
    """取消尚未触发的 Boom 回大厅定时任务。

    用于 Boom 展示期间被 ``close_lobby`` 打断等场景，避免旧任务在房间已重置后
    又把状态覆盖回大厅。
    """
    global _boom_task
    if _boom_task is not None and not _boom_task.done():
        _boom_task.cancel()
    _boom_task = None


async def send_error(websocket: WebSocket, code: ErrorCode, message: str) -> None:
    """向操作者定向下发错误消息。"""
    await manager.send_json(websocket, to_json(ErrorMessage(code=code, message=message)))


async def schedule_boom_reset(nickname: str, bomb: int) -> None:
    """安排 Boom 展示结束后回到大厅。

    先立即下发 BoomMessage（前端据此展示「昵称，Boom！」并倒计时），再创建一个
    延时任务，到时调用 ``finish_boom`` 并广播，使全员统一回到大厅界面。
    """
    global _boom_task

    # 先取消上一个尚未触发的定时任务，避免它在广播之后才醒来覆盖状态
    cancel_boom_task()

    await manager.broadcast(
        to_json(
            BoomMessage(
                nickname=nickname,
                bomb=bomb,
                back_to_lobby_in_ms=config.BOOM_DURATION_MS,
            )
        )
    )
    await manager.broadcast_state()

    async def _reset_after_delay() -> None:
        await asyncio.sleep(config.BOOM_DURATION_MS / 1000)
        # 仅当仍处于 BOOM 时才推进；期间被关闭大厅则不再覆盖状态
        if game_room.phase is Phase.BOOM:
            game_room.finish_boom()
            await manager.broadcast_state()

    _boom_task = asyncio.get_running_loop().create_task(_reset_after_delay())


async def handle_action(websocket: WebSocket, player_id: str, data: dict[str, Any]) -> None:
    """解析并执行一条客户端消息。

    状态转移（同步、原子）先完成，随后再执行异步广播。
    """
    try:
        message = ClientMessage(**data)
    except ValidationError as exc:
        logger.warning("invalid client message: %s", exc)
        await send_error(websocket, ErrorCode.MALFORMED_MESSAGE, "消息格式不正确")
        return

    action = message.known_action()
    if action is None:
        await send_error(websocket, ErrorCode.INVALID_ACTION, f"未知操作：{message.action}")
        return

    try:
        if action is ClientAction.PING:
            await manager.send_json(
                websocket,
                to_json(PongMessage(server_time=int(time.time() * 1000))),
            )
            return

        if action is ClientAction.JOIN_LOBBY:
            game_room.join_lobby(player_id, message.nickname)
            await manager.broadcast_state()
            return

        if action is ClientAction.START_GAME:
            game_room.start_game(player_id)
            await manager.broadcast_state()
            return

        if action is ClientAction.CLOSE_LOBBY:
            # 关闭大厅可能打断正在进行的 Boom 倒计时，先取消定时任务再重置状态
            cancel_boom_task()
            game_room.close_lobby(player_id)
            await manager.broadcast_state()
            return

        if action is ClientAction.GUESS:
            outcome = game_room.guess(player_id, message.value)
            if outcome.result is GuessResult.BOOM:
                assert outcome.boom_nickname is not None
                assert outcome.bomb is not None
                await schedule_boom_reset(outcome.boom_nickname, outcome.bomb)
            else:
                await manager.broadcast_state()
            return

    except GameError as err:
        await send_error(websocket, err.code, err.message)
        # 出错时同步一次状态，保证操作者界面与服务端一致
        await manager.send_json(websocket, to_json(game_room.snapshot(player_id)))
    except Exception:  # noqa: BLE001 - 兜底防止单个请求异常导致连接崩溃
        logger.exception("unexpected error while handling action")
        await send_error(websocket, ErrorCode.INVALID_ACTION, "服务端处理异常，请重试")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket 主端点：建立会话、循环收发消息、断线清理。"""
    player_id = await manager.connect(websocket)
    logger.info("connected %s (total=%d)", player_id, manager.connection_count)

    try:
        await manager.send_json(
            websocket,
            to_json(
                WelcomeMessage(
                    player_id=player_id,
                    server_time=int(time.time() * 1000),
                )
            ),
        )
        # 下发当前状态，让刷新页面的客户端立即回到正确界面
        await manager.send_json(websocket, to_json(game_room.snapshot(player_id)))

        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await send_error(websocket, ErrorCode.MALFORMED_MESSAGE, "消息不是合法的 JSON")
                continue
            if not isinstance(data, dict):
                await send_error(websocket, ErrorCode.MALFORMED_MESSAGE, "消息必须是 JSON 对象")
                continue
            await handle_action(websocket, player_id, data)

    except WebSocketDisconnect:
        logger.info("disconnected %s", player_id)
    except Exception:  # noqa: BLE001 - 连接级异常统一走清理流程
        logger.exception("websocket error for %s", player_id)
    finally:
        manager.disconnect(websocket)
        # 从房间移除并重算轮次；若影响对局则广播新状态
        affected = game_room.on_disconnect(player_id)
        if affected:
            await manager.broadcast_state()
