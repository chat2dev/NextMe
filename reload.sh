#!/usr/bin/env bash
# NextMe 热重载脚本
#
# 用法：./reload.sh
#
# 向运行中的 NextMe 进程发送 SIGHUP，触发 settings.json 中以下字段的热重载：
#   log_level, progress_debounce_seconds, memory_debounce_seconds,
#   memory_max_facts, permission_auto_approve, streaming_enabled, admin_users
#
# 以下字段需要重启才能生效（涉及 Feishu 连接或项目配置）：
#   app_id, app_secret, projects, bindings

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

PID_FILE="$HOME/.nextme/nextme.pid"

echo -e "${BLUE}╔══════════════════════════════╗${NC}"
echo -e "${BLUE}║       NextMe  热重载         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════╝${NC}"
echo ""

if [ ! -f "$PID_FILE" ]; then
    echo -e "${RED}✗  未找到 PID 文件，NextMe 可能未运行${NC}"
    exit 1
fi

PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [ -z "$PID" ]; then
    echo -e "${RED}✗  PID 文件为空${NC}"
    exit 1
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo -e "${RED}✗  进程 $PID 未运行（PID 文件可能已过期）${NC}"
    exit 1
fi

echo -e "${YELLOW}  正在向进程 PID $PID 发送 SIGHUP...${NC}"
if kill -HUP "$PID" 2>/dev/null; then
    echo ""
    echo -e "${GREEN}✓  SIGHUP 已发送，NextMe 正在重载以下配置：${NC}"
    echo -e "${GREEN}   log_level, progress_debounce_seconds, memory_debounce_seconds${NC}"
    echo -e "${GREEN}   memory_max_facts, permission_auto_approve, streaming_enabled, admin_users${NC}"
    echo ""
    echo -e "${YELLOW}  注意：以下字段需要重启才能生效：${NC}"
    echo -e "${YELLOW}   app_id, app_secret, projects, bindings${NC}"
else
    echo ""
    echo -e "${RED}✗  发送 SIGHUP 失败${NC}"
    exit 1
fi
