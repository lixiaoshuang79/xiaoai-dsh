# xiaoai-dsh 常见问题（FAQ）

本文收录项目从零搭建至今的真实踩坑记录。每条按「现象 → 原因 → 解决」
组织；全部问题均已解决。不涉及任何真实账号、人名与具体歌名。

---

## 1. 官方小爱「复活」抢答

**现象**：明明已经杀了官方进程，点歌/媒体类指令下达后，官方小爱还是抢先
说话、甚至自己放起了歌单（如念「打开小米音箱 App」并开始播歌）。

**原因**：官方 TTS 由 mibrain_service 独立进程播放、媒体播放由
mediaplayer/quickplayer 独立服务执行，**全部独立于 mico_aivs_lab**——
杀掉主进程拦不住已经下发的 TTS/歌单。官方云端对点歌类媒体指令响应极快，
可能在杀进程之前就把 TTS 和播放指令下发出去。

**解决**：`native-block.sh` 的 `kill_official_leftovers` 在 restart_aivs
之后同步掐残留：停 mediaplayer → 杀 miplayer → 重启 mibrain_service
（掐官方 TTS）→ 重启 quickplayer → 延迟 3s 再停一次 mediaplayer（官方
重连后云端可能补发媒体指令，拉歌单较慢随后才开播）。本地 AI 音乐走
miplayer 进程不受影响。

---

## 2. 设备双重执行（官方和 AI 都执行）

**现象**：Mac 明明在线，音箱却进入了直连模式，设备指令被官方执行了一遍、
AI 又执行了一遍（红外空调关后又开）。

**原因**：Mac 存活探测用错了端口——探测 4399（WS 端口），curl 对 WebSocket
端口的退出码乱跳（28/52/56），导致永久误判「Mac 挂了」→ 误切直连模式 →
官方独占设备执行 + AI 也执行 = 双执行。

**解决**：探测必须用 migpt 的 4397 `/healthz` **纯 HTTP** 端点
（0.0.0.0 监听，返回 `{"ok":true}`）；**禁止探测 4399 WS 端口**。切换需
连续确认：连续 3 次失败才切直连模式、连续 2 次成功才切回（防网络抖动误判）。

---

## 3. migpt 重启冲掉音箱端 LD_PRELOAD 注入

**现象**：migpt 侧做任何 abortXiaoAI（init.d restart）之后，音箱端拦截链
失效——官方设备执行又活了。

**原因**：init.d restart 是干净重启，procd respawn 会重置 service 配置的
env，把 `LD_PRELOAD=hook_final.so` 的注入冲掉；拦截链 hook 失效后官方
云端设备执行链路解锁。

**解决**：**一律用 restart-aivs.sh**（带 hook 的干净重启：init.d restart
后 `ubus call service set` 重新注入 LD_PRELOAD），绝不用 init.d restart。
「闭嘴」打断也改为 runShell 调 restart-aivs.sh（停播 + 保 hook 重启）。

---

## 4. 音箱 exec 命令自杀（exit_code -1）

**现象**：通过音箱 exec 执行 `pgrep -f xxx` 类命令时进程自杀，返回
exit_code -1。

**原因**：`pgrep -f` 非锚定模式会匹配 exec shell 自身——执行命令的 shell
命令行里就包含命令文本，把自己匹配进去了。

**解决**：一律用锚定正则 `pgrep -f '^...$'`（如
`pgrep -f '^/usr/bin/mico_aivs_lab$'`）。`ps|grep` 同理会匹配 exec shell
自身，造成假 NO-PRELOAD。

---

## 5. heredoc 部署脚本 MD5 不一致

**现象**：本地文件与音箱端文件 MD5 总对不上，部署校验一直失败。

**原因**：heredoc 部署脚本会在文件末尾多一个尾随空行，MD5 比对前本地文件
与远端内容差一个换行。

**解决**：本地文件先补一个换行再比对（或比对时忽略尾随换行）。

---

## 6. 深通道 YAML 配置被 yaml.safe_dump 重写破坏

**现象**：改完深通道 settings.yaml 后，reasoningEfforts 枚举失效，或
`off:` 键变成 `false: null`，深通道报配置错误。

**原因**：PyYAML 按 YAML 1.1 解析布尔值，`off` 会被解析成 `false`；用
`yaml.safe_dump` 重写整个文件会把 `off:` 键序列化成 `false: null`，破坏
reasoningEfforts 枚举语义。

**解决**：改 YAML 配置**严禁 yaml.safe_dump 重写**，用原文模板 + 占位符
复制（后台生成派生文件即用此方式）；手工修改时只改目标值不动结构。

---

## 7. 深通道掉进插件默认模型

