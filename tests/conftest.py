"""pytest 共享配置。

把项目根目录加入 sys.path，使测试可以用 ``from app import ...`` 导入应用包，
无论从哪个目录启动 pytest 都能正常收集。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.game import GameRoom  # noqa: E402


@pytest.fixture
def room() -> GameRoom:
    """提供一个炸弹固定在 42 的房间，便于测试确定性地命中或避开。"""
    return GameRoom(rng=lambda lo, hi: 42)


@pytest.fixture
def real_random_room() -> GameRoom:
    """提供使用真实随机源的房间，用于验证不变量。"""
    return GameRoom()


@pytest.fixture(autouse=True)
def _restore_config():
    """每个用例结束后恢复配置，避免用例间相互污染。"""
    snapshot = (
        config.SMALL_ROOM_MAX_PLAYERS,
        config.SMALL_ROOM_RANGE,
        config.LARGE_ROOM_RANGE,
        config.MAX_PLAYERS,
        config.ALLOW_DUPLICATE_NICK,
        config.NICKNAME_MAX_LEN,
    )
    yield
    (
        config.SMALL_ROOM_MAX_PLAYERS,
        config.SMALL_ROOM_RANGE,
        config.LARGE_ROOM_RANGE,
        config.MAX_PLAYERS,
        config.ALLOW_DUPLICATE_NICK,
        config.NICKNAME_MAX_LEN,
    ) = snapshot
