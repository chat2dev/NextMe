#!/usr/bin/env bash
# NextMe systemd user-service 安装脚本（Linux）
#
# 用途：将 NextMe 注册为 systemd 用户服务，使其在用户登录后自动启动，
#       并在崩溃后自动重启。启用 loginctl linger 后无需保持登录状态。
#
# 用法：
#   ./systemd-install.sh            # 安装并立即启动
#   ./systemd-install.sh --uninstall  # 停止并卸载
#
# 要求：Linux（systemd），已安装 uv，已完成 nextme 配置（~/.nextme/settings.json）

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

SERVICE_NAME="nextme"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/${SERVICE_NAME}.service"
LOG_DIR="$HOME/.nextme/logs"
LOG_FILE="$LOG_DIR/nextme.log"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.nextme/settings.json"

# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
    echo -e "${BLUE}╔══════════════════════════════╗${NC}"
    echo -e "${BLUE}║    NextMe systemd 服务卸载   ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════╝${NC}"
    echo ""

    if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl --user stop "$SERVICE_NAME"
        echo -e "${GREEN}✓  服务已停止${NC}"
    fi

    if systemctl --user is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl --user disable "$SERVICE_NAME"
        echo -e "${GREEN}✓  服务已禁用${NC}"
    fi

    if [ -f "$UNIT_FILE" ]; then
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload
        echo -e "${GREEN}✓  已删除 unit 文件：$UNIT_FILE${NC}"
    else
        echo -e "${YELLOW}⚠  未找到 unit 文件，跳过删除${NC}"
    fi

    echo ""
    echo -e "${GREEN}✓  卸载完成。如需重新安装，再次运行 ./systemd-install.sh${NC}"
    exit 0
fi

# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
echo -e "${BLUE}╔══════════════════════════════════╗${NC}"
echo -e "${BLUE}║   NextMe systemd 服务安装        ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════╝${NC}"
echo ""

# 检查 systemd user 支持
if ! systemctl --user status &>/dev/null 2>&1 && ! systemctl --user list-units &>/dev/null 2>&1; then
    echo -e "${YELLOW}⚠  无法连接 systemd 用户总线，尝试继续...${NC}"
fi

# 检查 uv
if ! command -v uv &>/dev/null; then
    echo -e "${RED}✗  未找到 uv，请先安装：${NC}"
    echo -e "${RED}   curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
    exit 1
fi
UV_PATH="$(command -v uv)"
echo -e "${GREEN}✓  uv：$UV_PATH${NC}"

# 检查 nextme 可用性
if ! uv run --project "$PROJECT_DIR" nextme --help &>/dev/null 2>&1; then
    echo -e "${RED}✗  未找到 nextme 命令，请先在项目目录执行：${NC}"
    echo -e "${RED}   cd $PROJECT_DIR && uv sync${NC}"
    exit 1
fi
echo -e "${GREEN}✓  nextme CLI 可用${NC}"

# 检查配置文件
if [ ! -f "$SETTINGS" ]; then
    echo -e "${RED}✗  未找到 ~/.nextme/settings.json${NC}"
    if [ -f "$PROJECT_DIR/settings.json.example" ]; then
        echo -e "${RED}   请先执行：mkdir -p ~/.nextme && cp $PROJECT_DIR/settings.json.example $SETTINGS${NC}"
    fi
    exit 1
fi
echo -e "${GREEN}✓  配置文件：$SETTINGS${NC}"

# 创建日志目录
mkdir -p "$LOG_DIR"
echo -e "${GREEN}✓  日志目录：$LOG_DIR${NC}"

# 停止旧服务（如已运行）
if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo -e "${YELLOW}⚠  发现已有服务，正在重启...${NC}"
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
fi

# 创建 unit 目录
mkdir -p "$UNIT_DIR"

# 写入 unit 文件
cat > "$UNIT_FILE" << EOF
[Unit]
Description=NextMe Feishu Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${UV_PATH} run --project ${PROJECT_DIR} nextme up
WorkingDirectory=${PROJECT_DIR}
Restart=on-failure
RestartSec=10
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}
Environment=HOME=${HOME}
Environment=PATH=${PATH}

[Install]
WantedBy=default.target
EOF
echo -e "${GREEN}✓  已写入 unit 文件：$UNIT_FILE${NC}"

# 重载 daemon 并启动
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user start "$SERVICE_NAME"
echo -e "${GREEN}✓  服务已启用并启动${NC}"

# 启用 linger（用户注销后服务继续运行）
if command -v loginctl &>/dev/null; then
    if loginctl enable-linger "$USER" 2>/dev/null; then
        echo -e "${GREEN}✓  已启用 loginctl linger（注销后服务持续运行）${NC}"
    else
        echo -e "${YELLOW}⚠  loginctl enable-linger 需要 sudo，服务仅在登录时运行${NC}"
        echo -e "${YELLOW}   如需后台持续运行，请执行：sudo loginctl enable-linger $USER${NC}"
    fi
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  安装完成！NextMe 现已作为后台服务运行。${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  日志：  tail -f ${LOG_FILE}"
echo -e "  状态：  systemctl --user status ${SERVICE_NAME}"
echo -e "  停止：  systemctl --user stop ${SERVICE_NAME}"
echo -e "  启动：  systemctl --user start ${SERVICE_NAME}"
echo -e "  卸载：  ./systemd-install.sh --uninstall"
echo ""
