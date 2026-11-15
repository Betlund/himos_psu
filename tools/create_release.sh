#!/usr/bin/env bash
# 用已打好的 tag 在 GitHub 上创建 Release。
#   ./tools/create_release.sh <notes文件> [tag]
# 例：./tools/create_release.sh _preview/release-notes-v0.2.0.md v0.2.0
#
# 需要 gh 已登录且对目标仓库有写权限；脚本会先检查，避免白跑一次。
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

NOTES="${1:?用法: ./tools/create_release.sh <notes文件> [tag]}"
TAG="${2:-$(git describe --tags --abbrev=0 2>/dev/null)}"
REPO="$(git remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+)(\.git)?#\1#')"

if [ -z "$TAG" ]; then
    echo "错误：无法确定 tag，请显式传入" >&2
    exit 2
fi
if [ -z "$REPO" ]; then
    echo "错误：无法从 origin 解析 GitHub 仓库" >&2
    exit 2
fi
if [ ! -f "$NOTES" ]; then
    echo "错误：notes 文件不存在：$NOTES" >&2
    exit 2
fi
command -v gh >/dev/null || { echo "错误：未安装 gh" >&2; exit 2; }

echo "仓库: $REPO"
echo "tag : $TAG"

PUSH_OK="$(gh api "repos/$REPO" --jq '.permissions.push' 2>/dev/null)"
if [ "$PUSH_OK" != "true" ]; then
    USER="$(gh api user --jq .login 2>/dev/null)"
    cat >&2 <<EOF
错误：当前 gh 账号（${USER:-unknown}）对 $REPO 没有写权限，无法创建 Release。
解决其一：
  1) 换有权限的账号：gh auth login
  2) 把当前账号加为协作者（仓库 Settings → Collaborators）
  3) 直接网页创建：https://github.com/$REPO/releases/new?tag=$TAG
EOF
    exit 1
fi

TAG_EXISTS="$(gh api "repos/$REPO/git/ref/tags/$TAG" --jq '.ref' 2>/dev/null)"
if [ -z "$TAG_EXISTS" ]; then
    echo "错误：远端不存在 tag $TAG（先 git push --follow-tags）" >&2
    exit 1
fi

TITLE="${TAG} — $(sed -n 's/^description = "\(.*\)"/\1/p' pyproject.toml | cut -c1-60)"
echo "创建 Release…"
gh api -X POST "repos/$REPO/releases" \
    -F tag_name="$TAG" \
    -F name="$TITLE" \
    -F body=@"$NOTES" \
    -F draft=false \
    -F prerelease=false \
    --jq '.html_url'
