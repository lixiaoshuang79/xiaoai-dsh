# xiaoai-dsh 部署指南（deploy.md）

> 前提：音箱已按 `flashing.md` 刷机并具备 root SSH（或至少已完成刷机与音箱端基础部署）。
> 本文覆盖：架构 → 依赖 → 配置后台 → Mac 各组件安装启动 → 音箱端部署 → launchd 常驻 → 降级容灾 → 验证清单。

---

## 1. 架构速览

```
┌────────────────────────────── 音箱端（/data/open-xiaoai/）─────────────────────────────┐
│  唤醒词「小爱同学」（原生）→ 官方云端 ASR 识别出文本                                     │
│                                                                                        │
│  client ──WebSocket──► Mac :4399（migpt 引擎）—— 对话、播报、控制                       │
│  native-block.sh + hook_final.so（LD_PRELOAD）—— 拦截官方云端执行指令                    │
│  direct-mode.sh —— 每 5s 探测 Mac:4397/healthz，Mac 失联 → 直连模式                     │
│  config.env / system_prompt.txt —— 直连模式的大模型配置与降级提示词                      │
│  本地 TTS（mibrain，男声音色 XiaoMi_M88）                                               │
└──────────────────────────────────────────────────────────────────────────────────────────┘
        │ 4399 WebSocket（音箱→Mac）
        ▼
┌────────────────────────────── Mac 端（本机大脑）──────────────────────────────┐
│  xiaogpt-bridge.py   :8322  OpenAI 兼容端点（唯一大脑入口，127.0.0.1）        │
│    ├─ 快通道：快模型直答（带 hass-mcp 工具循环）                              │
│    ├─ 深通道：DSH headless 深模型 + 技能 + 记忆（可选，需 DeepSeek Harness）  │
│    └─ 兜底：DSH 挂 → 直连大模型纯问答                                          │
│  migpt 引擎（pnpm start，基于 open-xiaoai 的 examples/migpt）                 │
│    ├─ :4399 WebSocket（音箱 client 接入，0.0.0.0）                             │
│    ├─ :4398 HTTP（/play /play_url /exec /native，仅 127.0.0.1）               │
│    └─ :4397 /healthz（音箱探测 Mac 存活，0.0.0.0）                             │
│  hass-mcp        :8321  HA 工具 MCP 服务器（127.0.0.1）                       │
│  admin 配置后台  :8390  python3 admin/server.py（127.0.0.1）                  │
│  web-audio 转发  :4378  网络音频流式转发（音箱拉流）                           │
│  xiaogpt（可选）：上游 ASR 桥，纯文字收发通道                                  │
└────────────────────────────────────────────────────────────────────────────────┘
        │ HA REST API（Bearer Token 认证）
        ▼
┌────────────────────────────── Home Assistant :8123 ──────────────────────────────┐
│  xiaomi_home 集成（MIoTLan 局域网直连）→ 家里的灯 / 空调 / 插座等设备             │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**一句话流程**：用户说「小爱同学，X」→ 音箱识别文本 → client 经 4399 送到 migpt → migpt 把文本交给本地桥（8322）→ 桥按意图路由（快通道 / 深通道 / 放行官方 / 直连兜底）→ 回答文本回 migpt 逐段播报 → 设备指令经 hass-mcp 走 HA 本地执行。

**端口地图**：

| 端口 | 服务 | 监听 | 用途 |
|---|---|---|---|
| 8322 | xiaogpt-bridge | 127.0.0.1 | OpenAI 兼容端点（大脑入口） |
| 8321 | hass-mcp | 127.0.0.1 | HA 工具 MCP 服务器 |
| 4399 | migpt WS | 0.0.0.0 | 音箱 client 接入（WebSocket） |
| 4398 | migpt HTTP | 127.0.0.1 | /exec /play /play_url /native |
| 4397 | migpt /healthz | 0.0.0.0 | 音箱探测 Mac 存活 |
| 4378 | web-audio 转发 | Mac 本机 | 防盗链音频流式转发 |
| 8123 | Home Assistant | 按 HA 配置 | 设备控制中枢 |
| 8390 | admin 配置后台 | 127.0.0.1 | 统一配置入口 |

---

## 2. 前置依赖清单

| 依赖 | 版本/说明 | 用途 |
|---|---|---|
| macOS | 任意（Apple Silicon / Intel 均可） | 本机大脑运行平台 |
| Python | 3.10+ | 桥、配置后台（纯标准库；辅助脚本需 aiohttp / cryptography / playwright） |
| Node.js | ≥ 20 | migpt 引擎（使用原生 fetch，16/18 无法运行） |
| pnpm | 9.x（仓库 packageManager 为 pnpm@9.15.9） | migpt 依赖管理 |
| Rust 工具链（cargo） | 需要 Xcode Command Line Tools（含 clang） | 编译 migpt 的 Rust neon 插件 open-xiaoai.node |
| OpenAI 兼容大模型 API | 快模型 + 深模型两个模型名 | 回答大脑；支持 reasoning 更佳 |
| Home Assistant | URL + 长期访问令牌 | 设备控制中枢（与 Mac 同机或局域网可达） |
| 小米账号 | 手机号/邮箱 + 密码（+ 设备 ID 可选） | xiaogpt ASR 桥（可选组件） |
| 已刷机音箱 | 见 flashing.md | 与 Mac 同一局域网；建议路由器静态绑定 IP |
| DeepSeek Harness 检出（可选） | git clone + pnpm install 即可用 | 深通道（深度推理/技能/记忆）；**没有也能跑** |
| 可选 CLI | netease-music / doubao-ask / ego-browser | 点歌、实时查询、网络音频技能 |

---

## 3. 后台配置（localhost 配置后台）

### 3.1 启动

```bash
cd <仓库路径>
python3 admin/server.py
# 输出：xiaoai-dsh 配置后台已启动： http://127.0.0.1:8390
```

浏览器打开 <http://127.0.0.1:8390>。首次运行读取示例配置（`config/config.example.json`）；点「保存」时写入 `config/local.json`（权限 600，已被 .gitignore 排除）并生成全部派生文件到 `config/generated/`。

### 3.2 逐项填写

| 分组 | 字段 | 说明 |
|---|---|---|
| 大模型 | API 地址（Base URL） | OpenAI 兼容端点，填你自己的，如 `https://api.example.com/v1` |
| | API Key | 你自己的密钥（`sk-...` 或网关 key） |
| | 快速模型 | 日常回答模型（快） |
| | 深度模型 | 复杂任务模型（强，支持推理更佳） |
| | 系统提示词 | 人设与铁律（小爱身份、口语化、称呼先生等），会注入快/深/降级各通道 |
| | 推理支持 | 模型支持 reasoning 才勾选；影响生成的 DSH 配置（reasoningEfforts 段） |
| Home Assistant | 地址 | 如 `http://127.0.0.1:8123` |
| | 长期访问令牌 | HA「个人资料 → 安全 → 长期访问令牌」里创建（`eyJ...`） |
| 小米账号 | 用户名 / 密码 | xiaogpt ASR 桥用（不用 xiaogpt 可留空） |
| | 设备 ID | 米家 App 设备信息里查；可留空由系统自动发现 |
| 音箱 | IP | 音箱局域网 IP |
| | miot DID | 米家 App 设备信息里查 |
| | TTS 音色 | 默认 XiaoMi_M88（男声，青葱） |
| 电脑 | 局域网 IP | 本机 IP；写入音箱端 server.txt 并用于存活探测 |
| 设备实体 | 主灯/氛围灯/音箱音量/空调（温度/模式/开关/风速）/塔扇/摄像头/扫地机器人等 17 个 | 全部可留空——桥启动时自动扫描 HA 按命名规律匹配，发现结果在桥日志里；匹配不对再手填（实体 ID 在 HA「开发者工具 → 状态」里查） |
| 路径 | node 可执行文件 | 深通道调用 dsh CLI 需要，写绝对路径（`which node` 查） |
| | DeepSeek Harness 检出目录 | 深通道必需（`apps/cli/src/bin.ts` 所在检出） |
| | 音箱工作目录 | 音箱历史/话题档案/项目记忆存放处（深通道 cwd） |
| | 音箱独立 DSH_HOME | 隔离的 DSH home（如 `~/.dsh-speaker`），音箱会话不进你的主 DSH |
| | netease-music / doubao-ask / ego-browser | 可选技能 CLI 路径 |

