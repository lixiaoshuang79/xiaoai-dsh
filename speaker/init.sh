#!/bin/sh
# shellcheck shell=sh
# 小爱音箱开机初始化（部署到音箱 /data/init.sh，由 /etc/rc.local 在开机时调起）
sleep 10
# 1. 官方小爱提示音静音（保留 wakeup_* 唤醒音；其余全部静音，每次开机重刷）
if [ -f /data/open-xiaoai/silent.wav ]; then
  for f in /data/sound/*; do
    [ -f "$f" ] || continue
    case "$f" in
      *wakeup_*) continue ;;
      *) cp /data/open-xiaoai/silent.wav "$f" ;;
    esac
  done
fi
# 1.5 固件连续对话开关（免唤醒词续聊的前提之一：官方唤醒后保持会话/听音窗口；
#     配合官方进程保持在线 + AI 播报完静默唤醒，用户可直接接话/打断）
echo on > /data/mipns/dialog_continuous 2>/dev/null
# 2. TTS 音色固定为男声（青葱 XiaoMi_M88，可按需改）
ubus call mibrain tts_vendor_switch '{"vendor_name":"XiaoMi_M88","language_name":"cmn-Hans-CN","switch_src":"manual","dialog_id":"","tone_type":0}' > /dev/null 2>&1
# 3. 原生执行拦截：设备指令在原生执行前被拦下，全权交给本地 AI（HA 通道执行）；
#    Mac 挂时自动切换直连模式（用户配置的大模型 + 本地 TTS 自主回答）
[ -x /data/open-xiaoai/native-block.sh ] && ( /data/open-xiaoai/native-block.sh >/dev/null 2>&1 & )
[ -x /data/open-xiaoai/direct-mode.sh ] && ( /data/open-xiaoai/direct-mode.sh >/dev/null 2>&1 & )
# 3.5 零竞态拦截器：给 mico_aivs_lab 注入 LD_PRELOAD（设备指令在写日志瞬间击杀进程，云端收不到请求）
[ -x /data/open-xiaoai/restart-aivs.sh ] && [ -f /data/open-xiaoai/hook_final.so ] && \
  /data/open-xiaoai/restart-aivs.sh /data/open-xiaoai/hook_final.so >/dev/null 2>&1
# 4. AI 桥 client（server.txt 里写 Mac 的局域网 IP）
SERVER=$(cat /data/open-xiaoai/server.txt 2>/dev/null)
[ -n "$SERVER" ] && ( /data/open-xiaoai/client "$SERVER" > /data/open-xiaoai/client.log 2>&1 & )
exit 0
