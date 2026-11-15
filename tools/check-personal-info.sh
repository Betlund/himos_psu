#!/usr/bin/env bash
# 阻止把个人信息/密钥提交进仓库。
# 作为 git pre-commit 钩子使用，也可手动运行：./tools/check-personal-info.sh
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

/usr/bin/python3 tools/privacy_scan.py --staged --worktree
status=$?

if [ "$status" -ne 0 ]; then
    cat <<'EOF'

────────────────────────────────────────────────────────────
提交被拦截：检测到个人信息或密钥。

常见处理：
  · 序列号/限量编号  改为合成值 SBRC00000000123 / No.123/1000
  · 绝对路径         改为相对路径或 $HOME 占位
  · 截图             用 --mock 的合成数据重新渲染
  · 字节码           删除 __pycache__，确认 .gitignore 覆盖 *.pyc

仅本次想跳过（不推荐）：git commit --no-verify
────────────────────────────────────────────────────────────
EOF
fi
exit "$status"
