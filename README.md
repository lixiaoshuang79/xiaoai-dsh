# xiaoai-dsh

把你的小米 AI 音箱改造成**本地大模型语音管家**：唤醒词还是「小爱同学」，但回答你的不再是官方云端，而是你自己配置的大模型（任何 OpenAI 兼容 API），设备控制走本地 Home Assistant 直连，官方云端彻底闭嘴。

```
你说话 → 音箱（官方 ASR 云端识别，但官方不发声不执行）
       → migpt 引擎（Mac，4399 WebSocket）
       → 本地桥 xiaogpt-bridge（8322，OpenAI 兼容端点）
       → 快通道（大模型直连带工具，秒级）或深通道（DSH headless 深度推理）
       → 回答逐段推回 → 音箱 TTS 播报
```

## 特性

- 🗣 **原生唤醒**：「小爱同学」唤醒，官方云级识别灵敏度，回答全走你的大模型
- 🤖 **大模型全自定义**：localhost 配置后台自己填 API 地址 / Key / 快慢模型 / 系统提示词（人设），三通道共用
- 🔌 **设备控制本地化**：开灯/关灯/空调/音量等走 Home Assistant MIoTLan 本地直连，官方云端收不到执行指令
- 🛡 **官方彻底闭嘴**：音箱端 LD_PRELOAD 钩子 + 守护脚本拦截官方 TTS/媒体/执行链，正常模式官方永远不发声
- 🧠 **自我进化**：复杂问题走深通道（DeepSeek Harness headless），解决后沉淀成技能（speaker-skills）与声明式工具（evolved-tools），同类问题下次秒答
- 🎵 **音乐替代链**：点歌走网易云 / 网络音频搜索（B站等）直链在线播放，补位官方音乐服务
- 🔋 **三级容灾**：桥挂 → migpt 直连大模型；Mac 全挂 → 音箱端直连模式（问答直连大模型 + 本地 TTS，设备指令放行官方兜底）
- 💬 **多轮对话**：反问检测自动保持麦克风（回答结尾是问题时无需重复唤醒）

## 组件

| 目录 | 说明 |
|------|------|
| `admin/` | localhost 配置后台（纯标准库 Python，127.0.0.1:8390），生成全部派生配置 |
| `bridge/` | Mac 本机桥（xiaogpt-bridge.py，8322 OpenAI 兼容端点 + 工具层） |
| `migpt/` | 音箱接入引擎（基于 idootop/open-xiaoai 的 MiGPT-Next 示例，4399 WS / 4398 HTTP / 4397 healthz） |
| `speaker/` | 部署到音箱 `/data/open-xiaoai/` 的脚本（拦截守护 + 直连容灾 + hook） |
| `probe/` | 刷机探针与 macOS 刷机脚本 |
| `skills/speaker-skills/` | 7 个音箱技能（深通道沉淀复用） |
| `config/` | 统一配置模板 `config.example.json`（唯一事实来源 `local.json` 不入库） |
| `docs/` | 刷机 / 部署 / 架构 / 配置 / 技能 / FAQ 文档 |

## 快速开始

1. **刷机**：按 [docs/flashing.md](docs/flashing.md) 给音箱刷 open-xiaoai 并部署 `speaker/` 脚本（约 30 分钟）
2. **配置**：`python3 admin/server.py` → 打开 http://127.0.0.1:8390 填大模型 / HA / 小米账号 / 设备实体
3. **启动 Mac 侧**：按 [docs/deploy.md](docs/deploy.md) 装 bridge + migpt（`pnpm install && pnpm build && pnpm start`）+ hass-mcp
4. **说话**：「小爱同学，开灯」「小爱同学，放首歌」

> 详细步骤见 [docs/deploy.md](docs/deploy.md)；架构原理见 [docs/architecture.md](docs/architecture.md)；配置逐字段说明见 [docs/config.md](docs/config.md)；踩坑合集见 [docs/faq.md](docs/faq.md)。

## 安全

- 你的所有密钥（大模型 Key、HA Token、小米账号）**只保存在本机** `config/local.json`（已 gitignore）与生成的派生文件（0600），**绝不入库、绝不上传音箱以外的设备**
- 配置后台仅监听 127.0.0.1，带 CSRF Origin 校验
- 音箱端 `config.env` 是刻意设计（Mac 失联时音箱要独立调大模型），只部署到你自己的音箱
- 刷机后请立即修改音箱 root 默认密码（见 flashing.md）
- 发现安全问题请私下报告，见 [SECURITY.md](SECURITY.md)

## 上游致谢

本项目建立在 [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai)（MIT，Del Wang）之上：刷机方案、音箱端 client、MiGPT 引擎框架均源自该项目（已停止维护）。另感谢 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt) 的小米账号认证桥。

## License

[MIT](LICENSE) © xiaoai-dsh contributors
