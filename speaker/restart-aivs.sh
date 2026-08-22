#!/bin/sh
# 可靠重启 mico_aivs_lab 并保持 LD_PRELOAD 钩子链
# 钩子链（glibc 标准：LD_PRELOAD 前列者先解析）：
#   hook_final（设备词拦截）在链首，其 dlsym(RTLD_NEXT) 进入 hook_tts（官方 TTS 拦截），
#   hook_tts 透传直接发原始 syscall。
# 用法: restart-aivs.sh [第二钩子绝对路径，缺省 hook_final.so]
HOOK2="${1:-/data/open-xiaoai/hook_final.so}"
HOOK1="/data/open-xiaoai/hook_tts.so"
[ -f "$HOOK1" ] || HOOK1=""
[ -f "$HOOK2" ] || HOOK2=""
if [ -n "$HOOK1" ] && [ -n "$HOOK2" ]; then
  PRELOAD="$HOOK2:$HOOK1"
elif [ -n "$HOOK1" ]; then
  PRELOAD="$HOOK1"
else
  PRELOAD="$HOOK2"
fi
/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1
sleep 1
if [ -n "$PRELOAD" ]; then
  ubus call service set "{\"name\":\"mico_aivs_lab\",\"instances\":{\"instance1\":{\"command\":[\"/usr/bin/mico_aivs_lab\"],\"env\":{\"LD_PRELOAD\":\"$PRELOAD\"}}}}" >/dev/null 2>&1
else
  ubus call service set '{"name":"mico_aivs_lab","instances":{"instance1":{"command":["/usr/bin/mico_aivs_lab"]}}}' >/dev/null 2>&1
fi
