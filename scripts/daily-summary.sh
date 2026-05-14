#!/bin/bash
# ============================================================
# daily-summary.sh — 谛听每日总结脚本
# 只记录状态日志，不记录聊天正文
# ============================================================
# crontab:
#   0 9 * * * $HOME/.diting/scripts/daily-summary.sh
# ============================================================

set -e
umask 077

WORKDIR="$HOME/.diting"
DITING_CMD="$WORKDIR/diting"
DATE_STR=$(date +%Y-%m-%d)
OUTPUT_DIR="$WORKDIR/outputs/$DATE_STR"
LOG_FILE="$WORKDIR/logs/daily-$DATE_STR.log"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$OUTPUT_DIR"
chmod 700 "$(dirname "$LOG_FILE")" "$OUTPUT_DIR" 2>/dev/null || true

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"; }

log "========================================"
log "谛听每日总结 — $DATE_STR"
log "========================================"

# 1. 检查微信
if ! pgrep -x WeChat > /dev/null; then
    log "[错误] 微信未运行，跳过"
    exit 1
fi

# 2. 检查 diting
if [ ! -f "$DITING_CMD" ]; then
    log "[错误] diting 未安装"
    exit 1
fi

# 3. 运行总结（只记录状态，不记录聊天正文）
log "开始运行总结，输出目录: $OUTPUT_DIR"
TMP_LOG="$(mktemp)"
trap 'rm -f "$TMP_LOG"' EXIT

if "$DITING_CMD" summarize --all --today --output-dir "$OUTPUT_DIR" --quiet > "$TMP_LOG" 2>&1; then
    grep -E "^(📊|📝|✅|❌|🔄|💾|\[错误\]|\[警告\]|\[提示\])" "$TMP_LOG" >> "$LOG_FILE" || true
    log "每日总结完成"
else
    RC=$?
    grep -E "^(📊|📝|✅|❌|🔄|💾|\[错误\]|\[警告\]|\[提示\])" "$TMP_LOG" >> "$LOG_FILE" || true
    log "[错误] 每日总结失败，退出码: $RC"
    rm -f "$TMP_LOG"
    exit "$RC"
fi
rm -f "$TMP_LOG"
trap - EXIT
SUMMARY_COUNT=$(ls "$OUTPUT_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
log "共生成 $SUMMARY_COUNT 份总结文件"
