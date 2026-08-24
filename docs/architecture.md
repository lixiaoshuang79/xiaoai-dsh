# xiaoai-dsh 架构说明

xiaoai-dsh 把小米 AI 音箱改造成「本地大模型语音管家」：音箱只负责收音与播报，
理解与执行全部交给本地（Mac）上的桥与深通道大脑。官方云端仅保留语音识别
（ASR），回答、设备控制、媒体播放均由本地链路接管。

## 1. 总体链路

```
┌─────────────────────────────── 音箱（小米 AI 音箱，root 刷机）───────────────────────────────┐
│                                                                                              │
│  用户说话 ──▶ 小爱同学唤醒（原生，本地链路，唤醒音「在呢」不受拦截）                            │
│                  │                                                                           │
│                  ▼                                                                           │
│  官方 ASR 云端识别（唯一保留的云端依赖；完全断网 = 音箱聋）                                    │
│                  │                                                                           │
│                  ▼                                                                           │
│  RecognizeResult 写入 /tmp/mico_aivs_lab/instruction.log                                     │
│                  │                                                                           │
│     ┌────────────┼──────────────────────────────┐                                            │
│     ▼            ▼                              ▼                                            │
│  native-block.sh（官方进程保持在线（连续对    │  open-xiaoai client                       │
│  话需要会话态），官方发声/执行由 hook 链零竞态     │  （音箱端 client，连 Mac 4399 WS）        │
│  拦截：TTS→hook_tts 杀 mediaplayer、设备→         │                                            │
│  hook_final 自杀、媒体→停播放器）                 │                                            │
│     │            kill_official_leftovers 停官方 │                                            │
│     │            TTS（mibrain_service）与媒体    │                                            │
│     │            播放（mediaplayer/quickplayer） │                                            │
│     │                                           ▼                                            │
│     └─────────────── 直连模式（Mac 失联时）      │                                            │
│                     /tmp/direct_mode 标志        │                                            │
│                     ├─ 问答：直连大模型 + 本地    │                                            │
│                     │  TTS（ubus mibrain          │                                            │
│                     │  text_to_speech）           │                                            │
│                     └─ 设备指令：放行官方云端     │                                            │
│                        （唯一设备控制通道）       │                                            │
└──────────────────────────────────────────────────┼────────────────────────────────────────────┘
                                                   │ WS（4399）
                                                   ▼
┌─────────────────────────────── Mac 本机 ────────────────────────────────────────────────────┐
│                                                                                              │
│  migpt 引擎（MiGPT-Next）                                                                    │
│    ├─ 4397 /healthz  健康检查（0.0.0.0，纯 HTTP，音箱端直连模式探测用）                        │
│    ├─ 4398 推送端点（127.0.0.1）：/play /play_url /native /exec                               │
│    ├─ 4399 WS 音箱连接（open-xiaoai）                                                        │
│    └─ speaker-gate.ts 播报互斥门（见 §6）                                                     │
│         │  OpenAI 兼容（127.0.0.1:8322/v1）                                                   │
│         ▼                                                                                    │
│  xiaogpt-bridge（8322，OpenAI 兼容端点）——本项目的「路由大脑」                                │
│    ├─ classify_intent 意图识别（11 域 40+ 意图，路由确定性，见 §4）                            │
│    ├─ 快通道：快模型直连带工具（1-5 秒），hass-mcp 工具 + 桥内置工具                           │
│    ├─ 深通道：dsh --profile headless（深模型深度推理，DSH_HOME 隔离）                          │
│    ├─ 豆包通道：国内实时资讯快速搜索（新闻/行情/路况/比分/价格）                               │
│    ├─ 容灾兜底：快模型纯问答（无工具，诚实告知「后台大脑离线」）                               │
│    └─ 自我进化：技能沉淀（speaker-skills/）+ 声明式工具（evolved-tools.json）                  │
│         │  MCP（Streamable HTTP，127.0.0.1:8321/mcp）                                         │
│         ▼                                                                                    │
│  hass-mcp（8321）──▶ Home Assistant（8123）──▶ 米家设备（MIoTLan 本地直连）                    │
│                                                                                              │
│  其他：4378 web-audio 防盗链转发（音箱拉流）、8390 本地配置后台（127.0.0.1）                   │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

一条完整的问答链路（正常模式）：

```
用户说话 → 音箱唤醒 → 官方 ASR 识别 → instruction.log 落盘
  → native-block.sh 记录 blocked-pass（官方进程保持在线，不杀；hook 链拦官方发声/执行）
  → open-xiaoai client 把文本发给 migpt（4399 WS）
  → migpt 探测桥健康（8322/v1/models）→ 调桥
  → 桥 classify_intent 分类意图 → 按路由分发（flash / flash_tools / deep / doubao / native）
  → 快通道：流式回答（垫场词 → 工具调用 → 结论）→ migpt 逐段播报
  → migpt 播报完自动静默唤醒（连续对话：用户免唤醒词直接接话/打断；event_notify
    src:1 在官方在线会话态有效）；桥 <<dialogue:keep_open|end>> 标记保留对话语义
  → 音箱 TTS 播报
