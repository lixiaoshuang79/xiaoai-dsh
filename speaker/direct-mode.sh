#!/bin/sh
# 音箱自主大脑守护：探测本地电脑（Mac）是否存活。
# Mac 挂了（migpt/桥全断）-> 切换「直连模式」（/tmp/direct_mode），
# native-block.sh 看到该标志后：问答直连大模型 + 本地 TTS 播报；
# 设备指令放行官方小爱执行（Mac 挂时官方云端是唯一设备控制通道）。
# Mac 恢复 -> 摘掉标志，回到正常模式（本地 AI 全功能）。
# 探测方式：GET http://MAC:4397/healthz（migpt 健康端点，纯 HTTP 200）。
#   连续 3 次失败才切直连模式；连续 2 次成功才切回（防网络抖动误判）。
#
# MAC_IP 从 /data/open-xiaoai/config.env 读取（由 xiaoai-dsh localhost 后台生成部署）；
# 也可以直接改本脚本里的 MAC 变量。

MAC=""
CONF="/data/open-xiaoai/config.env"
if [ -f "$CONF" ]; then
  . "$CONF" 2>/dev/null
  [ -n "$MAC_IP" ] && MAC="$MAC_IP"
fi
[ -z "$MAC" ] && MAC="192.168.1.100"
MODE_FLAG="/tmp/direct_mode"
FAIL=0
OK=0

log() { echo "$(date +%H:%M:%S) $1" >> /tmp/direct-mode.log; }

mac_alive() {
  # HTTP 200 且 body 含 ok = 存活
  resp=$(curl -s -m 3 http://$MAC:4397/healthz 2>/dev/null)
  echo "$resp" | grep -q '"ok":true' && return 0
  return 1
}

log "started, probing $MAC:4397/healthz every 5s"
while true; do
  if mac_alive; then
    OK=$((OK + 1)); FAIL=0
    if [ -f "$MODE_FLAG" ] && [ $OK -ge 2 ]; then
      rm -f "$MODE_FLAG"
      log "Mac 已恢复，退出直连模式（本地 AI 全功能回归）"
      OK=0
    fi
  else
    FAIL=$((FAIL + 1)); OK=0
    if [ ! -f "$MODE_FLAG" ] && [ $FAIL -ge 3 ]; then
      touch "$MODE_FLAG"
      log "Mac 失联（连续 $FAIL 次），进入直连模式"
    fi
  fi
  sleep 5
done
