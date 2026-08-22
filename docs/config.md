# xiaoai-dsh 配置说明

整个项目只有一个配置事实来源：**`config/local.json`**（已被 .gitignore
排除，永不入库），模板是 **`config/config.example.json`**。

所有组件（桥、migpt、各工具、音箱端脚本）都从 local.json 读取配置，
不在自己的代码里写死密钥、路径或设备实体。推荐通过本地配置后台修改：

```bash
python3 admin/server.py [--port 8390]
# 浏览器打开 http://127.0.0.1:8390
```

后台保存配置时会自动生成各组件需要的**派生文件**到 `config/generated/`，
并落盘 local.json（权限 0600）。

## 1. 配置字段逐项说明

### 1.1 `llm` — 大模型（三通道共用）

| 字段 | 类型 | 说明 |
|------|------|------|
| `base_url` | string | OpenAI 兼容 API 端点，如 `https://api.deepseek.com/v1`。快通道、深通道、migpt 直连兜底、音箱端直连（降级）共用同一端点 |
| `api_key` | string | API 密钥。只存本机，后台生成到多个派生文件（见 §3） |
| `fast_model` | string | 快模型：快通道直连用（不思考，实测 1-5 秒）。也用于 migpt 直连兜底、音箱端直连降级 |
| `deep_model` | string | 深模型：深通道 DSH headless 用（深度推理） |
| `reasoning_support` | bool | DeepSeek 系 reasoning 参数开关。为 true 时生成的 settings.yaml 带 reasonEfforts / thinkingFormat 兼容段（off/low/high/max），快通道补丁 reasoningEffort=off；为 false 时全部省略 |
| `system_prompt` | string | 系统提示词——用户可**完全自定义人设与铁律**，三通道（快/深/音箱直连）共用。示例预设了「管家小爱，称呼先生」人设 |

### 1.2 `home_assistant` — Home Assistant

| 字段 | 类型 | 说明 |
|------|------|------|
| `url` | string | HA 地址，如 `http://127.0.0.1:8123` |
| `token` | string | 长期访问令牌（Long-Lived Access Token）。只存本机；生成到 bridge/.env（0600），由 hass-mcp 使用 |

### 1.3 `xiaomi_account` — 小米账号（ASR 桥）

| 字段 | 类型 | 说明 |
|------|------|------|
| `username` | string | 小米账号（xiaogpt 上游 ASR 桥用，即 open-xiaoai 的云端 ASR 会话） |
| `password` | string | 小米账号密码。只存本机，生成到 xiaogpt-credentials（0600），由 run-xiaogpt.sh 注入环境变量 MI_USER/MI_PASS；**不写进** xiaogpt-config.yml |
| `device_id` | string | 音箱的 miot DID（跳过 miio 自动发现，写进 xiaogpt-config.yml 的 mi_did） |

### 1.4 `speaker` — 音箱

| 字段 | 类型 | 说明 |
|------|------|------|
| `ip` | string | 音箱局域网 IP（示例 192.168.1.x，按实际网络填写） |
| `did` | string | 音箱 miot DID（同上，写进 xiaogpt-config.yml） |
| `tts_vendor` | string | 音箱 TTS 音色，示例 `XiaoMi_M88`（男声「青葱」）；init.sh 开机固定 |

### 1.5 `mac` — Mac（本机）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ip` | string | Mac 局域网 IP（示例 192.168.1.x，按实际网络填写）。写进音箱端 config.env 的 MAC_IP，直连模式探测用它（4397/healthz） |

### 1.6 `devices` — 关键设备实体（桥的指令铁律引用这些实体）

| 字段 | 类型 | 说明 |
|------|------|------|
| `main_light` | string | 大灯 HA entity_id（**switch 实体**，不是 light 域）。ROUTER_INSTRUCTION 铁律：用户只说「开灯/关灯」没指明哪个灯时，只操作大灯本体，绝不碰氛围灯和任何指示灯 |
| `ambient_light` | string | 氛围灯 entity_id。用户明说「氛围灯」时才操作 |
| `speaker_volume` | string | 正在说话的音箱的 media_player entity_id（音量工具 0-100 直调） |
| `speaker_volume_2` | string | 第二台音量实体（可选，留空则只有一台） |
| `ac_temperature` | string | 空调温度 number 实体（红外空调唯一温度通道）。铁律：调温度只用它 set_value，绝不按温度±按钮，绝不反复查询验证 |
| `ac_turn_on` | string | 空调开机 button 实体（「开空调」= 先 press 它） |

### 1.7 `paths` — 本地路径

| 字段 | 类型 | 说明 |
|------|------|------|
| `node` | string | node 可执行文件路径（桥调 dsh CLI、各 node 工具用；launchd 常驻程序 PATH 不含 ~/.local/bin，要写全路径） |
| `dsh_checkout` | string | DeepSeek Harness 源码目录（深通道 CLI 位置 `apps/cli/src/bin.ts`、tsconfig.json 所在） |
| `speaker_workspace` | string | 音箱项目文件夹：对话历史 speaker-history.jsonl、话题档案 speaker-topics.json、待答复状态、提醒队列、深通道 cwd（project 记忆落地于此）。桥启动时会把 checkout 的 node_modules 符号链接到这里 |
| `speaker_dsh_home` | string | 音箱 DSH 隔离 home（默认 ~/.dsh-speaker）：深通道的 DSH_HOME，记忆全存这里，不与用户主 DSH 混用 |
| `netease_music_cli` | string | netease-music CLI 路径（点歌工具用） |
| `doubao_cli` | string | doubao-ask CLI 路径（豆包快速搜索通道用） |
| `ego_browser` | string | ego-browser CLI 路径（web-audio-play 的 B 站适配器用） |

