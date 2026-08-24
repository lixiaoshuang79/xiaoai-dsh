#!/bin/sh
# shellcheck shell=sh
# 音箱自主大脑（正常模式=Mac 在线）：
#   官方进程保持在线（不杀），唤醒后会话/听音窗口完整 → 用户免唤醒词接话/打断
#   （连续对话，2026-08-24 真机验证打通）；
#   官方一切发声/执行由 hook 链零竞态拦截（TTS→hook_tts 杀 mediaplayer、
#   设备→hook_final 自杀、媒体→kill_official_execution 停播放器）；
#   问答/设备/媒体全部由本地 AI（Mac 上的桥）接管；
#   官方设备词自杀重连后云端补发的响应由执行指令兜底（kill_official_execution）掐执行部件。
# 直连模式（Mac 挂了，direct-mode.sh 置 /tmp/xdf_direct_mode）：
#   官方放行设备/媒体，问答拦截官方抢答 + 直连大模型 + 本地 TTS 播报。
#
# ⚠️ 维护须知：修改本脚本部署到音箱后，必须 kill 掉旧的 tail 进程并重启守护——
#   旧脚本逻辑已被加载进内存，不重启会继续跑旧逻辑。重启命令：
#     kill $(pgrep -f '^tail -n 0 -F /tmp/mico_aivs_lab/instruction.log$') 2>/dev/null
#     kill $(pgrep -f '/data/open-xiaoai/native-block.sh$') 2>/dev/null
#     sleep 0.5
#     /data/open-xiaoai/native-block.sh >/dev/null 2>&1 &
#
# 大模型配置从 /data/open-xiaoai/config.env 读取（由 xiaoai-dsh localhost
# 后台生成部署），降级提示词从 /data/open-xiaoai/system_prompt.txt 读取。
DEVICE='开|关|打开|关闭|调|亮度|色温|模式|风速|扫|拖|加湿|除湿|灯|插座|风扇|窗帘|热水器|空调|净化|开关|电源|温度'
EXCEPT='放|播放|唱|来首|听|歌|音乐|故事|新闻|电台|广播|闹钟|提醒|倒计时|音量|声音|上一首|下一首|暂停|继续|晚安|早安|起床'
MODE_FLAG="/tmp/xdf_direct_mode"

CONF="/data/open-xiaoai/config.env"
LLM_BASE=""
LLM_KEY=""
LLM_MODEL=""
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090  # 配置文件由后台运行时生成，路径非常量
  . "$CONF" 2>/dev/null
fi

# ---- 配置值基础校验（防注入）----
# LLM_BASE/LLM_KEY/LLM_MODEL 来自 config.env，会拼进 curl URL、Authorization 头
# 与 JSON 请求体。虽然全部加双引号，仍限制字符集兜底：拒绝空白/引号/控制字符。
if [ -n "$LLM_BASE" ] && printf '%s' "$LLM_BASE" | grep -q '[^A-Za-z0-9:/._-]'; then
  echo "$(date +%H:%M:%S) invalid-LLM_BASE: rejected" >> /tmp/nb-trace.log
  exit 1
fi
if [ -n "$LLM_KEY" ] && printf '%s' "$LLM_KEY" | grep -q '[^A-Za-z0-9._:@/+=-]'; then
  echo "$(date +%H:%M:%S) invalid-LLM_KEY: rejected" >> /tmp/nb-trace.log
  exit 1
fi
if [ -n "$LLM_MODEL" ] && printf '%s' "$LLM_MODEL" | grep -q '[^A-Za-z0-9._:/@-]'; then
  echo "$(date +%H:%M:%S) invalid-LLM_MODEL: rejected" >> /tmp/nb-trace.log
  exit 1
fi

if [ -f /data/open-xiaoai/system_prompt.txt ]; then
  # 转单行 + 转义双引号（提示词要嵌进 JSON 请求体）
  LLM_SYSTEM=$(tr '\n' ' ' < /data/open-xiaoai/system_prompt.txt | sed 's/\\/\\\\/g; s/"/\\"/g')
else
  LLM_SYSTEM="你是小爱，这家的智能语音管家，称呼用户为先生。当前本地电脑离线，你处于直连云端大模型的降级模式：没有设备控制工具、没有深度思考，只能基础问答。回答口语化、简短，不超过三句话，中文。开灯关灯查设备这类事做不了，就说：本地电脑不在线，这类事情暂时做不了，先生。简单问答、聊天、算术直接回答。"
fi

# ---- 单实例守卫 ----
# busybox ash 的管道循环子 shell 会继承主进程命令行（ps 看起来像多实例），
# 用锚定正则把 main + 循环子 shell + 延迟清理子 shell 一起杀光（只留自己），
# 锚定可防 exec 部署 shell 自匹配（其命令行是整个命令 blob，不会精确等于脚本路径）。
# pidfile 兜底同时启动的竞态：后写者胜出，先写者自动退出。
for p in $(pgrep -f '/data/open-xiaoai/native-block\.sh$' 2>/dev/null); do
  [ "$p" = "$$" ] && continue
  kill "$p" 2>/dev/null