```


## 1.1 仓库结构（顶层目录与依赖方向）

```
speaker/         音箱端脚本（busybox ash）：native-block.sh 拦截/兜底、
                 direct-mode.sh 直连模式、restart-aivs.sh 干净重启等
                  │  WS（4399，open-xiaoai client）
                  ▼
migpt/           TS 编排引擎 + Rust 底层 client（open-xiaoai.node）
                 ├─ src/*.ts     对话编排、speaker-gate 播报互斥、config
                 ├─ src/*.rs     WS client / auth / exec RPC（neon 编译为 node 模块）
                 └─ test/        单测（TS + Rust）
                  │  OpenAI 兼容（127.0.0.1:8322/v1，Bearer bridge.secret）
                  ▼
bridge/          Python 桥（8322）——「路由大脑」编排层
                 ├─ xiaogpt-bridge.py   编排主文件（意图/路由/通道/HTTP Handler）
                 ├─ security.py         URL/IP 校验与日志脱敏（叶子）
                 ├─ state_store.py      原子写 / turn 代际 / 全局锁（叶子）
                 ├─ topic_state.py      话题档案/待答复/历史持久化（叶子）
                 ├─ device_discovery.py HA 设备自动发现（叶子）
                 ├─ config_loader.py    配置读取（叶子，所有模块共用）
                 └─ test_*.py           各模块回归测试（纯离线）
                  │  MCP（8321） / HA REST（8123）
                  ▼
        Home Assistant ──▶ 米家设备（MIoTLan 本地直连）
                  │  深通道（dsh --profile headless，DSH_HOME 隔离）
                  ▼
        DSH 深通道（Mac 上的本地大模型大脑）

admin/           本地配置后台（8390，X-Admin-Token）——配置面，不在运行时主链路
packages/        migpt 依赖的本地包（@mi-gpt 等 pnpm workspace）
config/          配置模板与生成物（local.json 不入库；runtime 数据不在仓库）
scripts/         verify.sh / secret-scan.sh 等开发与 CI 脚本
docs/            架构/配置/部署/FAQ 文档
```

依赖方向铁律：主桥 xiaogpt-bridge.py → 叶子模块（security / state_store /
topic_state / device_discovery / config_loader），**叶子模块绝不反向 import
主桥**。叶子模块只依赖标准库与 config_loader；LLM 编排（topic_choose /
topic_summarize）留在主桥，topic_state 通过参数注入摘要函数。

## 2. 端口地图

| 端口 | 进程 | 监听 | 用途 |
|------|------|------|------|
| 8322 | xiaogpt-bridge | 127.0.0.1 | OpenAI 兼容端点（/v1/chat/completions、/v1/models），migpt 的唯一 AI 上游；/v1/chat/completions 需 `Bearer <bridge.secret>`，/v1/models 无鉴权（健康探测用） |
| 8321 | hass-mcp | 127.0.0.1 | Home Assistant 全量工具 MCP（Streamable HTTP） |
| 4399 | migpt（open-xiaoai） | 局域网（默认 0.0.0.0） | 音箱端 client 的 WebSocket 连接；可选 allowlist + 共享 secret 认证（XIAOAI_WS_ALLOWLIST / XIAOAI_WS_SECRET，未配置时完全兼容旧 client 并警告） |
| 4398 | migpt（HTTP 端点） | 127.0.0.1 | /exec（音箱 shell，限本机）、/play（推送播报）、/play_url（音频播放）、/native（原生执行通道）；全部要求 `Bearer <bridge.secret>` |
| 4397 | migpt（healthz） | 0.0.0.0 | /healthz 纯 HTTP 健康检查，音箱端直连模式探测 Mac 存活 |
| 4378 | web-audio-play relay | 0.0.0.0 | 防盗链音频流式转发（带 Referer/UA，零落盘，只代理公网流，流地址带随机访问令牌），30 分钟无请求自动退出；音箱经 `http://<Mac-IP>:4378/s/<token>` 拉流 |
| 8123 | Home Assistant | 127.0.0.1 | HA REST API / WebSocket |
| 8390 | admin/server.py | 127.0.0.1 | 本地配置后台（读写 config/local.json，生成派生文件；要求 X-Admin-Token） |

## 3. 音箱端拦截与双模式

### 3.1 正常模式（Mac 在线）：官方全禁

- **核心机制**：`native-block.sh` 监听 `/tmp/mico_aivs_lab/instruction.log` 的
  `RecognizeResult`（`is_final:true`），每轮语音拿到最终文本后**立即**
  `restart-aivs.sh`（带 LD_PRELOAD 钩子链 hook_final.so:hook_tts.so 的干净重启）
  杀掉官方进程。官方云端下发的 TTS/媒体/执行指令全部进不来——**官方永远不发声、
  不执行**。
- ASR 结果在杀之前已写入 instruction.log，migpt/本地 AI 链路不受影响；
  官方重启后自动重连云端，下一轮语音识别照常（用户说话间隔远大于重连时间）。
- **官方 TTS 零竞态拦截**：hook_tts.so 在官方写 Speak 指令的 write/writev 瞬间
  杀光所有 mediaplayer（官方 TTS 唯一播放者；本地 AI 的 TTS/音乐走 miplayer，
  杀 mediaplayer 零影响）。判别三态：官方版权失败话术 LEAK-KILL、本地作答中
  （`/tmp/xdf_our_pending` 15s 时间窗）PEND-KILL、闹钟/音量官方独占确认 PASS。
- **官方补发响应兜底**：`kill_official_execution` 在官方执行指令
  （Speak/StartAnswer/Play/LOOP_MODE/Execute/wangyiyun…）落盘瞬间掐执行部件，
  **绝不重启官方进程**（重启只会诱发云端再次补发，形成死循环）：
  Speak/StartAnswer → kill_tts_chain（僵尸进程感知的 mediaplayer 延时补重启）；
  媒体指令 → 停 mediaplayer + 重启 quickplayer；Execute → restart_aivs 拦 IR。
- `kill_official_leftovers` 同步掐官方残留（点歌类媒体指令云端响应极快，
  可能抢先下发）：停 mediaplayer / 杀 miplayer / 重启 quickplayer。
- **唤醒音「在呢」不受影响**：它在 RecognizeResult 之前由本地链路触发。
- **ASR 仍依赖官方云端**（语音云 FLAC 上传）：完全断网 = 音箱聋。
  所以方案是「杀进程」而不是「断网」。
- 设备指令由本地 AI 通过 HA（MIoTLan 本地直连）执行，与官方零竞态。

### 3.2 直连模式（Mac 失联）：音箱自主兜底

- `direct-mode.sh` 每 5 秒 `GET http://<Mac>:4397/healthz` 探测 Mac；
  **连续 3 次失败** → 创建 `/tmp/direct_mode` 标志；**连续 2 次成功** → 摘除标志。
  （连续计数防网络抖动误判；必须探测 4397 纯 HTTP，禁止探测 4399 WS——见 FAQ。）
- 直连模式下 `native-block.sh` 分支：
  - 媒体/闹钟/音量等（EXCEPT 词）→ 放行官方；
  - 设备指令（DEVICE 词）→ **放行官方云端独占执行**（Mac 挂时官方云端是唯一
    设备控制通道；此时绝不能杀官方，否则设备双执行）；
  - 其余问答 → `restart_aivs` 杀官方抢答 + 直连大模型（config.env 配置，
    OpenAI 兼容）+ 本地 TTS 播报（ubus mibrain text_to_speech）。
- 音箱端大模型配置来自 `config/generated/speaker/config.env`（后台生成部署），
  降级提示词来自 `config/generated/speaker/system_prompt.txt`。

### 3.3 音箱端脚本运行保障

- `init.sh` 开机初始化：提示音静音（保留 wakeup 唤醒音）、TTS 音色固定、
  启动 native-block/direct-mode、注入 hook、拉起 open-xiaoai client。
- 后台进程一律 `( cmd & )` 双括号脱离，否则 exec shell 退出即死。
- 单实例守卫：锚定 `pgrep -f '/data/open-xiaoai/native-block\.sh$'` + pidfile
  兜底（busybox ash 管道循环子 shell 会继承主进程命令行，非锚定会误杀自己或漏杀）。
- `restart-aivs.sh` 是**唯一**允许的重启方式：init.d restart 会把 LD_PRELOAD
  注入的环境冲掉（procd respawn 重置 env），导致拦截链失效。

## 4. 意图框架（桥侧路由大脑）

migpt 不再做任何路由/意图判断（关键词表全部退役）。所有文本发桥，由
`classify_intent` 把用户每句话分类到意图体系，路由由框架的
`INTENT_TAXONOMY` **确定性决定**（模型的 route 建议只记录不采用——防漂移）。

11 个意图域（40+ 意图）：

| 域 | 典型意图 | 路由 |
|----|---------|------|
| device_control | 开关设备 / 调节参数 / 查状态 / 场景联动 | flash_tools（HA 工具） |
| media | 点歌 / 个人歌单 / 讲故事 / 电台 / 播放控制 / 特殊音频 / 白噪音 | flash_tools（本地工具），play_audio_resource → deep |
| reminder | 设闹钟 / 设提醒 / 倒计时 / 查闹钟 / 取消 | flash_tools（本地提醒队列） |
| query_time | 现在几点 / 今天几号 | flash_tools（get_now_time） |
| weather | 天气/气温/下雨 | flash_tools（get_weather） |
| knowledge | 百科 / 一般问答 | flash |
| realtime | 新闻 / 路况 / 行情 / 比分 / 价格 | doubao（豆包快速搜索）；快递物流/游戏更新 → deep |
| chitchat | 打招呼 / 告别 / 自我介绍 / 心情 / 闲聊 | flash（farewell 强制对话 end） |
| dialogue_mgmt | 确认 / 否定 / 闭嘴 / 取消 / 再说一遍 / 继续说 | flash；stop_interrupt → native_instant |
| deep_task | 分析方案 / 研究 / 文件操作 / 写作代码 / 多步任务 | deep |
| fallback | 无法分类 | flash（保守直答） |

分类失败时返回保守 fallback（flash 直答），对话状态缺省 end。

**确定性短路**：先于所有路由判断。红外设备（空调）无状态回读，多步枚举
（扫地机清扫模式）模型绕圈必翻车——这类高频确定性指令由桥侧正则直连 HA
一次完成，根本不进模型：空调（温度/模式/风速/开关机）、塔扇（风速/摇头/
风模式/定时/夹角）、扫地机（清扫模式/启停/回充）、摄像头（开关）。查询句
（「关了吗」「还有多少电」）有护栏不会误触发。短路用的设备实体来自
`config/devices`，留空时启动自动从 HA 发现（见 config.md §1.6）。

**「深」升级协议**：快通道回答若以「深」收尾（单字或叙述最后一行），桥把
整段吞掉不播报，自动转后台深通道，先播垫场语（「这个问题让我好好想想……」）。

## 5. 通道与容灾（三级降级）

### 5.1 快通道（flash / flash_tools）

桥内直连大模型快模型（不思考，实测 1-5 秒），OpenAI function calling 带
hass-mcp 全量工具 + 桥内置工具（见 §7）。流程：垫场词 → 工具调用循环
（最多 6 轮）→ 结论播报。工具轮后回答混入「自问自答/思考」叙述时由 Flash
润色成纯结论（polish_for_speech）。

### 5.2 深通道（deep）

`dsh --profile headless`（深模型深度推理，带工具），用于复杂分析、联网研究、
文件操作、创作、多步任务。特性：

- **DSH_HOME 隔离**：`DSH_HOME=<speaker_dsh_home>`（默认 `~/.dsh-speaker`），
  音箱发起的会话不进用户主 DSH 的会话列表，记忆全存隔离 home。
- **cwd = 音箱项目文件夹**（speaker_workspace）：对话历史
  （speaker-history.jsonl）、话题档案（speaker-topics.json）、project 记忆
  落地于此。
- **话题连续性**：新问题先由快模型（topic_choose）判定话题归属——相关则续聊
  并注入最近 3 轮问答上下文，无关则新建；完成后 update_topic 追加并重写摘要。
- **进度播报**：深任务可能跑 1-2 分钟，45 秒定时让音箱说一句「还在处理」，
  不叫用户干等。
- **结果润色**：深通道结论经前台 Flash 润色成口语短句再播报
  （polish_for_speech，含对话状态判断）。
- **运行前提**：SPEAKER_HOME/node_modules 符号链接到 checkout 的
  node_modules，环境变量 TSX_TSCONFIG_PATH 指向 checkout/tsconfig.json
  （否则 tsx 解析 @deepseek-ai/* 失败）。

### 5.3 豆包通道（doubao）

国内实时资讯（新闻/行情/路况/比分/价格）走豆包快速搜索（doubao-ask CLI），
约 10-15 秒，比深通道快 4 倍以上。CLI 内置 6-14 秒节流与风控保护（全局
状态文件，与主 DSH 共享节流额度）。失败/风控自动降级深通道（后台 push）。

### 5.4 三级容灾（挂一层降一级）

```
第 1 级  桥内 DSH 挂
         └─▶ 快模型纯问答兜底（ask_llm_plain，无工具）
             诚实告知「后台大脑离线了，这类事情请稍后再试」，绝不编造

第 2 级  桥（8322）探测失败
         └─▶ migpt 原生 fetch 直连大模型播报（askLlmDirect）
             健康缓存 10s / 故障缓存 3s，桥恢复后自动切回（不重启 migpt）
             注意：engine.askAI 内部吞连接错误（播「出错了」不抛异常），
             必须先探测桥健康再决定调用，try-catch 捕不到

第 3 级  Mac 整个失联
         └─▶ 音箱端直连模式（/tmp/direct_mode）
             问答直连大模型 + 本地 TTS；设备指令放行官方云端独占执行
```

三层降级的提示词都明确告知能力受限（系统提示词 + 能力受限说明），
让用户清楚当前处于降级模式。

### 5.5 自我认知

桥侧 `_brain_state`（full/degraded）由 ask_dsh 成功/失败驱动，
`SELF_AWARENESS_TEXT()` 注入快通道提示词，让音箱知道自己当前处于
全功能还是降级状态。

## 6. 播报互斥（speaker-gate）

同一时刻音箱只能有一个声音。`speaker-gate.ts` 三条规则：

1. **流式回答屏障**：AI 流式回答的 chunk 必须连续播完（enqueueChunk）——
   深通道推送 / 唤醒 / 音乐等「回答外」播报在回答进行中挂起
   （pendingAfterAnswer），回答结束（endAnswer）后按到达顺序继续。
   根治病根：推送插在 chunk 之间 → 「回答了一个字就被抢答」。
2. **对话代际（epoch）**：每轮新对话 +1，旧对话排队中的播报段轮到它时发现
   代际过期 → 直接丢弃（用户插话后不再冒出旧回答的尾巴）。「闭嘴」时
   flushPlayQueue 作废全部排队播报。
3. **音乐链独立（musicEpoch）**：音乐（miplayer 拉流）与 TTS 分链——
   新歌顶旧歌，但放歌不打断 TTS 播报；AI 开始说话（新对话）时停音乐让位。

对话控制：桥流式末尾下发 `<<dialogue:keep_open|end>>` 标记，migpt 解析执行
（keep_open = 静默唤醒保持麦克风一轮；缺标记/失败 = end——安全默认，
绝不出现「一直听」）。意图 farewell/stop_interrupt/cancel/deny 强制 end。

播报放行协议：桥对 media/reminder 域下发 `<<native_passthrough>>`，migpt
收到后不播报，官方小爱自己应答。

## 7. 工具层

### 7.1 桥内置工具（快通道直接调用）

- **HA 全量工具**：hass-mcp（8321，Streamable HTTP，MCP 会话复用）
  ——get_entity / entity_action / list_entities / search_entities_tool 等，
  设备指令首选 entity_action（本地直连、状态可回读验证）。
- **get_weather**：HA 天气实体（含未来几天预报），直连 HA REST。
- **get_now_time**：本机时间/日期/星期（模型不自编时间）。
- **电脑文件三件套（只读）**：list_computer_files / read_computer_file /
  search_computer_files——限制在主目录内、黑名单屏蔽敏感目录
  （.ssh/.dsh/钥匙串等），搜索默认只搜桌面/下载/文稿等常用目录。
- **音量工具**：set_speaker_volume / get_speaker_volume（HA
  media_player 实体，0-100 直调；没指明哪台就调正在说话的这台）。
- **native_device_command**：走音箱原生日志通道（migpt :4398/native →
  音箱 ubus mibrain ai_service nlp_text），1-2 秒完成，适合单句设备控制；
  首选仍是 HA entity_action。
- **web_audio_play**：网络音频在线播放（B 站适配器 + 通用浏览器捕获兜底 +
  防盗链 relay 4378）。B 站必须用 `fnval=0&quality=32` 渐进式完整 mp4
  （fnval=16 的 DASH .m4s 裸分片 miplayer/VLC 播不了、永远缓冲），且一律走
  Mac relay（直链探针 Range 200 会误判、miplayer 实拉挂起）。点歌降级链：
  网易云 → web_audio_play → 深（netease_music_play 工具内部自动降级 B 站，
  一次工具调用完成点歌）。工具找到音乐后流式垫「找到了，马上放。」覆盖
  回答生成期的静音空档。
- **speaker_music_control**：暂停/继续（桥记 last_music_url 重播）。
- **网易云 4 工具**：netease_music_play（点歌正版快链）/ netease_music_personal
  （每日推荐/红心/歌单）/ netease_music_playlist（歌单名匹配）/
  netease_music_lyric（LRC 剥时间戳）。播放链：search → url(--level lossless)
  → POST 127.0.0.1:4398/play_url。反封号：CLI 内置 ≥5s 节流 + 桥侧缓存 +
  禁止 NETEASE_NO_WAIT。
- **提醒队列 3 工具**：reminder_set / reminder_list / reminder_cancel——本地
  JSON 队列（speaker-reminders.json）+ 10 秒线程到点 push 播报
  （官方小爱已全禁，闹钟/提醒由桥侧接管）。
- **确定性短路四件套**：`_ac_shortcut`（空调温度 16-30/模式/风速±/开关机）、
  `_fan_shortcut`（塔扇定时/夹角/摇头/风模式/风速）、`_vacuum_shortcut`
  （清扫模式/启停/回充）、`_camera_shortcut`（摄像头开关）——正则直连 HA，
  先于所有路由；查询句有护栏，绝不误触发。
- **设备自动发现**：`_discover_devices()` 在桥启动时扫描 HA 实体，把
  `config/devices` 留空的实体按米家命名规律自动填上（只填空缺、不覆盖
  已配置的），发现结果打印日志；快通道提示词的设备规则按发现结果渲染。

### 7.2 播放时序铁律

工具阶段（netease_music_play / web_audio_play 等）**绝不直接播放**，只登记
`_pending_play(url, title)` 并返回「已找到：X」；播放统一由
`_flush_pending_play()` 在 AI 回答播报完成后推送（speaker-gate 的
pendingAfterAnswer 保证挂起到 endAnswer 后播）——否则「AI 说话停音乐」
会把刚响的音乐打断，表现为「一会有一会没」。web-audio-play 必须用
--no-play 配合（只搜索拿 URL 不 POST migpt）。

### 7.3 自我进化（技能 + 声明式工具）

深通道解决复杂问题后，把可复用流程沉淀下来：

- **技能沉淀**：回答末尾附 `【技能】…【技能结束】` 块（名称/何时使用/步骤），
  桥解析后写入 `skills/speaker-skills/<名>/SKILL.md`（标准 frontmatter），
  下次深通道按任务匹配注入复用（最多注入 5 个、每个截 800 字）。
- **声明式工具**：`【工具】…【工具结束】` 块（JSON：name/description/steps），
  桥校验后写入 `evolved-tools.json` 并注册进快通道工具列表。安全校验：
  http 步骤只允许本机 HA（path 以 /api/ 开头）；shell 步骤只允许只读命令
  （禁止修改/删除/重启/联网外传类操作）。
- **长期记忆**：深通道注入 MEMORY_INSTRUCTION（memory 工具，Memory Evolve
  reviewMode=auto 直接落盘；先查自己的记忆，查不到再只读用户主记忆）。
- **话题档案清理**：daily_cleanup（启动 + 每天一次）删除 7 天未活跃话题档案
  与 headless 会话文件，防爆炸；长期记忆不随会话清理丢失。

## 8. 体验设施（桥侧）

- **busy 排队 45 秒**：用户连问两个问题（打断/追问）时排队而非拒绝，等
  上一个请求最多 45 秒，等不到才 503。
- **重复问题去重**：5 分钟内同一问题再问直接复答上次答案（带「刚才说过」
  提示）；状态查询句（结果会变）不去重。
- **ASR 容错**：同音词修正表（如「冰箱 → 小爱」）+ 截断句上下文猜测
  （「打开/帮我把/调到」等结尾 = 说了一半，提示模型结合上下文直接执行
  最合理的那个，不反问）。
- **filler 垫场**：意图分类后立即出声（六句轮换），不等模型。
- **元叙述清洗**：工具轮后回答混入「让我确认/我应该/不过」等特征 →
  polish_for_speech 润成纯结论。
- **strip_bbcode**：清洗模型偶发的 [size=2] BBCode 杂质。
- **追问机制**：回答结尾含「还是/吗/呢/么/?/？」判定为反问 → 静默唤醒保持
  听音 + 写入 speaker-pending.json（TTL 10 分钟），下一轮 consume_pending
  注入反问全文，让「A」这种回答有上下文。

## 9. 配置与运维

- 唯一事实来源 `config/local.json`（gitignore，永不入库）；模板
  `config/config.example.json`。详见 [config.md](config.md)。
- 本地配置后台 `admin/server.py`（127.0.0.1:8390，纯标准库无依赖），
  读写配置并生成派生文件到 `config/generated/`；自带「测试连接」
  （/models 探测大模型、/api/ 探测 HA）。
- 音箱端运维走 migpt :4398/exec 端点（限 loopback）。
- 单实例注意：migpt 只允许一个实例；DSH 主实例只监听 127.0.0.1:3080，
  不要启动第二个实例。