### 1.8 `optional` — 可选

| 字段 | 类型 | 说明 |
|------|------|------|
| `netease_cookie` | string | 网易云账号 cookie（可选；CLI 首次使用时也会自行配置维护）。留空则点歌走匿名能力 |

## 2. 后台生成逻辑（admin/server.py）

保存配置后生成以下派生文件（相对 config/generated/）：

| 派生文件 | 权限 | 内容与用途 |
|----------|------|-----------|
| `bridge/xiaogpt-credentials` | 0600 | `export MI_USER=…/MI_PASS=…/MI_DEVICE_ID=…`——run-xiaogpt.sh 注入环境变量给 xiaogpt ASR 桥；账号密码不落进 yml |
| `bridge/xiaogpt-config.yml` | 0644 | xiaogpt 配置：hardware=OH2P、mi_did、OpenAI 端点 `http://127.0.0.1:8322/v1`、tts=mi 等 |
| `bridge/.env` | 0600 | `HA_URL=…` + `HA_TOKEN=…`——run-mcp.sh 读取，hass-mcp 使用 |
| `speaker/config.env` | 0600 | `LLM_BASE/LLM_KEY/LLM_MODEL/MAC_IP`——部署到音箱 /data/open-xiaoai/config.env，直连模式（Mac 失联）时音箱独立调大模型用 |
| `speaker/system_prompt.txt` | 0644 | 降级提示词 = `llm.system_prompt` + 能力受限说明（「后台大脑离线，没有设备控制工具、没有深度思考，只能基础问答…不要编造」）。部署到音箱 /data/open-xiaoai/system_prompt.txt |
| `dsh-speaker/settings.yaml` | 0644 | 深通道 DSH 配置：agent-default-model（深模型 + reasoningEffort high）+ llm-pi-ai providers（speaker-llm 段，baseURL/模型表；reasoning_support 时带 DeepSeek 系兼容段） |
| `dsh-speaker/dsh-fast.patch.yml` | 0644 | 快通道模型补丁：agent-default-model 覆盖为 fast_model（reasoning_support 时 reasoningEffort=off） |
| `dsh-speaker/cordis.patch.yml` | 0644 | 深通道服务链补丁：补 headless 树缺失的 storage/workspace 服务链 + 挂载 Memory Evolve（reviewMode: auto，记忆直接落盘） |
| `dsh-speaker/.credentials.yaml` | 0600 | `LLM_API_KEY: <api_key>`——深通道 provider 的凭据文件（必须是实体文件且 0600，符号链接会导致凭据校验失败、深通道整体起不来） |

派生文件由后台用「原文模板 + 占位符替换」生成，**严禁用 yaml.safe_dump
重写**（PyYAML 会把 `off:` 键变 `false: null`，破坏 reasoningEfforts 枚举）。

## 3. 安全设计

- **密钥只在本机**：api_key / 小米账号密码 / HA token 只存 config/local.json
  （0600），派生到各组件时对含密钥文件一律 0600；账号密码不进 yml，
  只进 0600 的 credentials 文件。
- **后台仅监听 127.0.0.1**，且 POST 有 Origin 校验（拒绝跨站请求，仅允许
  本机页面操作）；响应带 CSP/nosniff 头；请求体限 2MB。
- **音箱端 config.env 是刻意设计**：Mac 完全失联时音箱要能独立调大模型。
  密钥以明文存在于音箱私有目录，属可接受风险（音箱 root 已归本机所有）。
- **派生文件全部在 config/generated/**（.gitignore 排除），不会入库泄漏。

## 4. 「测试连接」按钮

后台提供两个连通性测试（只读探测，不写任何配置）：

| 测试 | 探测方式 | 判定 |
|------|---------|------|
| 大模型 | `GET <base_url>/models`（带 Authorization） | 2xx=端点可达；401/403=Key 无效；404=网关不提供模型列表，也属连通正常；网络错误会给出原因 |
| Home Assistant | `GET <url>/api/`（带 Authorization） | 2xx=可达；401/403=Token 无效 |

## 5. 快速开始

```bash
# 1. 复制模板并填写
cp config/config.example.json config/local.json
# 2. 启动配置后台，浏览器打开 http://127.0.0.1:8390 填写并保存
python3 admin/server.py
# 3. 保存后 config/generated/ 下出现全部派生文件；部署音箱端两份：
#    config/generated/speaker/config.env
#    config/generated/speaker/system_prompt.txt
#    （部署到音箱 /data/open-xiaoai/ 对应位置）
# 4. 按需启动组件：
#    bridge/run-mcp.sh                 # hass-mcp（8321）
#    bridge/run-xiaogpt.sh             # xiaogpt ASR 桥
#    python3 bridge/xiaogpt-bridge.py  # 本地大脑桥（8322）
#    cd migpt && pnpm start            # migpt 引擎（4397/4398/4399）
```