done
for t in $(pgrep -f '^tail -n 0 -F /tmp/mico_aivs_lab/instruction\.log$' 2>/dev/null; \
           pgrep -f '^tail -F /tmp/mico_aivs_lab/instruction\.log$' 2>/dev/null); do
  kill "$t" 2>/dev/null
done
sleep 0.5
echo $$ > /tmp/native-block.pid
sleep 0.3
if [ "$(cat /tmp/native-block.pid 2>/dev/null)" != "$$" ]; then
  exit 0
fi

LOG_FILE="/tmp/nb-trace.log"
log() {
  # 轮转：超过 512KB 时只保留最近 2000 行，防止无限增长（busybox tail/mv 兼容）
  if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null)" -gt 524288 ]; then
    tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null
    mv "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null
  fi
  echo "$(date +%H:%M:%S) $1" >> "$LOG_FILE"
}

say() {
  # 本地 TTS 播报（XiaoMi_M88 男声，init.sh 已固定音色）
  # 文本嵌 JSON 前先转义反斜杠与双引号
  tts_txt=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
  ubus call mibrain text_to_speech "{\"text\":\"$tts_txt\",\"save\":0}" >/dev/null 2>&1
}

llm_ask() {
  # 直连大模型（OpenAI 兼容），返回回答文本；失败返回空
  [ -z "$LLM_KEY" ] && return 1
  [ -z "$LLM_BASE" ] && return 1
  q=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
  body=$(printf '{"model":"%s","messages":[{"role":"system","content":"%s"},{"role":"user","content":"%s"}],"thinking":{"type":"disabled"}}' \
    "$LLM_MODEL" "$LLM_SYSTEM" "$q")
  curl -s -m 15 -X POST "$LLM_BASE/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $LLM_KEY" \
    -d "$body" 2>/dev/null | sed -n 's/.*"content":"\([^"]*\)".*/\1/p' | head -1
}

restart_aivs() {
  if [ -x /data/open-xiaoai/restart-aivs.sh ]; then
    ( /data/open-xiaoai/restart-aivs.sh /data/open-xiaoai/hook_final.so >/dev/null 2>&1 & )
  else
    ( /etc/init.d/mico_aivs_lab restart >/dev/null 2>&1 & )
  fi
}

kill_tts_chain() {
  # v3 钩子在官方写 Speak 的瞬间已零竞态杀光 mediaplayer（官方 TTS 唯一播放者，
  # 我们走 miplayer 不受影响）。这里只做延时补重启：确认没有存活的 mediaplayer
  # 才重启——若还活着说明是钩子放行的官方合法应答（闹钟/音量确认），绝不能打断。
  # 注意：/bin/pidof 会匹配僵尸进程（重启残留），必须用 /proc/stat 状态排除 Z。
  ( sleep 0.6
    alive=0
    for p in $(/bin/pidof mediaplayer 2>/dev/null); do
      [ "$(cut -d' ' -f3 /proc/$p/stat 2>/dev/null)" != "Z" ] && alive=1
    done
    if [ "$alive" = "0" ]; then
      sleep 1.4
      /etc/init.d/mediaplayer restart >/dev/null 2>&1
    fi
  ) &
}

kill_official_leftovers() {
  # 官方 TTS/媒体播放链独立于 mico_aivs_lab（mediaplayer/quickplayer 各有自己的服务进程）。
  # 官方云端对点歌等媒体指令响应极快，可能在杀进程前就已把播放指令下发出去——
  # 必须同步掐掉，否则「官方复活」（孙燕姿事故：官方说「打开小米音箱APP」还自己
  # 放起了歌单）。官方 TTS 由 v3 钩子在 Speak 落盘瞬间杀 mediaplayer 拦截，
  # 这里无需预杀（避免误伤正在播放的本地 AI 播报）。
  ubus call mediaplayer player_play_operation '{"action":"stop"}' >/dev/null 2>&1
  mphelper pause >/dev/null 2>&1
  # 杀 miplayer 重试 3 次（0.2s 间隔）：防播放器进程被杀后又被拉起残留
  for _ in 1 2 3; do
    for p in $(/bin/pidof miplayer 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
    sleep 0.2
  done
  # 清官方播放器状态（quickplayer 是官方媒体播放进程）
  ( /etc/init.d/quickplayer restart >/dev/null 2>&1 & )
}

kill_official_execution() {
  # 官方补发响应兜底（五月天事故）：官方被重启后重连云，云端会把之前
  # 没送达的响应（官方音乐搜索很慢，可晚到 30 秒+）补发给重连后的官方，官方照样
  # 说话放歌。这里在官方执行指令落盘瞬间掐执行部件，且不重启官方进程——重启只会
  # 诱发再次补发，形成循环。
  case "$1" in
    Execute)
      # 设备执行指令：IR 执行在官方进程内，只能杀官方进程拦（hook 词表已零竞态
      # 覆盖常见设备词，这里兜底罕见词漏网场景）
      restart_aivs
      log "blocked-exec-device: Execute"
      ;;
    Speak|StartAnswer)
      # 官方说话：v3 钩子已在写指令瞬间零竞态杀 mediaplayer（官方 TTS 唯一播放者，
      # 我们走 miplayer 不受影响），这里补 mediaplayer 死了才延时重启。
      # 不碰 mibrain/misound/官方进程——重启官方只会诱发云端再次补发形成死循环。
      # @uptime 用于竞速延迟诊断
      kill_tts_chain
      log "blocked-exec-tts: $1 @$(cut -d' ' -f1 /proc/uptime)"
      ;;
    *)
      # 官方媒体指令（Play/歌单等）：官方播放是 REPLACE_ALL 会顶掉本地音乐，必须掐
      ubus call mediaplayer player_play_operation '{"action":"stop"}' >/dev/null 2>&1
      mphelper pause >/dev/null 2>&1
      ( /etc/init.d/quickplayer restart >/dev/null 2>&1 & )
      log "blocked-exec-media: $1"
      ;;
  esac
}

