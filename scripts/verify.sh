#!/usr/bin/env bash
# xiaoai-dsh 本地一键验证（给维护者用，与 CI 对齐）：
#   python 编译 + 单测 → secret-scan → shellcheck → TypeScript typecheck/test → Rust check/test
# 用法：bash scripts/verify.sh        （未安装的工具自动跳过并提示）
# 前置：migpt 目录已 pnpm install（本脚本不自动安装依赖）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/6 python compile =="
python3 -m compileall -q bridge admin

echo "== 2/6 python unittest (bridge + admin) =="
python3 -m unittest discover -s bridge -p 'test_*.py'
python3 -m unittest discover -s admin -p 'test_*.py'

echo "== 3/6 secret-scan =="
bash scripts/secret-scan.sh

echo "== 4/6 shellcheck =="
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -S warning speaker/*.sh bridge/*.sh
  echo "shellcheck 通过"
else
  echo "shellcheck 未安装，跳过（CI 会自动安装）"
fi

echo "== 5/6 TypeScript (migpt) =="
if command -v pnpm >/dev/null 2>&1; then
  (cd migpt && pnpm exec tsc --noEmit && pnpm test)
else
  echo "pnpm 未安装，跳过"
fi

echo "== 6/6 Rust (migpt) =="
if command -v cargo >/dev/null 2>&1; then
  (cd migpt && cargo check && cargo test)
else
  echo "cargo 未安装，跳过"
fi

echo "✅ verify.sh 全部通过"
