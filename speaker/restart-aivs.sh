#!/bin/sh
# 可靠重启 mico_aivs_lab 并保持 LD_PRELOAD 钩子
# 用法: restart-aivs.sh [hook.so 绝对路径]
# 原理：
#   1. init.d restart = 干净重启（procd respawn retry=0，崩溃后不会自动拉起，只能走这里）
#   2. service set 注入 env —— env 从「无」变为「有钩子」= 配置变化，procd 会内部重启一次
# 最终状态 = 新实例带钩子运行。
HOOK="${1:-/data/open-xiaoai/hook_final.so}"
[ -f "$HOOK" ] || HOOK=""
/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1
sleep 1
if [ -n "$HOOK" ]; then
  ubus call service set "{\"name\":\"mico_aivs_lab\",\"instances\":{\"instance1\":{\"command\":[\"/usr/bin/mico_aivs_lab\"],\"env\":{\"LD_PRELOAD\":\"$HOOK\"}}}}" >/dev/null 2>&1
else
  ubus call service set '{"name":"mico_aivs_lab","instances":{"instance1":{"command":["/usr/bin/mico_aivs_lab"]}}}' >/dev/null 2>&1
fi