> 页面自带「验证」按钮：LLM 验证请求 `{base}/models`（404 视为端点可达），HA 验证请求 `{base}/api/`（200 即通）。

### 3.3 保存后生成的派生文件（config/generated/）

| 文件 | 内容 | 用途 |
|---|---|---|
| `bridge/xiaogpt-credentials` | 小米账号（600） | run-xiaogpt.sh 注入环境变量 |
| `bridge/xiaogpt-config.yml` | xiaogpt 配置（指向 127.0.0.1:8322） | ASR 桥 |
| `bridge/.env` | HA_URL / HA_TOKEN（600） | run-mcp.sh 读取 |
| `speaker/config.env` | 大模型直连配置 + MAC_IP（600） | 部署到音箱 |
| `speaker/system_prompt.txt` | 降级提示词（自动追加「后台大脑离线」说明） | 部署到音箱 |
| `dsh-speaker/settings.yaml` | 音箱 DSH 主配置（深模型/快模型/供应商） | 装进音箱 DSH_HOME |
| `dsh-speaker/dsh-fast.patch.yml` | 快通道模型补丁 | 桥直接引用 |
| `dsh-speaker/cordis.patch.yml` | 音箱专属覆盖层（storage/workspace + Memory Evolve） | 装进音箱 DSH_HOME |
| `dsh-speaker/.credentials.yaml` | LLM_API_KEY（600） | 装进音箱 DSH_HOME |

