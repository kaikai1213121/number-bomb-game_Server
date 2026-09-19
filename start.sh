#!/usr/bin/env bash
#
# 数字炸弹游戏一键启动脚本
#
# 用法：
#   ./start.sh           # 首次运行会自动创建虚拟环境并安装依赖
#   ./start.sh --update  # 强制重新安装依赖
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# --- 自动创建虚拟环境（仅首次） ---
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/3] 创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/3] 虚拟环境已存在，跳过创建"
fi

# --- 安装依赖（首次或带 --update 参数时） ---
if [ ! -f "$VENV_DIR/.installed" ] || { [ $# -gt 0 ] && [ "$1" = "--update" ]; }; then
    echo "[2/3] 安装依赖..."
    "$PIP" install --upgrade pip -q
    "$PIP" install -r requirements.txt -q
    touch "$VENV_DIR/.installed"
else
    echo "[2/3] 依赖已安装，跳过（如需更新请加 --update 参数）"
fi

# --- 启动服务 ---
echo "[3/3] 启动数字炸弹服务端..."
echo ""
exec "$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
