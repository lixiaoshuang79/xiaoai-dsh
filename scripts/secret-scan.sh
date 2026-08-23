#!/usr/bin/env bash
# xiaoai-dsh 密钥扫描脚本
# 扫描 git tracked 文本文件中的常见密钥模式与疑似真实 IP（RFC5737 文档网段除外），
# 防止真实凭据/内网信息进入公开仓库。
#
# 用法：
#   bash scripts/secret-scan.sh            # 扫描全部 tracked 文件（CI 使用）
#   bash scripts/secret-scan.sh --staged   # 只扫描暂存区（提交前自查）
#
# 退出码：0=干净  1=发现疑似泄露/真实 IP  2=执行错误
#
# 排除（白名单，均为示例占位或自身模式串）：
#   config/config.example.json  示例配置（空 key + RFC5737 文档网段 IP）
#   SECURITY.md                 内含自查命令样本（模式字符串本身）
#   scripts/secret-scan.sh      本脚本自身（含模式字符串）
#   docs/                       文档示例占位（由文档维护者统一管理）

set -u

STAGED=0
if [ "${1:-}" = "--staged" ]; then
  STAGED=1
fi

EXCLUDES=(
  ':(exclude)config/config.example.json'
  ':(exclude)SECURITY.md'
  ':(exclude)scripts/secret-scan.sh'
  ':(exclude)docs'
)

# IP 检查额外豁免：仅豁免**必须验证私网/公网 IP 语义**的测试文件
# （isBlockedHostname/validate_audio_url 等测试用例的样本 IP，非真实网络信息）。
# 仅豁免「疑似真实 IP」检查；密钥模式检查（run_grep 默认路径）仍覆盖这些文件。
# 新增含示例 IP 的测试文件时须在此登记（附理由），否则 CI 会红。
# 注意：仅能换 RFC5737 的样本（如纯解析/示例值）一律先换，不要进此名单。
EXCLUDES_IP=(
  ':(exclude)bridge/test_bridge.py'              # validate_audio_url 私网/公网语义测试
  ':(exclude)migpt/test/speaker-gate.test.ts'    # isBlockedHostname 私网/公网语义测试
)

# 常见密钥模式（git grep 扩展正则；不要加会命中模板代码的宽松模式）
SECRET_PATTERNS='AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN (RSA|OPENSSH|EC|DSA|PRIVATE) PRIVATE KEY-----|sk-[A-Za-z0-9]{20,}|eyJhbGciOi[A-Za-z0-9_-]{10,}|Bearer [A-Za-z0-9._-]{20,}'

fail=0

run_grep() {
  # $@ = pattern + 其余 git grep 参数
  local pattern="$1"
  shift
  if [ "$STAGED" = "1" ]; then
    git grep --cached -nE -I "$pattern" -- . "${EXCLUDES[@]}" "$@" 2>/dev/null
  else
    git grep -nE -I "$pattern" -- . "${EXCLUDES[@]}" "$@" 2>/dev/null
  fi
}

# ---- 1. 密钥模式检查（hard fail）----
output=$(run_grep "$SECRET_PATTERNS")
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "❌ [secret] 检测到疑似密钥模式："
  echo "$output"
  fail=1
elif [ "$rc" -gt 1 ]; then
  echo "❌ [secret] git grep 执行失败（rc=$rc）"
  fail=1
fi

# ---- 2. 疑似真实 IP 检查（RFC5737 文档网段之外视为可疑）----
# 安全网段：127.0.0.0/8（回环）、0.0.0.0、255.255.255.255（广播）、
#           RFC5737 文档网段 192.0.2.0/24、198.51.100.0/24、203.0.113.0/24。
# 其余（RFC1918 内网/公网地址）一律报告——公开仓库不应出现真实网络信息。
is_safe_ip() {
  case "$1" in
    127.*|0.*|255.255.255.255) return 0 ;;
    192.0.2.*|198.51.100.*|203.0.113.*) return 0 ;;
    169.254.169.254) return 0 ;;  # 云 metadata 保留地址（代码里作为 SSRF 拒绝面出现；精确例外，不放行其他 link-local）
  esac
  return 1
}

# 跳过浏览器 UA 版本号等形如 IPv4 的合法数字串（Chrome/150.0.0.0 之类）
is_version_string_line() {
  printf '%s\n' "$1" | grep -qE '(Chrome|Safari|Firefox|Gecko|WebKit)/[0-9.]+' && return 0
  return 1
}

ip_output=$(run_grep '[0-9]{1,3}(\.[0-9]{1,3}){3}' "${EXCLUDES_IP[@]}")
rc=$?
if [ "$rc" -eq 0 ]; then
  bad=""
  while IFS= read -r line; do
    if is_version_string_line "$line"; then
      continue
    fi
    for ip in $(printf '%s\n' "$line" | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}'); do
      if ! is_safe_ip "$ip"; then
        bad="$bad$line"$'\n'
        break
      fi
    done
  done <<< "$ip_output"
  if [ -n "$bad" ]; then
    echo "❌ [ip] 检测到疑似真实 IP（仅允许 127.x / 0.x / 255.255.255.255 / RFC5737 文档网段）："
    printf '%s' "$bad"
    echo "   提示：示例 IP 请改用 RFC5737 文档网段（192.0.2.0/24、198.51.100.0/24、203.0.113.0/24）"
    fail=1
  fi
elif [ "$rc" -gt 1 ]; then
  echo "❌ [ip] git grep 执行失败（rc=$rc）"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "✅ 密钥扫描通过：无密钥模式、无疑似真实 IP"
  exit 0
fi
exit 1