---

## 4. Mac 各组件安装启动

### 4.1 桥依赖（.venv）

```bash
cd <仓库路径>/bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install hass-mcp
```

### 4.2 xiaogpt ASR 桥依赖（可选，独立 venv）

```bash
cd <仓库路径>/bridge
python3 -m venv .venv-xiaogpt
.venv-xiaogpt/bin/pip install xiaogpt
```

### 4.3 复制生成的桥配置

`run-xiaogpt.sh` / `run-mcp.sh` 会**先找 bridge/ 下的文件、再找 ../config/generated/bridge/ 下的**，所以不复制也能跑；复制后两边互不影响：

```bash
cd <仓库路径>
cp config/generated/bridge/* bridge/
```

### 4.4 启动桥与 hass-mcp（前台测试）

```bash
cd <仓库路径>/bridge
.venv/bin/python xiaogpt-bridge.py --port 8322   # OpenAI 兼容端点
./run-mcp.sh                                      # hass-mcp :8321（读 .env）
```

### 4.5 migpt 引擎

```bash
cd <仓库路径>/migpt
pnpm install
pnpm build    # 编译 Rust neon 插件 → migpt/open-xiaoai.node
pnpm start    # 4399 WS / 4398 HTTP / 4397 healthz
```

> **连续对话（免唤醒词续聊）**：AI 播报完音箱自动开麦，用户直接接话/打断（实测
> 「问天气 → 那后天呢 → 那大后天呢」逐轮承接）。三要素缺一不可：
>
> 1. **官方进程保持在线**（`speaker/native-block.sh` 已内置：普通问答不杀官方，
>    官方发声/执行由 hook 链拦截——这是听音窗口存在的前提）；
> 2. **固件连续对话开关**（`speaker/init.sh` 已内置：开机写
>    `/data/mipns/dialog_continuous=on`）；
> 3. **migpt 播报完主动静默唤醒**：在 open-xiaoai 的 `examples/migpt/config.ts`
>    流式回答结束处（`endAnswer()` 之后）调用 `await sleep(500); await enqueueWakeUp(epoch);`
>    替换原来「仅 `keep_open` 标记才唤醒」的分支——每次播报完都唤醒，静默无提示音，
>    用户不开口则官方听音窗口到期自动退出。桥侧 `record_pending/consume_pending`
>    已支持「那后天呢」式承接短句自动补全话题（topic_state 模块）。

