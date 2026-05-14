#!/bin/bash
# ============================================================
# build-release.sh — 谛听发布打包脚本（白名单模式）
# 只打包允许的文件，确保不泄漏敏感信息
# ============================================================
set -e

VERSION="${1:?用法: $0 <版本号>  例如: $0 1.5.0}"
STRIP_VER="${VERSION#v}"  # 去掉前缀 v
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_NAME="谛听-v${STRIP_VER}"
RELEASE_DIR="/tmp/${RELEASE_NAME}"
ZIP_PATH="$HOME/Desktop/${RELEASE_NAME}.zip"

# ═════════════════════════════════════════
# 白名单：只打包这些文件
# ═════════════════════════════════════════

ALLOWED_FILES=(
    "summarize.py"
    "install.sh"
    "README.md"
    "SKILL.md"
    "config.example.yaml"
    "requirements.txt"
    ".gitignore"
    "scripts/daily-summary.sh"
    "scripts/build-release.sh"
    "scripts/check-release.sh"
)

# ═════════════════════════════════════════
# 黑名单：绝对不能出现在发布包中
# ═════════════════════════════════════════

DENIED_PATTERNS=(
    "config\.yaml"
    "wechat_keys\.json"
    "keys_extract\.log"
    "\.db$"
    "\.sqlite$"
    "\.sqlite3$"
    "\.key$"
)

echo "========================================="
echo "  谛听发布打包 v${STRIP_VER}"
echo "========================================="
echo ""

# 1. 清理旧的构建目录
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR/scripts"

# 2. 按白名单复制文件
echo "📦 复制文件..."
COPIED=0
for f in "${ALLOWED_FILES[@]}"; do
    src="$PROJECT_DIR/$f"
    if [ -f "$src" ]; then
        cp "$src" "$RELEASE_DIR/$f"
        echo "  ✅ $f"
        COPIED=$((COPIED + 1))
    else
        echo "  ⚠️  跳过（不存在）: $f"
    fi
done

echo ""
echo "  共复制 $COPIED 个文件"

# 3. 设置执行权限
chmod +x "$RELEASE_DIR/install.sh" 2>/dev/null || true
chmod +x "$RELEASE_DIR/scripts/"*.sh 2>/dev/null || true

# 4. 运行隐私检查
echo ""
echo "🔍 运行隐私检查..."
CHECK_SCRIPT="$PROJECT_DIR/scripts/check-release.sh"
if [ -f "$CHECK_SCRIPT" ]; then
    bash "$CHECK_SCRIPT" "$RELEASE_DIR" || {
        echo ""
        echo "❌ 隐私检查未通过，终止打包！"
        rm -rf "$RELEASE_DIR"
        exit 1
    }
else
    echo "  ⚠️  check-release.sh 不存在，跳过自动检查"
    echo "  建议手动检查发布包"
fi

# 5. 打包
echo ""
echo "📦 打包..."
rm -f "$ZIP_PATH"
cd /tmp
zip -r -q "$ZIP_PATH" "$RELEASE_NAME"

# 6. 计算校验和
CHECKSUM=$(shasum -a 256 "$ZIP_PATH" | awk '{print $1}')
SIZE=$(du -h "$ZIP_PATH" | awk '{print $1}')

# 7. 清理
rm -rf "$RELEASE_DIR"

echo ""
echo "========================================="
echo "  ✅ 打包完成！"
echo "========================================="
echo ""
echo "  文件: $ZIP_PATH"
echo "  大小: $SIZE"
echo "  SHA256: $CHECKSUM"
echo ""