**现象**：深通道突然报 QUOTA/欠费类错误，或行为与配置不符。

**原因**：音箱 DSH 的 settings.yaml 是独立配置，**不自动继承主 DSH** 的
agent-default-model / llm-pi-ai 段；主 DSH 改模型时音箱侧没同步，深通道
掉进插件默认模型（走官方 API，账户常欠费）。

**解决**：音箱 settings.yaml 独立维护必备两段：`agent-default-model`
（深模型 + reasoningEffort high）+ `llm-pi-ai.providers` 完整模型表；
快通道靠 `dsh-fast.patch.yml` 覆盖快模型。主 DSH 改模型时必须同步音箱侧。
诊断：`DSH_HOME=<音箱home> TSX_TSCONFIG_PATH=<checkout>/tsconfig.json
node --import tsx/esm <checkout>/apps/cli/src/bin.ts --profile headless
[--patch dsh-fast.patch.yml] "测试"` 看实际走的模型。

---

## 8. 网易云点歌「一会有一会没」

**现象**：点歌后音乐刚响一下就被打断，时有时无，体验混乱。

**原因**：工具阶段（netease_music_play / web_audio_play）直接播放，播放与
AI 回答播报竞争——「AI 说话停音乐」把刚响的音乐打断；工具轮在回答前返回，
播放又插进回答 chunk 之间。

**解决**：**工具阶段绝不直接播放**，只登记 `_pending_play(url, title)`
并返回「已找到：X」；播放统一由 `_flush_pending_play()` 在 AI 回答播报
完成后推送（speaker-gate 的 pendingAfterAnswer 保证挂起到 endAnswer 后
播）。web-audio-play 必须用 --no-play 配合（只搜索拿 URL 不 POST）。

---

## 9. 红外空调指令多轮重复操作

**现象**：说「空调调到 25 度」，模型反复查询验证、重复操作多次才完成，
甚至撞上工具轮上限说「我查得有点绕」。

**原因**：红外空调无状态回读，state 全部 unknown——任何「查状态验证」
对其无效，模型工具链必然多轮搜索 + 重复操作（实测 6 轮 4 次操作撞上限）。

**解决**：**桥侧确定性正则短路**，不走模型：「空调 + 数字温度」→
`_ac_temp_shortcut` 直连 HA number 实体 set_value（带「开」字先按开机键
+ 0.5s 间隔；范围 16-30），先于所有路由判断一次完成。ROUTER_INSTRUCTION
同步立铁律：只用 number 实体 set_value，禁温度±按钮，禁反复查询。新增
无回读设备指令时照此模式做确定性短路。

---

## 10. 音箱后台脚本退出即死

**现象**：音箱上 `sh script.sh &` 启动的后台脚本，exec shell 一退出进程
就没了，native-block/direct-mode 起不来。

**原因**：busybox ash 下直接 `cmd &` 启动的子进程挂在 exec shell 的会话/
进程组下，shell 退出即被回收。

**解决**：一律 `( cmd & )` **双括号脱离**（子 shell 后台），exec shell
退出后进程继续活。init.sh 里三个常驻脚本都是这种写法。

---

## 11. 官方新闻电台/媒体指令突然响

**现象**：正常模式下，官方新闻电台或媒体指令偶尔突然发声（叠声），
与本地 AI 回答混在一起。

**原因**：官方小爱的媒体执行链（新闻电台/音乐/闹钟）独立于 AI 问答流，
migpt 的 callAIKeywords 拦截不到，只能靠音箱端杀官方进程；而原词表只拦
设备词，新闻类词漏网。

**解决**：音箱端拦截词表补媒体词——native-block.sh 新增 blocked-news
分支（新闻/资讯/头条/热点/大事/要闻 → restart_aivs 保 hook 重启）。
改完脚本后必须 kill 旧 tail 进程再重启（sh 已把旧逻辑加载到内存）。

---

## 12. 单实例守卫误判「多实例」

**现象**：native-block.sh 的 pgrep 检查发现自己「有多个实例」，互相杀、
反复重启。

**原因**：busybox ash 的管道循环子 shell 会继承主进程命令行，ps 看起来
像多个实例；非锚定 pgrep 还会匹配 exec 部署 shell 自身（其命令行是整个
命令 blob）。

**解决**：锚定 pgrep（`/data/open-xiaoai/native-block\.sh$`）把 main +
循环子 shell + 延迟清理子 shell 一起杀光（只留自己），再加 pidfile 兜底
同时启动的竞态（后写者胜出，先写者自动退出）；tail 进程用锚定
`^tail -n 0 -F …instruction\.log$` 单独清理。配合 `tail -n 0 -F` 防止
重启时重放旧日志误杀一轮官方。
