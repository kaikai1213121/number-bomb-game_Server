"""FastAPI 应用入口：路由挂载、静态资源与生命周期钩子。

⚠️ 部署约束
-----------
游戏状态存于进程内存（``app.ws.game_room`` 单例），**只能单 worker 运行**：

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

多 worker 会把玩家分散到不同进程，导致彼此不可见。若需横向扩展，必须先引入
Redis 做共享状态与发布订阅，属于二期改造范围。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .ws import game_room, manager, router as ws_router

logger = logging.getLogger("bomb")

#: 静态资源目录（前端单页），位于项目根目录的 static/
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动与关闭时的日志与清理。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("数字炸弹后端启动")
    logger.info("小范围(人数<=%d): %d-%d", config.SMALL_ROOM_MAX_PLAYERS, *config.SMALL_ROOM_RANGE)
    logger.info("大范围(人数>%d): %d-%d", config.SMALL_ROOM_MAX_PLAYERS, *config.LARGE_ROOM_RANGE)
    logger.info("Boom 停留时长: %d ms", config.BOOM_DURATION_MS)
    try:
        yield
    finally:
        game_room.reset_to_idle()
        logger.info("数字炸弹后端已关闭，房间状态已重置")


app = FastAPI(
    title="数字炸弹小游戏",
    description="多人在线数字炸弹游戏的 WebSocket 后端",
    version="1.0.0",
    lifespan=lifespan,
)

# 允许跨域，便于前端与后端分离部署时的联调
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router)


@app.get("/api/health", tags=["运维"])
async def health() -> dict[str, object]:
    """存活检查，返回服务状态、当前阶段与房间内人数。"""
    return {
        "status": "ok",
        "phase": game_room.phase.value,
        "playerCount": len(game_room.players),
        "connections": manager.connection_count,
    }


@app.get("/api/state", tags=["运维"])
async def state() -> dict[str, object]:
    """只读状态快照，用于调试。

    注意：**不会**返回炸弹数字，避免调试接口泄露答案。
    """
    snapshot = game_room.snapshot(viewer_id="__debug__")
    data = snapshot.model_dump(by_alias=True, mode="json", exclude_none=False)
    if isinstance(data.get("game"), dict):
        data["game"]["bomb"] = None
    return data


@app.get("/api/config", tags=["运维"])
async def runtime_config() -> dict[str, object]:
    """返回当前生效的规则参数，便于部署后核对配置。"""
    return {
        "smallRoomMaxPlayers": config.SMALL_ROOM_MAX_PLAYERS,
        "smallRoomRange": list(config.SMALL_ROOM_RANGE),
        "largeRoomRange": list(config.LARGE_ROOM_RANGE),
        "boomDurationMs": config.BOOM_DURATION_MS,
        "nicknameMaxLen": config.NICKNAME_MAX_LEN,
        "allowDuplicateNick": config.ALLOW_DUPLICATE_NICK,
        "maxPlayers": config.MAX_PLAYERS,
    }


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """前端单页入口。"""
    return FileResponse(STATIC_DIR / "index.html")


# 静态资源挂载放在最后，避免覆盖上面显式声明的路由
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:  # pragma: no cover - 仅在 static 目录缺失时触发
    logger.warning("静态目录不存在：%s，前端页面将不可用", STATIC_DIR)