### 4.6 深通道（可选但推荐）

深通道 = 音箱的 DSH（DeepSeek Harness）headless 会话，负责复杂任务、工具调用、长期记忆。三个相关配置项：

- **`paths.dsh_checkout`**：DSH 检出目录（`apps/cli/src/bin.ts` 所在）。检出后 `pnpm install` 即可；
- **`paths.speaker_workspace`**：音箱工作目录。深通道以它为 cwd 运行（project 记忆落地于此）。需要把 DSH 检出的 node_modules 符号链接进来，否则 tsx 解析 `@deepseek-ai/*` 失败：

```bash
mkdir -p <音箱工作目录>
ln -s <dsh_checkout>/node_modules <音箱工作目录>/node_modules
```

- **`paths.speaker_dsh_home`**：隔离的 DSH home（如 `~/.dsh-speaker`）。首次部署把后台生成的配置装进去：

```bash
mkdir -p ~/.dsh-speaker
cp <仓库路径>/config/generated/dsh-speaker/settings.yaml ~/.dsh-speaker/
cp <仓库路径>/config/generated/dsh-speaker/cordis.patch.yml ~/.dsh-speaker/
cp <仓库路径>/config/generated/dsh-speaker/.credentials.yaml ~/.dsh-speaker/
chmod 600 ~/.dsh-speaker/.credentials.yaml   # 必须 600 且为实体文件（符号链接会导致鉴权失败）
```

> `dsh-fast.patch.yml` 不需要复制：桥直接从 `config/generated/dsh-speaker/` 引用。

**没有 DSH 时音箱仍然可用**：快通道（桥直连大模型）+ migpt 直连兜底 + 音箱端直连兜底照常工作，只是失去深度推理、技能库与长期记忆。

### 4.7 xiaogpt ASR 桥（可选）

```bash
cd <仓库路径>/bridge && ./run-xiaogpt.sh
```

（自动从 `xiaogpt-credentials` 注入小米账号；令牌缺失/损坏时静默刷新；以 `xiaogpt-config.yml` 连本地桥。）

---

## 5. 音箱端部署

刷机后的初始部署见 `flashing.md` §3；本节是从后台生成文件出发的部署流程。

```bash
cd <仓库路径>

# 1. 上传音箱端文件（client 来自上游 releases，约 900KB；silent.wav 生成方法见 flashing.md §3.3）
scp config/generated/speaker/config.env root@<音箱IP>:/data/open-xiaoai/
scp config/generated/speaker/system_prompt.txt root@<音箱IP>:/data/open-xiaoai/
scp speaker/native-block.sh speaker/direct-mode.sh speaker/restart-aivs.sh root@<音箱IP>:/data/open-xiaoai/
scp speaker/hook/hook_final.so root@<音箱IP>:/data/open-xiaoai/
scp speaker/init.sh root@<音箱IP>:/data/init.sh
printf '%s\n' "<Mac局域网IP>" > server.txt
scp server.txt root@<音箱IP>:/data/open-xiaoai/

# 2. SSH 到音箱：权限 + init 钩子 + 立即生效
ssh root@<音箱IP>
chmod +x /data/open-xiaoai/client /data/open-xiaoai/*.sh /data/init.sh
cat /etc/rc.local        # 确认有 /data/init.sh 的执行行；没有就手动加：/data/init.sh &
sh /data/init.sh         # 立即生效（或 reboot）
pgrep -f client && tail -n 20 /data/open-xiaoai/client.log
```

> 记住 flashing.md 的坑：音箱端无 `base64`/`od`，文件一律 scp；后台进程必须 `( cmd & )` 包裹；heredoc 会多一个尾随空行（优先 scp）。

