# 安全说明

## 密钥存放

`xiaoai-dsh` 的所有敏感信息——大模型 API Key、Home Assistant Token、
小米账号密码——**只保存在你自己刷过机的设备上**（Mac 的 `config/local.json`
与部署到音箱的 `speaker/config.env`），由 localhost 配置后台（`admin/server.py`）
写入 `config/local.json`（已被 `.gitignore` 排除）以及若干生成文件：

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `config/local.json` | 统一配置（唯一事实来源，含 `bridge.secret`） | ❌ 已忽略 |
| `config/local-admin.token` | 配置后台的 CSRF token（600） | ❌ 已忽略 |
| `config/generated/` | 后台生成的全部派生配置（含 `bridge-secret` 兜底文件） | ❌ 已忽略 |
| `bridge/.env` | HA URL/Token | ❌ 已忽略 |
| `bridge/xiaogpt-credentials` | 小米账号/密码/设备 ID（600） | ❌ 已忽略 |
| `bridge/xiaomi-cookie.json` | 从浏览器提取的小米登录 Cookie（600） | ❌ 已忽略 |
| `speaker/config.env` | 音箱端大模型直连配置（部署到音箱） | ❌ 已忽略 |
| `speaker/system_prompt.txt` | 音箱端降级提示词（部署到音箱） | ❌ 已忽略 |

## 进程间鉴权

- **桥 ↔ migpt**（127.0.0.1:8322 与 4398）：全部 POST 端点要求
  `Authorization: Bearer <bridge.secret>`（后台保存配置时自动生成 32 位 hex，
  写入 `config/local.json` 并兜底到 `config/generated/bridge-secret`）。
  `/v1/models` 保持无鉴权（migpt 健康探测用），不暴露任何数据。
- **音箱 ↔ migpt**（4399 WebSocket）：可选两层认证——来源 IP allowlist
  （`XIAOAI_WS_ALLOWLIST`）+ 共享 secret（`XIAOAI_WS_SECRET`，握手后首条消息
  `{"auth":"..."}` 恒时比较）。未配置时保持对旧版音箱客户端的完全兼容（启动
  日志会警告），生产部署建议至少开 allowlist。启用 secret 后旧版音箱二进制会
  在 5 秒超时后断开重连——迁移顺序：先升级音箱端 client，再开启 secret。
- **web-audio relay**（4378）：只代理公网音频流（拒绝 loopback/私网/metadata
  目标），流地址带随机访问令牌（每次点歌重新生成），局域网内其他设备无法
  旁听；B 站 mcdn 等证书链不完整的 CDN 仅在匹配域名时跳过 TLS 校验。
- **文件工具**（桥的只读电脑文件三件套）：限制在 Mac 主目录内，逐分量拒绝
  符号链接（防 TOCTOU 逃逸），屏蔽 `.ssh`/`.dsh`/钥匙串等敏感目录。

## 提交前自查

```bash
# 全仓库扫一遍密钥特征（应该零命中）
grep -rniE "sk-[a-z0-9]{16,}|Bearer [A-Za-z0-9._-]{20,}|eyJhbGciOi" . \
  --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=target | grep -v "example"
```

## 音箱端密钥的说明

音箱上的 `native-block.sh` 在「Mac 失联」的降级模式下需要**独立**调用大模型，
所以音箱本地会存一份 `config.env`（含 API Key）。这是刻意设计：
音箱在内网、配置由你自己部署，不要把这个文件交给不可信的人。

## 刷机默认口令

刷机流程涉及设备默认 root 口令（上游 open-xiaoai 项目的公开默认值），
刷机完成后建议按 `docs/flashing.md` 修改或禁用口令登录。

## 上游项目

本项目的引擎与刷机工具链基于以下开源项目（MIT）：

- [open-xiaoai](https://github.com/idootop/open-xiaoai) —— 小爱音箱接管引擎与刷机工具
- [MiGPT](https://github.com/idootop/mi-gpt) —— 小爱音箱接入 LLM 的先行者
- [xiaogpt](https://github.com/yihong0618/xiaogpt) —— 小爱音箱 ASR 桥

如有安全漏洞报告，欢迎提 Issue。
