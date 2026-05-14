#!/bin/bash
# ============================================================
# check-release.sh — 谛听发布前隐私检查
# 检查目标目录中是否包含敏感文件或疑似密钥
# 发现问题则返回非零退出码，阻止打包
# ============================================================
set -e

TARGET_DIR="${1:?用法: $0 <发布目录>}"

if [ ! -d "$TARGET_DIR" ]; then
    echo "❌ 目录不存在: $TARGET_DIR" >&2
    exit 1
fi

echo "🔍 检查发布目录: $TARGET_DIR"
echo ""

ERRORS=0
WARNINGS=0

# ═════════════════════════════════════════
# 1. 检查敏感文件
# ═════════════════════════════════════════

SENSITIVE_PATTERNS="wechat_keys|keys_extract|\.db$|\.sqlite$|\.sqlite3$|\.key$|config\.yaml$"

echo "  检查敏感文件..."
while IFS= read -r file; do
    if [ -n "$file" ]; then
        echo "  ❌ 发现敏感文件: $file"
        ERRORS=$((ERRORS + 1))
    fi
done < <(find "$TARGET_DIR" -type f | grep -E "$SENSITIVE_PATTERNS" || true)

# ═════════════════════════════════════════
# 2. 检查疑似密钥内容
# ═════════════════════════════════════════

echo "  检查疑似密钥内容..."
KEY_PATTERN='wxid_[a-zA-Z0-9_]+|sk-[A-Za-z0-9_-]{16,}'

while IFS= read -r match; do
    if [ -n "$match" ]; then
        # 排除占位符和注释
        # 检查匹配行中是否包含 xxx 占位符或 # 注释标记
        if echo "$match" | grep -qE '(xxxx|placeholder|example|示例|wxid_xxx)'; then
            continue
        fi
        # 提取文件名和行内容
        content_part=$(echo "$match" | sed 's/^[^:]*:[0-9]*://')
        # 跳过行内注释中的 wxid
        if echo "$content_part" | grep -qE '#.*wxid_'; then
            continue
        fi
        echo "  ❌ 发现疑似密钥: $match"
        ERRORS=$((ERRORS + 1))
    fi
done < <(grep -R -n -E "$KEY_PATTERN" "$TARGET_DIR" 2>/dev/null || true)

# ═════════════════════════════════════════
# 3. 检查不应存在的目录
# ═════════════════════════════════════════

echo "  检查不应存在的目录..."
FORBIDDEN_DIRS="outputs logs tmp decrypted wechat-db-decrypt-macos __pycache__ venv"

for d in $FORBIDDEN_DIRS; do
    if [ -d "$TARGET_DIR/$d" ]; then
        echo "  ❌ 发现禁止目录: $d/"
        ERRORS=$((ERRORS + 1))
    fi
done

# ═════════════════════════════════════════
# 4. 检查必须存在的文件
# ═════════════════════════════════════════

echo "  检查必须文件..."
REQUIRED_FILES="summarize.py install.sh README.md config.example.yaml"

for f in $REQUIRED_FILES; do
    if [ ! -f "$TARGET_DIR/$f" ]; then
        echo "  ⚠️  缺少必要文件: $f"
        WARNINGS=$((WARNINGS + 1))
    fi
done

# ═════════════════════════════════════════
# 5. 检查 config.example.yaml 中不能有真实密钥
# ═════════════════════════════════════════

echo "  检查示例配置..."
EXAMPLE_CONFIG="$TARGET_DIR/config.example.yaml"
if [ -f "$EXAMPLE_CONFIG" ]; then
    if grep -qE '(sk-[A-Za-z0-9]{20,}|api_key:\s+sk-)' "$EXAMPLE_CONFIG" 2>/dev/null; then
        echo "  ❌ config.example.yaml 包含疑似真实 API Key"
        ERRORS=$((ERRORS + 1))
    fi
fi

# ═════════════════════════════════════════
# 结果
# ═════════════════════════════════════════

echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo "❌ 隐私检查未通过：发现 $ERRORS 个问题"
    exit 1
elif [ "$WARNINGS" -gt 0 ]; then
    echo "⚠️  隐私检查通过（有 $WARNINGS 个警告）"
    exit 0
else
    echo "✅ 隐私检查通过"
    exit 0
fi