---

## 6. launchd 常驻模板

两个 LaunchAgent 分别守护 migpt 与桥（`KeepAlive` 崩溃自动拉起、`RunAtLoad` 登录自启）。放到 `~/Library/LaunchAgents/`，文件名与 Label 一致（`<你的域名>` 换成你自己的反向域名，如 `com.example`）。

### 6.1 com.<你的域名>.xiaogpt-migpt.plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.<你的域名>.xiaogpt-migpt</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>pnpm start</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/<你的路径>/xiaoai-dsh/migpt</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/<你的路径>/xiaoai-dsh/logs/migpt.log</string>
  <key>StandardErrorPath</key>
  <string>/<你的路径>/xiaoai-dsh/logs/migpt.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

### 6.2 com.<你的域名>.xiaogpt-bridge.plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.<你的域名>.xiaogpt-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/<你的路径>/xiaoai-dsh/bridge/.venv/bin/python</string>
    <string>/<你的路径>/xiaoai-dsh/bridge/xiaogpt-bridge.py</string>
    <string>--port</string>
    <string>8322</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/<你的路径>/xiaoai-dsh/bridge</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/<你的路径>/xiaoai-dsh/logs/bridge.log</string>
  <key>StandardErrorPath</key>
  <string>/<你的路径>/xiaoai-dsh/logs/bridge.err.log</string>
</dict>
</plist>
```

### 6.3 加载与检查

```bash
# 加载（gui 域，当前用户）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<你的域名>.xiaogpt-migpt.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<你的域名>.xiaogpt-bridge.plist

# 检查
launchctl list | grep xiaogpt

# 卸载（改配置后重载）
launchctl bootout gui/$(id -u)/com.<你的域名>.xiaogpt-migpt
launchctl bootout gui/$(id -u)/com.<你的域名>.xiaogpt-bridge
```

要点：

- `WorkingDirectory` 必须指向组件目录（migpt 的 `pnpm start` 依赖 cwd）；
- launchd 的 PATH 精简且不含 `~/.local/bin`：桥内 node / 各 CLI 路径都从 `config/local.json` 的 `paths` 段读取绝对路径，无需在 plist 里配环境变量；若你的 pnpm 不在 `/opt/homebrew/bin` 或 `/usr/local/bin`，用绝对路径（`which pnpm` 查）替换 ProgramArguments；
- 桥与 migpt 都是单实例进程：**不要**同时手动启动与 launchd 守护，否则端口冲突（EADDRINUSE）；
- hass-mcp / xiaogpt 若也要常驻，照此模式再写两个 plist；
- 作为音箱大脑的 Mac 建议保持不休眠（如 `sudo pmset -a disablesleep 1`，或使用 caffeinate），否则合盖后音箱会进入直连降级模式。

---

## 7. 降级容灾说明

三级降级，保证「音箱永远有响应」：

1. **正常模式（全栈在线）**：用户说话 → 音箱 ASR → migpt → 桥（快/深通道）→ 回答播报；设备指令由桥经 hass-mcp → HA 本地执行；音箱端 hook/native-block 锁定官方云端执行链，官方不发声、不执行。
2. **Mac 桥挂（migpt 存活）**：migpt 探测 8322 失败（`isBridgeHealthy`，健康缓存 10s / 故障缓存 3s）→ **直连你配置的大模型**（原生 fetch，OpenAI 兼容）播报。无工具、无深度，基础问答可用；桥恢复后自动切回。
3. **Mac 全挂**：音箱端 `direct-mode.sh` 每 5s 探测 `http://<MacIP>:4397/healthz`，**连续 3 次失败** → 置 `/tmp/direct_mode` 进入直连模式。`native-block.sh` 在该模式下：
   - 问答类 → 杀官方进程 + 直连大模型（`config.env`）+ 本地 TTS 播报；
   - 设备指令 → **放行官方小爱云端执行**（Mac 挂时官方云端是唯一设备控制通道）；
   - **连续 2 次探测成功** → 摘掉标志，自动切回正常模式。
