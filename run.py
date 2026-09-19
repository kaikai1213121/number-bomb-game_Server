"""开发环境启动入口。

用法::

    python run.py

生产环境请直接用 uvicorn 命令启动（务必保持单 worker）::

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("BOMB_HOST", "0.0.0.0")
    port = int(os.environ.get("BOMB_PORT", "8000"))
    # reload 便于开发时热重载；生产环境请用上面的 uvicorn 命令
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
