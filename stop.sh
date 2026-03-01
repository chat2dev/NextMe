#!/usr/bin/env bash
# NextMe 停止脚本
#
# 用法：./stop.sh [--timeout 15]
#
# 所有额外参数会透传给 nextme down。

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$HOME/.nextme/nextme.pid"

echo -e "${BLUE}╔══════════════════════════════╗${NC}"
echo -e "${BLUE}║       NextMe  停止中         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════╝${NC}"
echo ""

# 检查 uv
if ! command -v uv &>/dev/null; then
    echo -e "${RED}✗  未找到 uv${NC}"
    exit 1
fi

# 如果 PID 文件存在，直接显示正在运行的 PID
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo -e "${YELLOW}  正在停止进程 PID $PID ...${NC}"
    else
        echo -e "${YELLOW}  PID 文件存在但进程已结束 (PID $PID)${NC}"
    fi
else
    echo -e "${YELLOW}  未找到 PID 文件，可能 NextMe 未运行${NC}"
fi

cd "$PROJECT_DIR"
if uv run nextme down "$@"; then
    echo ""
    echo -e "${GREEN}✓  NextMe 已停止${NC}"
else
    echo ""
    echo -e "${YELLOW}⚠  nextme down 返回非零，请检查进程是否仍在运行：${NC}"
    echo -e "${YELLOW}   ps aux | grep nextme${NC}"
    exit 1
fi
