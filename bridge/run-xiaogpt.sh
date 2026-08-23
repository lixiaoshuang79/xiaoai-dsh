#!/bin/sh
# shellcheck shell=sh
# xiaogpt 常驻服务包装脚本：
#   1) 从后台生成的 xiaogpt-credentials 读取小米账号
#   2) 若令牌缺失/损坏则静默刷新（设备ID已验证，无需手机确认）
#   3) 启动 xiaogpt，连本地桥（127.0.0.1:8322）
# 依赖：本目录下 .venv-xiaogpt（pip install xiaogpt），
#       配置文件由 localhost 后台生成：config/generated/bridge/ 下两个文件
#       会自动复制到本目录（bridge/xiaogpt-credentials、bridge/xiaogpt-config.yml）。
# 注：xiaogpt-credentials 由 admin 后台用 shlex.quote 生成（值带单引号包裹），
#     用点号（. file）读取可正确解析含引号/特殊字符的值。
cd "$(dirname "$0")" || exit 1
if [ -f xiaogpt-credentials ]; then
    . ./xiaogpt-credentials
elif [ -f ../config/generated/bridge/xiaogpt-credentials ]; then
    . ../config/generated/bridge/xiaogpt-credentials
fi
CFG="xiaogpt-config.yml"
[ -f "$CFG" ] || CFG="../config/generated/bridge/xiaogpt-config.yml"
# 令牌自愈：文件不存在或过小视为损坏
if [ ! -s "$HOME/.mi.token" ] || [ "$(wc -c < "$HOME/.mi.token")" -lt 100 ]; then
    .venv-xiaogpt/bin/python refresh-token.py || true
fi
exec .venv-xiaogpt/bin/xiaogpt --config "$CFG"
