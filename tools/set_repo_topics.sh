#!/usr/bin/env bash
# 把 .github/topics.txt 里的标签同步到 GitHub 仓库（话题用于被发现/引流）。
#   ./tools/set_repo_topics.sh            应用
#   ./tools/set_repo_topics.sh --dry-run  只打印将要写入的列表
#
# 需要 gh 已登录且对仓库有写权限；无权限时会明确说明，而不是抛一个 404。
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

TOPICS_FILE="$ROOT/.github/topics.txt"
REPO="$(git remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+)(\.git)?#\1#')"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

[ -f "$TOPICS_FILE" ] || { echo "错误：找不到 $TOPICS_FILE" >&2; exit 2; }
[ -n "$REPO" ] || { echo "错误：无法从 origin 解析 GitHub 仓库" >&2; exit 2; }

mapfile -t NAMES < <(grep -vE '^\s*(#|$)' "$TOPICS_FILE" | sed 's/\s*$//')
COUNT="${#NAMES[@]}"
if [ "$COUNT" -eq 0 ]; then
    echo "错误：topics 列表为空" >&2
    exit 2
fi
if [ "$COUNT" -gt 20 ]; then
    echo "错误：GitHub 每个仓库最多 20 个 topic，当前 $COUNT 个" >&2
    exit 2
fi
for name in "${NAMES[@]}"; do
    if ! printf '%s' "$name" | grep -qE '^[a-z0-9]+(-[a-z0-9]+)*$'; then
        echo "警告：'$name' 不符合 GitHub topic 规范（小写、连字符）" >&2
    fi
done

echo "仓库 : $REPO"
echo "数量 : $COUNT"
printf '将写入: %s\n' "$(printf '%s ' "${NAMES[@]}")"

if [ "$DRY" -eq 1 ]; then
    exit 0
fi

PUSH_OK="$(gh api "repos/$REPO" --jq '.permissions.push' 2>/dev/null)"
if [ "$PUSH_OK" != "true" ]; then
    USER="$(gh api user --jq .login 2>/dev/null)"
    cat >&2 <<EOF

错误：当前 gh 账号（${USER:-unknown}）对 $REPO 没有写权限，无法设置 topics
（GitHub 对无权限的写操作返回 404，看起来像仓库不存在）。

任选其一解锁：
  1) 换成仓库所有者账号：gh auth login
  2) 网页操作：仓库首页 About 右侧齿轮 → Topics，粘贴以下内容
     $(printf '%s ' "${NAMES[@]}")
EOF
    exit 1
fi

# 通过 stdin 传 JSON，避免写临时文件
printf '%s\n' "${NAMES[@]}" \
    | /usr/bin/python3 -c 'import json,sys; print(json.dumps({"names":[l.strip() for l in sys.stdin if l.strip()]}))' \
    | gh api -X PUT "repos/$REPO/topics" --input - --jq '.names[]' 2>&1 | sed 's/^/  ✓ /'
echo "完成：https://github.com/$REPO"
