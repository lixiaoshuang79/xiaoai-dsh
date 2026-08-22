# 安全说明

## 你的密钥永远只在本机

`xiaoai-dsh` 的所有敏感信息——大模型 API Key、Home Assistant Token、
小米账号密码——**只保存在本机**，由 localhost 配置后台（`admin/server.py`）
写入 `config/local.json`（已被 `.gitignore` 排除）以及若干生成文件：

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `config/local.json` | 统一配置（唯一事实来源） | ❌ 已忽略 |
| `config/generated/` | 后台生成的全部派生配置 | ❌ 已忽略 |
| `bridge/.env` | HA URL/Token | ❌ 已忽略 |
| `bridge/xiaogpt-credentials` | 小米账号/密码/设备 ID | ❌ 已忽略 |
| `speaker/config.env` | 音箱端大模型直连配置（部署到音箱） | ❌ 已忽略 |
| `speaker/system_prompt.txt` | 音箱端降级提示词（部署到音箱） | ❌ 已忽略 |

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
