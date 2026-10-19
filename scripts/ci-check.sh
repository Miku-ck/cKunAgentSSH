#!/usr/bin/env bash
# ci-check.sh — 本地一键 CI 检查（与 .github/workflows/ci.yml 检查项一致）
#
# 用法: bash scripts/ci-check.sh
# 任一项失败即退出非 0，可挂在 pre-push hook 或日常开发收尾。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1;36m━━ %s ━━\033[0m\n' "$*"; }
FAILED=0

run() { # run <描述> <命令...>
  local desc="$1"; shift
  step "$desc"
  if "$@"; then
    echo "✅ $desc"
  else
    echo "❌ $desc 失败"
    FAILED=1
  fi
}

# 1. 后端测试（含跨端白名单契约测试）—— 口径与 npm run test:backend / CI 同一来源
run "后端 pytest" npm run --silent test:backend

# 2. 前端类型检查
run "前端 typecheck (vue-tsc)" npm run --silent typecheck

# 3. 前端单测
run "前端单测 (vitest)" bash -c "npx vitest run"

# 4. 构建
run "electron-vite build" npm run --silent build

# 5. 网关字节漂移自检：hillgw.mjs 与 shared/gateway-mux.ts 是两套独立实现，
#    金样用例钉住两侧必须一致的帧格式。file_grep 声称它是"漂移守卫"，但此前
#    CI 与本地检查都不跑它 —— 改了 HEADER_LEN/帧序也照样全绿。
run "网关 mux 字节漂移自检 (hillgw --selftest)" node gateway/hillgw.mjs --selftest

step "结果"
if [ "$FAILED" -eq 0 ]; then
  echo "全部检查通过 ✅"
  exit 0
else
  echo "存在失败项 ❌"
  exit 1
fi
