# xiaoai-dsh

把小米小爱音箱 Pro（型号 OH2P）接上你自己的大模型，做成一个本地语音管家。

唤醒词还是「小爱同学」，语音识别也还是走小米云端（云端识别的灵敏度本地方案比不上，这部分保留）。但识别出文本之后，回答问题、控制设备、放音乐这些事全部由你 Mac 上跑的程序接管——小米云端的语音助手不再出声、不再执行。

## 适合谁

- 手上有小米小爱音箱 Pro（OH2P），想让它回答用自己的大模型；
- 家里有 Home Assistant（或者愿意装一个），想把设备控制接到音箱上；
- 有一台可以长期开着的 macOS 当大脑。

开发和使用都在 OH2P 上验证过。open-xiaoai 刷机方案支持的其他机型理论上同样适用，但没有逐一测过。

## 能做什么

- **回答**：日常问题走快模型，一两秒出结果；复杂问题自动转深模型慢慢想，想出来的经验还会沉淀成技能下次复用。
- **设备**：开灯关灯、空调（温度/模式/风速/开关）、塔扇（风速/摇头/定时/风模式）、扫地机器人（只扫/只拖/扫拖/回充）、摄像头开关、插座等。设备实体不用手填——程序启动时自动扫描 Home Assistant，按米家设备的命名规律匹配空调、风扇、扫地机、摄像头这些；你家设备名字改得比较特别、匹配不对的时候，再去配置后台手动指一下。
- **音乐**：点歌走网易云正版音源，没版权自动换网络音源；白噪音、电台也可以。
- **提醒**：闹钟、倒计时、定时提醒，到点音箱播报。
- **兜底**：Mac 挂了，音箱还能直连大模型做基础问答；Mac 恢复后自动切回来。

## 安装

1. 按 [docs/flashing.md](docs/flashing.md) 给音箱刷 open-xiaoai 并部署音箱端脚本；
2. 按 [docs/deploy.md](docs/deploy.md) 在 Mac 上装桥、migpt 引擎和 Home Assistant 工具；
3. 浏览器打开配置后台（`python3 admin/server.py` → http://127.0.0.1:8390），填大模型 API 和 HA 地址，设备实体留空即可自动发现；
4. 对音箱说「小爱同学」。

配置项逐字段说明见 [docs/config.md](docs/config.md)，架构原理见 [docs/architecture.md](docs/architecture.md)，踩坑合集见 [docs/faq.md](docs/faq.md)。

## 目录

| 目录 | 是什么 |
|------|--------|
| `admin/` | 本地配置后台（浏览器里填配置，保存后自动生成各组件要用的配置文件），只监听 127.0.0.1:8390 |
| `bridge/` | Mac 上的主程序 `xiaogpt-bridge.py`（编排层：大模型调用、意图路由、设备指令、音乐、提醒、HTTP Handler）+ 叶子模块：`security.py`（URL/IP 校验与日志脱敏）、`state_store.py`（原子写/turn 代际）、`topic_state.py`（话题档案/待答复/历史持久化）、`device_discovery.py`（HA 设备自动发现）；对外是 OpenAI 兼容接口（127.0.0.1:8322） |
| `migpt/` | 音箱接入引擎（基于 open-xiaoai 的 MiGPT-Next）：WebSocket 接音箱（4399）、推送播报（4398）、健康检查（4397） |
| `speaker/` | 部署到音箱 `/data/open-xiaoai/` 的脚本：开机初始化、拦截官方执行、Mac 失联时的直连兜底、预编译的 LD_PRELOAD 拦截库 |
| `probe/` | 刷机脚本封装和 USB 诊断程序 |
| `skills/speaker-skills/` | 给深模型复用的技能（按任务匹配注入） |
| `config/` | 配置模板 `config.example.json`；你本机的真实配置 `local.json` 不进仓库 |
| `test/` | 各组件回归测试：`bridge/test_bridge.py`（编排层）、`bridge/test_security.py`、`bridge/test_state_store.py`、`bridge/test_topic_state.py`、`bridge/test_device_discovery.py`（叶子模块）、`admin/test_admin.py`、`migpt/test/`、Rust 单元测试（`cargo test`） |

## 开发环境

- macOS（刷机工具链只支持 macOS；桥本身是纯 Python 3.10+）；
- Node.js ≥ 20（migpt 引擎用原生 fetch）；
- Rust stable（`migpt/` 与 `packages/client-rust/`）；
- Python 3.10+（bridge / admin，标准库 + 少量依赖）。

运行全部测试：

```bash
python3 -m unittest discover -s bridge -p 'test_*.py'    # 桥
python3 admin/test_admin.py                              # 配置后台
cd migpt && pnpm install && pnpm typecheck && pnpm test  # 引擎（TS）
cd migpt && cargo test --locked                           # 引擎（Rust）
```

## 安全

- 大模型 Key、HA Token、小米账号密码只存在本机 `config/local.json`（权限 600），不进仓库；
- 配置后台只监听 127.0.0.1（8390），并要求 `X-Admin-Token`（启动时生成，存 `config/local-admin.token`）；
- 桥（8322）与 migpt（4398）之间的所有推送/执行端点要求 `Authorization: Bearer <bridge.secret>`（后台保存配置时自动生成 32 位 hex）；桥的 `/v1/chat/completions` 同样要求该 secret；
- 音箱端 WebSocket（4399）支持可选来源 IP allowlist + 共享 secret 认证（`XIAOAI_WS_ALLOWLIST` / `XIAOAI_WS_SECRET`，部署时按需开启，见 docs/deploy.md）；
- web-audio 流式转发 relay（4378）只代理公网音频流（拒绝内网/本机地址），且流地址带随机访问令牌，局域网其他设备无法旁听；
- 音箱端配置文件里带大模型 Key（Mac 失联时音箱要能自己直连），只部署到你自己刷过机的音箱；
- 刷机后先改音箱 root 密码——上游默认密码是公开的（见 flashing.md）；
- 发现安全问题请私下报告，见 [SECURITY.md](SECURITY.md)。

## 由来

基于 [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai)（MIT）——刷机方案、音箱端程序和 MiGPT 引擎都源自这个项目（已停止维护）。小米账号认证桥参考 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt)。

## License

[MIT](LICENSE)