log "started"
# ---- 主循环（tail 守护）----
# tail -n 0：只读新增行，重启时不重放旧日志（避免刚启动就误杀一轮官方）。
# 外层 while 重启保护：tail -F 在日志被删除/重建时会继续跟随，但若 tail 进程
# 意外死亡（OOM/被杀），管道结束会导致整个守护静默退出——拦截失效 = 官方复活。
# 因此管道结束后 sleep 1 自动重启，拦截守护绝不能死。
while :; do
  tail -n 0 -F /tmp/mico_aivs_lab/instruction.log 2>/dev/null | while read -r line; do
    name=$(printf '%s\n' "$line" | sed -n 's/.*"name":"\([A-Za-z_]*\)".*/\1/p')
    [ -z "$name" ] && continue

    # ---- 官方响应执行指令补发兜底（正常/直连模式通用判断，直连放行官方） ----
    case "$name" in
      StartAnswer|Speak|Play|LOOP_MODE|SetProperty|InstructionControl|Execute|Group|wangyiyun)
        if [ ! -f "$MODE_FLAG" ]; then
          kill_official_execution "$name"
        fi
        continue
        ;;
    esac

    [ "$name" = "RecognizeResult" ] || continue
    case "$line" in
      *'"is_final":true'*) ;;
      *) continue ;;
    esac
    text=$(printf '%s\n' "$line" | sed -n 's/.*"text":"\([^"]*\)".*/\1/p')
    # 已知局限：ASR 文本若含转义引号（\"）会在此处截断——语音文本几乎不含引号，
    # 截断只影响直连模式的问答内容，不影响拦截判定（判定只看 is_final + name）。
    [ -z "$text" ] && continue

    if [ -f "$MODE_FLAG" ]; then
      # ---- 直连模式（Mac 挂了）：音箱自主兜底 ----
      # 媒体/闹钟/音量等：放行官方（官方能力，Mac 挂了也能用）
      printf '%s\n' "$text" | grep -qE "$EXCEPT" && continue
      # 设备指令：放行官方小爱云端执行！
      # Mac 挂时 HA 不可用，官方云端是唯一能控家里设备的通道；
      # 若 Mac 其实没挂（误判），官方执行 + AI 执行会双执行（红外 toggle 灾难），
      # 所以这里绝不能杀官方，必须让官方独占设备执行。
      if printf '%s\n' "$text" | grep -qE "$DEVICE"; then
        log "direct-device-let-official: $text"
        continue
      fi
      # 非设备问答：杀官方抢答 + 大模型直连 + 本地 TTS
      log "direct-mode: $text"
      restart_aivs   # 杀掉官方 NLP/TTS，抢答
      answer=$(llm_ask "$text")
      if [ -n "$answer" ]; then
        say "$answer"
        log "direct-answer: $(printf '%.60s' "$answer")"
      else
        say "抱歉，云端大模型暂时也连不上，请稍后再试，先生。"
        log "direct-fail: $text"
      fi
      continue
    fi

    # ---- 正常模式：官方执行全禁（2026-08-24 连续对话修复版） ----
    # 不再每轮 restart_aivs 杀官方：官方被杀后永远处于重启-重连状态，「唤醒后
    # 会话/播报后听音窗口」从不建立，用户免唤醒词接话/打断全部失效
    # （event_notify 静默/有声唤醒与 oneshot 在空闲态均无效，真机三连实验证实）。
    # 改为官方进程保持在线，官方永远不发声不执行，但会话状态完整：
    #   · 官方 TTS：hook_tts 拦（Speak 写指令瞬间杀 mediaplayer，闹钟除外）
    #   · 官方媒体：hook_tts 拦 + 上方 kill_official_execution 兜底
    #   · 官方设备指令：hook_final 词表零竞态使官方进程自杀；漏网 Execute 由
    #     上方执行指令兜底 restart_aivs（设备词场景牺牲会话，可接受）
    #   · 唤醒后官方保持会话 → AI 播报完官方听音窗口恢复 → 免唤醒词接话/打断。
    # 孙燕姿事故补强保留：官方响应快时可能已抢先下发播放——同步掐残留。
    kill_official_leftovers
    log "blocked-pass: $text"
    continue
  done
  log "tail-exited: 守护自动重启"
  sleep 1
done