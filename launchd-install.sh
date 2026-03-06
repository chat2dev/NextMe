#!/usr/bin/env bash
# NextMe launchd LaunchAgent 安装脚本（macOS）
#
# 用途：将 NextMe 注册为 macOS launchd LaunchAgent，使其在用户登录后自动启动，
#       锁屏/息屏期间保持网络连接，并在崩溃后自动重启。
#
# 用法：
#   ./launchd-install.sh          # 安装并立即启动
#   ./launchd-install.sh --uninstall  # 停止并卸载
#
# 要求：macOS，已安装 uv，已完成 nextme 配置（~/.nextme/settings.json）

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

LABEL="com.nextme.bot"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/.nextme/logs"
LOG_FILE="$LOG_DIR/nextme.log"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.nextme/settings.json"

# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
    echo -e "${BLUE}╔══════════════════════════════╗${NC}"
    echo -e "${BLUE}║    NextMe LaunchAgent 卸载   ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════╝${NC}"
    echo ""

    if launchctl list | grep -q "$LABEL" 2>/dev/null; then
        launchctl stop "$LABEL" 2>/dev/null || true
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        echo -e "${GREEN}✓  LaunchAgent 已停止并卸载${NC}"
    else
        echo -e "${YELLOW}⚠  LaunchAgent 未加载，跳过卸载步骤${NC}"
    fi

    if [ -f "$PLIST_PATH" ]; then
        rm -f "$PLIST_PATH"
        echo -e "${GREEN}✓  已删除 plist：$PLIST_PATH${NC}"
    fi

    echo ""
    echo -e "${GREEN}✓  卸载完成。如需重新安装，再次运行 ./launchd-install.sh${NC}"
    exit 0
fi

# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
echo -e "${BLUE}╔══════════════════════════════════╗${NC}"
echo -e "${BLUE}║   NextMe LaunchAgent 安装        ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════╝${NC}"
echo ""

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

# 如果已加载，先卸载旧版本
if launchctl list | grep -q "$LABEL" 2>/dev/null; then
    echo -e "${YELLOW}⚠  发现已有 LaunchAgent，正在重新安装...${NC}"
    launchctl stop "$LABEL" 2>/dev/null || true
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

# 构建 launchd PATH：确保 claude CLI 等工具可被找到
LAUNCH_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[[ -d "$HOME/.local/bin" ]] && LAUNCH_PATH="$HOME/.local/bin:$LAUNCH_PATH"
[[ -d "/opt/homebrew/bin" ]] && LAUNCH_PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$LAUNCH_PATH"
echo -e "${GREEN}✓  launchd PATH：$LAUNCH_PATH${NC}"

# 创建日志目录
mkdir -p "$LOG_DIR"
echo -e "${GREEN}✓  日志目录：$LOG_DIR${NC}"

# 写入 plist
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- 唯一标识符 -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- 启动命令：uv run --project <dir> nextme up -->
    <key>ProgramArguments</key>
    <array>
        <string>${UV_PATH}</string>
        <string>run</string>
        <string>--project</string>
        <string>${PROJECT_DIR}</string>
        <string>nextme</string>
        <string>up</string>
    </array>

    <!-- 补全 PATH，确保 claude CLI 可被找到；禁用代理避免 SOCKS 错误 -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCH_PATH}</string>
        <key>ALL_PROXY</key>
        <string></string>
        <key>HTTPS_PROXY</key>
        <string></string>
        <key>HTTP_PROXY</key>
        <string></string>
        <key>all_proxy</key>
        <string></string>
        <key>https_proxy</key>
        <string></string>
        <key>http_proxy</key>
        <string></string>
        <key>no_proxy</key>
        <string>*</string>
    </dict>

    <!-- 工作目录 -->
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <!-- 日志输出：Python 内部 RotatingFileHandler 已写入 ${LOG_FILE}，
         launchd 输出重定向到 /dev/null 避免同一文件被写两次 -->
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>

    <!-- 崩溃后自动重启 -->
    <key>KeepAlive</key>
    <true/>

    <!-- 用户登录后自动启动 -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 重启最小间隔（秒），防止崩溃循环 -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!-- 提高调度优先级，减少锁屏后被节流的概率 -->
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
EOF
echo -e "${GREEN}✓  已写入 plist：$PLIST_PATH${NC}"

# 加载并启动
launchctl load "$PLIST_PATH"
launchctl start "$LABEL"
echo -e "${GREEN}✓  LaunchAgent 已加载并启动${NC}"

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━���━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  安装完成！NextMe 现已作为后台服务运行。${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  日志：  tail -f ${LOG_FILE}"
echo -e "  状态：  launchctl list | grep nextme"
echo -e "  停止：  launchctl stop ${LABEL}"
echo -e "  启动：  launchctl start ${LABEL}"
echo -e "  卸载：  ./launchd-install.sh --uninstall"
echo ""
