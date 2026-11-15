#!/usr/bin/env bash
# 本地流水线：语法检查 → 单元测试 → 隐私扫描 → 汇总
# 用法:
#   ./tools/pipeline.sh          完整跑一遍
#   ./tools/pipeline.sh --quick  跳过历史扫描（pre-commit 用）
set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
PY=/usr/bin/python3
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

fail=0
run_stage() {
    local name="$1"; shift
    printf '\n\033[1m== %s ==\033[0m\n' "$name"
    if "$@"; then
        printf '   \033[32mPASS\033[0m %s\n' "$name"
    else
        printf '   \033[31mFAIL\033[0m %s\n' "$name"
        fail=$((fail + 1))
    fi
}

stage_compile() { "$PY" -m compileall -q src tests tools; }
stage_tests()   { "$PY" -m unittest discover -s tests 2>&1 | tail -4; }
stage_privacy() { "$PY" tools/privacy_scan.py --worktree --staged; }
stage_history() { "$PY" tools/privacy_scan.py --history --messages; }

run_stage "语法检查 (compileall)" stage_compile
run_stage "单元测试 (unittest)" stage_tests
run_stage "隐私扫描 (工作区 + 暂存区)" stage_privacy
if [ "$QUICK" -eq 0 ]; then
    run_stage "隐私扫描 (全历史 + 提交信息)" stage_history
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
    printf '\033[32m流水线通过\033[0m\n'
    exit 0
fi
printf '\033[31m流水线失败：%d 个阶段未通过\033[0m\n' "$fail"
exit 1
