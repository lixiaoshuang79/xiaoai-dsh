#!/bin/sh
# shellcheck shell=sh
# 音箱自主大脑守护：探测本地电脑（Mac）是否存活。
# Mac 挂了（migpt/桥全断）-> 切换「直连模式」（/tmp/xdf_direct_mode），
# native-block.sh 看到该标志后：问答直连大模型 + 本地 TTS 播报；
# 设备指令放行官方小爱执行（Mac 挂时官方云端是唯一设备控制通道）。
# Mac 恢复 -> 摘掉标志，回到正常模式（本地 AI 全功能）。
# 探测方式：GET http://MAC:4397/healthz（migpt 健康端点，纯 HTTP 200）。
#   连续 3 次失败才切直连模式；连续 2 次成功才切回（防网络抖动误判）。
#
# MAC_IP 从 /data/open-xiaoai/config.env 读取（由 xiaoai-dsh localhost 后台生成部署）；
# 未配置或含非法字符时拒绝运行（宁可不守护，也不拿错误地址探测）。
#
# ⚠️ 维护须知：修改本脚本部署到音箱后，须 kill 旧进程重启守护（旧逻辑在内存里）：
#     kill $(pgrep -f '/data/open-xiaoai/direct-mode.sh$') 2>/dev/null
#     sleep 0.5
#     /data/open-xiaoai/direct-mode.sh >/dev/null 2>&1 &

MAC=""
CONF="/data/open-xiaoai/config.env"
if [ -f "$CONF" ]; then
  . "$CONF" 2>/dev/null
  [ -n "$MAC_IP" ] && MAC="$MAC_IP"
fi
MODE_FLAG="/tmp/xdf_direct_mode"
FAIL=0
OK=0
LOG_FILE="/tmp/direct-mode.log"

log() {
  # 轮转：超过 512KB 时只保留最近 2000 行（busybox tail/mv 兼容）
  if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null)" -gt 524288 ]; then
    tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null
    mv "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null
  fi
  echo "$(date +%H:%M:%S) $1" >> "$LOG_FILE"
}

# ---- 单实例守卫（与 native-block.sh 同款：锚定 pgrep + pidfile 兜底竞态）----
for p in $(pgrep -f '/data/open-xiaoai/direct-mode\.sh$' 2>/dev/null); do
  [ "$p" = "$$" ] && continue
  kill "$p" 2>/dev/null
done
sleep 0.5
echo $$ > /tmp/direct-mode.pid
sleep 0.3
if [ "$(cat /tmp/direct-mode.pid 2>/dev/null)" != "$$" ]; then
  exit 0
fi

# ---- MAC_IP 校验（拼接探测 URL 前防注入）----
# 只允许 IP/主机名字符集 [0-9A-Za-z.:-]；未配置直接退出，不做静默默认。
case "$MAC" in
  '')
    log "MAC_IP 未配置（config.env 缺 MAC_IP），拒绝运行"
    exit 1
    ;;
  *[!0-9A-Za-z.:-]*)
    log "MAC_IP 含非法字符: $MAC（仅允许 [0-9A-Za-z.:-]），拒绝运行"
    exit 1
    ;;
esac

mac_alive() {
  # HTTP 200 且 body 含 ok = 存活
  resp=$(curl -s -m 3 "http://$MAC:4397/healthz" 2>/dev/null)
  echo "$resp" | grep -q '"ok":true' && return 0
  return 1
}

log "started, probing $MAC:4397/healthz every 5s"
while true; do
  if mac_alive; then
    OK=$((OK + 1)); FAIL=0
    if [ -f "$MODE_FLAG" ] && [ "$OK" -ge 2 ]; then
      # rm 前重查标志：防竞态（另一实例/刚被置位）导致标志震荡
      if [ -f "$MODE_FLAG" ]; then
        rm -f "$MODE_FLAG"
        log "Mac 已恢复，退出直连模式（本地 AI 全功能回归）"
      fi
      OK=0
    fi
  else
    FAIL=$((FAIL + 1)); OK=0
    if [ ! -f "$MODE_FLAG" ] && [ "$FAIL" -ge 3 ]; then
      # touch 前重查标志：防竞态下重复置位
      if [ ! -f "$MODE_FLAG" ]; then
        touch "$MODE_FLAG"
        log "Mac 失联（连续 $FAIL 次），进入直连模式"
      fi
      FAIL=0
    fi
  fi
  sleep 5
done