4. **连大模型也失败**：直连模式播报「云端大模型暂时也连不上」。

要点：

- 存活探测必须用 **4397 纯 HTTP 健康端点**（0.0.0.0 监听，返回 `{"ok":true}`）；**不要**探测 4399 WS 端口（curl 退出码不稳定，曾导致误判直连模式、设备双执行）；
- 切换有连续计数防抖动（失败×3 切直连、成功×2 切回）；
- 直连模式下设备指令由官方独占执行，AI 侧已无工具，不会双执行。

---

## 8. 验证清单

假设所有组件已按上文启动。逐端口验证（Mac 上执行）：

| 端口 | 服务 | 验证命令 | 正常表现 |
|---|---|---|---|
| 8390 | admin 后台 | `curl -s http://127.0.0.1:8390/api/config` | 返回 JSON（含 `_is_example` 等字段） |
| 8322 | 桥 | `curl -s http://127.0.0.1:8322/v1/models` | HTTP 200 + 模型列表 JSON（/v1/models 无鉴权；chat/completions 需 Bearer <bridge.secret>） |
| 8321 | hass-mcp | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8321/mcp` | 返回非 000（4xx/5xx 也算服务在听） |
| 4397 | migpt healthz | `curl -s http://127.0.0.1:4397/healthz` | `{"ok":true}` |
| 4398 | migpt /play | `curl -s -X POST http://127.0.0.1:4398/play -H "Authorization: Bearer <bridge.secret>" -H 'Content-Type: application/json' -d '{"text":"测试播报"}'` | `{"ok":true}` 且音箱播报 |
| 4398 | migpt /exec | `curl -s -X POST http://127.0.0.1:4398/exec -H "Authorization: Bearer <bridge.secret>" -H 'Content-Type: application/json' -d '{"cmd":"echo ok"}'` | `{"ok":true,...}`（在音箱上执行成功） |
| 4399 | migpt WS | 音箱 client 连接后看 migpt 日志 | `nc -z 127.0.0.1 4399` 通 |
| 4378 | web-audio 转发 | 点歌触发后 `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4378/ping` | 200（无播放时可能不监听，正常） |
| 8123 | HA | `curl -s -H "Authorization: Bearer <HA Token>" http://127.0.0.1:8123/api/` | `{"message":"API running."}` |

音箱端（SSH 到音箱执行）：

```bash
pgrep -f client && pgrep -f native-block && pgrep -f direct-mode
tail -n 5 /data/open-xiaoai/client.log
# 音箱 → Mac 存活探测链路
curl -s http://<Mac局域网IP>:4397/healthz     # 应返回 {"ok":true}
```

端到端冒烟：

1. 对音箱说「小爱同学，现在几点了」→ 应听到本地 AI 回答（非官方小爱）；
2. 说一条设备指令（如「打开主灯」）→ 灯亮且 AI 简短确认；
3. 停掉 migpt（或拔掉 Mac 网线）约 15 秒后，说「小爱同学，1 加 1 等于几」→ 音箱进入直连模式仍能回答（本地 TTS）；
4. 恢复 migpt 约 10 秒后，再问一句 → 自动切回正常模式（回答含完整工具能力）。

---

## 附：日常运维速查

| 操作 | 命令 |
|---|---|
| 改配置 | `python3 admin/server.py` → 浏览器 8390 → 保存（热生效：桥会重新读取 local.json；migpt 需重启） |
| 看 migpt 日志 | `tail -f <你的路径>/xiaoai-dsh/logs/migpt.log` |
| 看桥日志 | `tail -f <你的路径>/xiaoai-dsh/logs/bridge.log` |
| 重启音箱侧拦截 | 音箱上 `sh /data/init.sh`（或 reboot） |
| 在音箱上执行命令 | `curl -s -X POST http://127.0.0.1:4398/exec -H 'Content-Type: application/json' -d '{"cmd":"<命令>"}'` |
