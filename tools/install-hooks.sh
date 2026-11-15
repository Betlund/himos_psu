#!/usr/bin/env bash
# 安装本地 git 钩子（不进入版本库，各自仓库安装一次）。
#   ./tools/install-hooks.sh         安装 pre-commit + pre-push
#   ./tools/install-hooks.sh --force 覆盖已存在的钩子
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS="$ROOT/.git/hooks"
TARGET="$HOOKS/pre-commit"
PUSH="$HOOKS/pre-push"

if [ ! -d "$HOOKS" ]; then
    echo "错误：$HOOKS 不存在（这里不是 git 仓库？）" >&2
    exit 2
fi

if [ -f "$TARGET" ] && [ "${1:-}" != "--force" ]; then
    echo "已存在 $TARGET，未覆盖。确认要覆盖请加 --force" >&2
    exit 1
fi

cat > "$TARGET" <<'HOOK'
#!/usr/bin/env bash
# 由 tools/install-hooks.sh 生成：提交前扫描个人信息与密钥
exec "$(git rev-parse --show-toplevel)/tools/check-personal-info.sh"
HOOK
chmod +x "$TARGET"

if [ -f "$PUSH" ] && [ "${1:-}" != "--force" ]; then
    echo "已存在 $PUSH，跳过 pre-push（要覆盖加 --force）"
else
    cat > "$PUSH" <<'HOOK'
#!/usr/bin/env bash
# 由 tools/install-hooks.sh 生成：推送前跑完整流水线
exec "$(git rev-parse --show-toplevel)/tools/pipeline.sh"
HOOK
    chmod +x "$PUSH"
    echo "已安装 pre-push 钩子 → $PUSH"
fi

echo "已安装 pre-commit 钩子 → $TARGET"
echo "手动自测: ./tools/pipeline.sh"
