#!/bin/bash
set -e

# ============================================================
# 谛听（DiTing）v1.5 — 一键安装脚本
# ============================================================

umask 077

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { printf "${CYAN}[INFO]${NC}  %s\n" "$1"; }
ok()    { printf "${GREEN}[OK]${NC}    %s\n" "$1"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$1"; }
err()   { printf "${RED}[ERROR]${NC} %s\n" "$1"; }

# 固定第三方工具版本
DECRYPT_TOOL_TAG="v1.0"

# ═════════════════════════════════════════
# 环境检测
# ═════════════════════════════════════════

info "检测系统环境..."

OS=$(uname -s)
ARCH=$(uname -m)

if [ "$OS" != "Darwin" ]; then
    err "仅支持 macOS"
    exit 1
fi

if [ "$ARCH" != "arm64" ]; then
    err "仅支持 Apple Silicon (M 系列芯片) Mac。当前架构: $ARCH"
    exit 1
fi
ok "系统: macOS $ARCH"

# --- SIP 检测（仅警告，不再强制） ---
info "检查 SIP 状态..."
SIP_STATUS=$(csrutil status 2>/dev/null || echo "unknown")
if echo "$SIP_STATUS" | grep -q "disabled"; then
    ok "SIP 已关闭（密钥提取需要）"
    ok "注意：安装完成后建议恢复 SIP：csrutil enable"
elif echo "$SIP_STATUS" | grep -q "enabled"; then
    warn "SIP 已开启。首次安装需要关闭 SIP 才能提取密钥。"
    warn "关闭方法：关机 → 按住电源键 → 选项 → 终端 → csrutil disable"
    warn "如果你已有密钥文件（wechat_keys.json），可以跳过关闭 SIP。"
    printf "密钥文件存在？(y/N): "
    read -r HAS_KEYS
    if [ "$HAS_KEYS" != "y" ] && [ "$HAS_KEYS" != "Y" ]; then
        warn "请先参考 README 关闭 SIP，然后重新运行本脚本。"
        exit 1
    fi
else
    warn "无法检测 SIP 状态，继续安装..."
fi

# --- 微信进程 ---
info "检查微信运行状态..."
WECHAT_COUNT=$(pgrep -x WeChat | wc -l | tr -d ' ')
if [ "$WECHAT_COUNT" -eq 0 ]; then
    warn "微信未运行。请打开微信并登录后再运行本脚本。"
    exit 1
elif [ "$WECHAT_COUNT" -gt 1 ]; then
    warn "检测到 $WECHAT_COUNT 个微信进程。"
    printf "是否继续？(y/N): "
    read -r CONTINUE
    if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
        exit 1
    fi
else
    ok "微信运行中"
fi

# --- Homebrew ---
info "检查 Homebrew..."
if ! command -v brew &>/dev/null; then
    err "需要 Homebrew 才能安装系统依赖。请先安装："
    err '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi
ok "Homebrew 已安装"

# --- Python ---
info "检查 Python..."
PYTHON=""
for p in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/bin/python3; do
    if [ -x "$p" ]; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    info "安装 Python 3.12..."
    brew install python@3.12
    PYTHON="/opt/homebrew/bin/python3.12"
fi
PY_VER=$("$PYTHON" --version 2>&1 | awk '{print $2}')
ok "Python: $PY_VER ($PYTHON)"

# ═════════════════════════════════════════
# 安装系统依赖
# ═════════════════════════════════════════

info "安装系统依赖 (llvm, sqlcipher)..."
brew install llvm sqlcipher
ok "系统依赖安装完成"

# ═════════════════════════════════════════
# 设置项目目录
# ═════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR="$HOME/.diting"
TOOL_DIR="$WORKDIR/wechat-db-decrypt-macos"
VENV_DIR="$WORKDIR/venv"

mkdir -p "$WORKDIR"

# ═════════════════════════════════════════
# 克隆解密工具（固定 tag，不 git pull）
# ═════════════════════════════════════════

if [ -d "$TOOL_DIR/.git" ]; then
    ok "解密工具已存在（跳过更新）"
else
    info "克隆解密工具（版本: $DECRYPT_TOOL_TAG）..."
    if ! git clone --depth 1 --branch "$DECRYPT_TOOL_TAG" \
        https://github.com/Thearas/wechat-db-decrypt-macos.git "$TOOL_DIR" 2>/dev/null; then
        err "克隆指定版本 ($DECRYPT_TOOL_TAG) 失败"
        err "请确认版本号是否正确，或手动克隆后重新运行"
        printf "是否继续使用默认版本？(y/N): "
        read -r CONTINUE
        if [ "$CONTINUE" = "y" ] || [ "$CONTINUE" = "Y" ]; then
            warn "回退到默认分支，版本不受控制"
            git clone --depth 1 \
                https://github.com/Thearas/wechat-db-decrypt-macos.git "$TOOL_DIR"
        else
            exit 1
        fi
    fi
    ok "解密工具就绪"
fi

# ═════════════════════════════════════════
# 创建虚拟环境
# ═════════════════════════════════════════

if [ ! -d "$VENV_DIR" ]; then
    info "创建 Python 虚拟环境..."
    "$PYTHON" -m venv "$VENV_DIR"
fi
ok "虚拟环境就绪: $VENV_DIR"

# ═════════════════════════════════════════
# 安装 Python 依赖（固定版本）
# ═════════════════════════════════════════

info "安装 Python 依赖..."
VENV_PYTHON="$VENV_DIR/bin/python3"
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet pyyaml==6.0 requests==2.31.0
deactivate
ok "Python 依赖安装完成"

# ═════════════════════════════════════════
# 复制核心文件
# ═════════════════════════════════════════

# 配置文件（不覆盖已有，只从 example 复制）
if [ -f "$SCRIPT_DIR/config.example.yaml" ] && [ ! -f "$WORKDIR/config.yaml" ]; then
    cp "$SCRIPT_DIR/config.example.yaml" "$WORKDIR/config.yaml"
    chmod 600 "$WORKDIR/config.yaml"
    info "配置文件已创建: $WORKDIR/config.yaml"
fi

# 主脚本
if [ -f "$SCRIPT_DIR/summarize.py" ]; then
    cp "$SCRIPT_DIR/summarize.py" "$WORKDIR/summarize.py"
    chmod +x "$WORKDIR/summarize.py"
fi

# 辅助脚本
if [ -d "$SCRIPT_DIR/scripts" ]; then
    mkdir -p "$WORKDIR/scripts"
    cp -r "$SCRIPT_DIR/scripts/"* "$WORKDIR/scripts/"
    chmod +x "$WORKDIR/scripts/"*.sh 2>/dev/null || true
fi

# ═════════════════════════════════════════
# 生成稳定的 diting 命令
# ═════════════════════════════════════════

WRAPPER_SCRIPT="$WORKDIR/diting"
cat > "$WRAPPER_SCRIPT" << 'WRAPPER'
#!/bin/bash
# 谛听（DiTing）— 稳定的 CLI 入口
# 自动查找可用的 Python（venv 优先）
for py in \
    "$HOME/.diting/venv/bin/python3" \
    "$HOME/.diting/wechat-db-decrypt-macos/venv/bin/python3" \
    "/opt/homebrew/bin/python3.12" \
    "/usr/bin/python3"; do
    if [ -x "$py" ]; then
        exec "$py" "$HOME/.diting/summarize.py" "$@"
    fi
done
echo "错误: 找不到 Python，请先运行 install.sh" >&2
exit 1
WRAPPER
chmod +x "$WRAPPER_SCRIPT"

# 添加到 PATH
SHELL_RC="$HOME/.zshrc"
ALIAS_CMD="alias diting='$WRAPPER_SCRIPT'"

if grep -q "alias diting=" "$SHELL_RC" 2>/dev/null; then
    ok "终端命令 diting 已存在"
else
    echo "" >> "$SHELL_RC"
    echo "# 谛听（DiTing）" >> "$SHELL_RC"
    echo "$ALIAS_CMD" >> "$SHELL_RC"
    ok "终端命令已配置。重新加载终端或执行 'source ~/.zshrc' 后可用: diting"
fi

# ═════════════════════════════════════════
# 安装 WorkBuddy Skill
# ═════════════════════════════════════════

SKILL_DIR="$HOME/.workbuddy/skills/diting"
SKILL_FILE="$SKILL_DIR/SKILL.md"

if [ -f "$SCRIPT_DIR/SKILL.md" ]; then
    mkdir -p "$SKILL_DIR"
    cp "$SCRIPT_DIR/SKILL.md" "$SKILL_FILE"
    ok "已安装为 WorkBuddy Skill"
    info "对小龙虾说："帮我总结一下XXX群""
fi

# ═════════════════════════════════════════
# 密钥提取（仅在需要时）
# ═════════════════════════════════════════

KEYS_FILE="$TOOL_DIR/wechat_keys.json"
if [ -f "$KEYS_FILE" ] && [ -s "$KEYS_FILE" ]; then
    info "密钥文件已存在，跳过提取（如需重新提取请删除 $KEYS_FILE）"
else
    info "从微信进程内存中提取数据库密钥..."
    echo "（这可能需要 10-30 秒，请确保微信处于登录状态）"

    # 使用临时文件 + trap 确保密钥日志被清理
    KEY_LOG="$(mktemp)"
    trap 'rm -f "$KEY_LOG"' EXIT

    cd "$TOOL_DIR"
    # 防止 set -e 中断：即使密钥提取脚本失败也继续执行后续逻辑
    PYTHONPATH_OVERRIDE="$(/opt/homebrew/opt/llvm/bin/lldb -P 2>/dev/null || echo '')"
    if PYTHONPATH="$PYTHONPATH_OVERRIDE" "$PYTHON" find_key_memscan.py > "$KEY_LOG" 2>&1; then
        if grep -q "keys found" "$KEY_LOG" 2>/dev/null; then
            ok "密钥提取成功"
        else
            warn "密钥提取完成但未检测到 keys found，请运行 diting doctor 检查"
        fi
    else
        warn "密钥提取脚本执行失败，请检查 SIP、微信进程和微信版本"
        warn "可运行 diting doctor 查看详细信息"
    fi
    # trap 会自动清理 KEY_LOG
fi

# ═════════════════════════════════════════
# 设置文件权限
# ═════════════════════════════════════════

info "设置文件权限..."
chmod 700 "$WORKDIR" 2>/dev/null || true
chmod 700 "$WORKDIR/outputs" "$WORKDIR/tmp" "$WORKDIR/decrypted" 2>/dev/null || true
chmod 600 "$WORKDIR/config.yaml" 2>/dev/null || true
if [ -f "$KEYS_FILE" ]; then
    chmod 600 "$KEYS_FILE"
fi
ok "文件权限已设置"

# ═════════════════════════════════════════
# 测试
# ═════════════════════════════════════════

info "运行快速测试..."
TEST_RESULT=$("$VENV_PYTHON" "$WORKDIR/summarize.py" --version 2>&1)
if echo "$TEST_RESULT" | grep -q "diting\|谛听" 2>/dev/null; then
    ok "安装完成！🎉"
    echo ""
    echo "========================================="
    echo "  谛听（DiTing）v1.5 已就绪！"
    echo "========================================="
    echo ""
    echo "快速开始："
    echo "  1. 编辑配置: vim $WORKDIR/config.yaml"
    echo "  2. 环境自检: diting doctor"
    echo "  3. 列出会话: diting list"
    echo "  4. 今日总结: diting summarize --all --today"
    echo ""
    echo "📖 完整文档请查看 README.md"
    echo ""
else
    warn "测试未通过，请检查安装日志。"
    warn "手动测试: diting doctor"
fi
