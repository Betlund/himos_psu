#!/usr/bin/env bash
# 清理「任何」准备公开的私有副本：序列号、限量编号、__pycache__ 等。
# 不设默认目录——必须显式给出，避免误伤其他工程。
#
#   ./tools/sanitize_private.sh <目录>             预演，只列出会改什么
#   ./tools/sanitize_private.sh <目录> --apply     真正写入（先打 tar 备份）
#
# 也可用环境变量：HIMOS_PRIVATE_DIR=<目录> ./tools/sanitize_private.sh [--apply]
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/usr/bin/python3

TARGET="${1:-${HIMOS_PRIVATE_DIR:-}}"
MODE="${2:-}"
if [ "$TARGET" = "--apply" ]; then
    TARGET="${HIMOS_PRIVATE_DIR:-}"
    MODE="--apply"
fi

if [ -z "$TARGET" ]; then
    echo "用法：./tools/sanitize_private.sh <目录> [--apply]" >&2
    echo "本工具不会自动选择目录；请显式指定要公开的副本。" >&2
    exit 2
fi
if [ ! -d "$TARGET" ]; then
    echo "错误：$TARGET 不是目录" >&2
    exit 2
fi

if [ "$MODE" = "--apply" ]; then
    STAMP="$(date +%Y%m%d-%H%M%S)"
    BACKUP="$(dirname "$TARGET")/$(basename "$TARGET").sanitize-backup-$STAMP.tar.gz"
    echo "备份 → $BACKUP"
    tar -czf "$BACKUP" -C "$(dirname "$TARGET")" "$(basename "$TARGET")" || exit 1
    echo "备份大小: $(du -h "$BACKUP" | cut -f1)"
    "$PY" "$ROOT/tools/sanitize_private_copy.py" "$TARGET" --apply --drop-usb-node
    echo
    echo "复查是否存在残留序列号:"
    remaining="$(grep -rhoE 'SBRC[A-Z0-9]{11}' "$TARGET" --include='*.py' --include='*.md' 2>/dev/null \
        | grep -v '^SBRC00000000123$' | sort -u)"
    if [ -n "$remaining" ]; then
        echo "  仍有残留，请人工检查："
        echo "$remaining" | sed 's/^/    /'
        exit 1
    fi
    echo "  未发现真实序列号（仅保留合成值 SBRC00000000123）"
else
    "$PY" "$ROOT/tools/sanitize_private_copy.py" "$TARGET"
    echo
    echo "确认无误后执行：./tools/sanitize_private.sh $TARGET --apply"
fi
