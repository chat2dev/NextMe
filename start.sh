#!/usr/bin/env bash
# NextMe 启动脚本
#
# 用法：./start.sh [--log-level DEBUG]
#
# 所有额外参数会透传给 nextme up。

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$HOME/.nextme/nextme.pid"

echo -e "${BLUE}╔══════════════════════════════╗${NC}"
echo -e "${BLUE}║       NextMe  启动中         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════╝${NC}"
echo ""

# 检查是否已有实例运行
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo -e "${YELLOW}⚠  NextMe 已在运行 (PID $OLD_PID)${NC}"
        echo -e "${YELLOW}   先执行 ./stop.sh 再重启，或直接运行 nextme up。${NC}"
        exit 1
    fi
fi

# 检查 uv
if ! command -v uv &>/dev/null; then
    echo -e "${RED}✗  未找到 uv，请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
    exit 1
fi

# 检查 nextme CLI（先 uv run，也接受全局安装）
if ! uv run nextme --help &>/dev/null 2>&1; then
    echo -e "${RED}✗  未找到 nextme 命令，请在项目目录执行 uv sync${NC}"
    exit 1
fi

# 配置文件提示
SETTINGS="$HOME/.nextme/settings.json"
if [ ! -f "$SETTINGS" ]; then
    echo -e "${YELLOW}⚠  未找到 ~/.nextme/settings.json${NC}"
    if [ -f "$PROJECT_DIR/settings.json.example" ]; then
        echo -e "${YELLOW}   请先执行：mkdir -p ~/.nextme && cp $PROJECT_DIR/settings.json.example $SETTINGS${NC}"
    fi
    exit 1
fi

echo -e "${GREEN}✓  配置文件：$SETTINGS${NC}"
echo -e "${GREEN}✓  项目目录：$PROJECT_DIR${NC}"
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  NextMe 启动中，使用 Ctrl+C 或 ./stop.sh 停止${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

cd "$PROJECT_DIR"
exec uv run nextme up "$@"
