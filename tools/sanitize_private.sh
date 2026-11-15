#!/usr/bin/env bash
# 清理私有逆向副本（默认目标为本仓库对应的原目录）。
#   ./tools/sanitize_private.sh            预演，只列出会改什么
#   ./tools/sanitize_private.sh --apply    真正写入（会先备份）
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${HIMOS_PRIVATE_DIR:-$HOME/Project/鑫谷电源上位机逆向/himos_psu}"
PY=/usr/bin/python3

if [ ! -d "$TARGET" ]; then
    echo "错误：找不到私有副本 $TARGET" >&2
    echo "可用 HIMOS_PRIVATE_DIR=/path ./tools/sanitize_private.sh 指定" >&2
    exit 2
fi

if [ "${1:-}" = "--apply" ]; then
    STAMP="$(date +%Y%m%d-%H%M%S)"
    BACKUP="$TARGET.sanitize-backup-$STAMP.tar.gz"
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
    echo "确认无误后执行：./tools/sanitize_private.sh --apply"
fi
