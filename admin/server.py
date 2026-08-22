#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiaoai-dsh localhost 配置后台
============================
本机浏览器打开 http://127.0.0.1:8390 即可配置：
  - 大模型（OpenAI 兼容 API 地址 / Key / 快模型 / 深模型 / 系统提示词）
  - Home Assistant（URL / Token）
  - 小米账号（刷机后 xiaogpt ASR 桥用）
  - 音箱与电脑 IP、关键设备实体、本地路径

所有配置只保存在本机 config/local.json（.gitignore 已排除），
保存时自动生成各组件需要的派生文件到 config/generated/。
纯 Python 标准库实现，无任何第三方依赖，仅监听 127.0.0.1。

用法：
    python3 admin/server.py [--port 8390]
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
LOCAL_CONFIG = os.path.join(CONFIG_DIR, "local.json")
EXAMPLE_CONFIG = os.path.join(CONFIG_DIR, "config.example.json")
GENERATED_DIR = os.path.join(CONFIG_DIR, "generated")
STATIC_DIR = os.path.join(ROOT, "admin", "static")

DEFAULT_PORT = 8390


# ---------------------------------------------------------------- config io

def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> tuple[dict, bool]:
    """返回 (config, is_example)。local.json 不存在时回落到示例配置。"""
    if os.path.exists(LOCAL_CONFIG):
        try:
            with open(LOCAL_CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            with open(EXAMPLE_CONFIG, encoding="utf-8") as f:
                example = json.load(f)
            return deep_merge(example, cfg), False
        except (json.JSONDecodeError, OSError):
            pass
    with open(EXAMPLE_CONFIG, encoding="utf-8") as f:
        return json.load(f), True


def save_config(cfg: dict) -> None:
    with open(LOCAL_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(LOCAL_CONFIG, 0o600)


def _sub(template: str, mapping: dict) -> str:
    out = template
    for k, v in mapping.items():
        out = out.replace("{{%s}}" % k, str(v))
    return out


def _write(path: str, content: str, mode: int = 0o644) -> str:
    full = os.path.join(GENERATED_DIR, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(full, mode)
    return full


def _quote_sh(s: str) -> str:
    """shell 双引号转义（密钥不含换行时安全）。"""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


XIAOGPT_CONFIG_TPL = """# xiaogpt 配置：小爱音箱 → 本地桥（本机大脑）
# 账号密码不写在这里：由 run-xiaogpt.sh 从 xiaogpt-credentials（权限600）注入环境变量 MI_USER / MI_PASS

hardware: OH2P
# 音箱的 miot DID（跳过 miio 自动发现）
mi_did: "{{did}}"
use_command: false
mute_xiaoai: true
verbose: false

# ===== 对话 AI 设置 =====
bot: chatgptapi
prompt: "请用200字以内口语化回答，不要列表和链接"
keyword:
    - "请"
    - "帮我"
    - "请问"
change_prompt_keyword:
    - "更改提示词"
start_conversation: "开始持续对话"
end_conversation: "结束持续对话"
stream: false

# ===== OpenAI 兼容端点：本地桥（127.0.0.1:8322）=====
openai_key: "dummy"
api_base: "http://127.0.0.1:8322/v1"

# ===== 语音设置 =====
tts: mi
"""

SPEAKER_CONFIG_TPL = """# 音箱端大模型直连配置（Mac 失联降级模式用）
# 由 xiaoai-dsh 本地后台生成，部署到音箱 /data/open-xiaoai/config.env，勿提交仓库
LLM_BASE="{{base_url}}"
LLM_KEY="{{api_key}}"
LLM_MODEL="{{fast_model}}"
MAC_IP="{{mac_ip}}"
"""

SYSTEM_PROMPT_DEGRADED_SUFFIX = (
    "\n\n当前后台大脑离线，你处于音箱端直连降级模式：没有设备控制工具、没有深度思考，"
    "只能做基础问答。遇到查设备、开灯关灯、查实时数据这类做不了的事，"
    "就说「后台大脑暂时离线了，这类事情请稍后再试」，不要编造。"
)

DSH_SETTINGS_TPL = """permission:
  defaultPreset: danger-full-access
agent-default-model:
  provider: speaker-llm
  model: {{deep_model}}
  reasoningEffort: high
llm-pi-ai:
  providers:
    speaker-llm:
      displayName: Speaker LLM
      apiKeyEnv: LLM_API_KEY
      api: openai-completions
      baseURL: {{base_url}}
      defaultMaxTokens: 131072
      compat:
        maxTokensField: max_tokens
      models:
        - id: {{deep_model}}
          name: {{deep_model}}
{{deep_compat}}        - id: {{fast_model}}
          name: {{fast_model}}
{{fast_compat}}"""

REASONING_COMPAT = """          compat:
            maxTokensField: max_tokens
            thinkingFormat: deepseek
            supportsReasoningEffort: true
            supportsDeveloperRole: false
            requiresReasoningContentOnAssistantMessages: true
            supportsStore: false
          reasoningEfforts:
            off:
            low: low
            high: high
            max: max
"""

DSH_FAST_PATCH_TPL = """# 快速通道模型补丁（由 xiaoai-dsh 后台生成）
- id: agent-default-model
  config:
    provider: speaker-llm
    model: {{fast_model}}
{{fast_effort}}"""

CORDIS_PATCH_TPL = """# 音箱专属覆盖层（DSH_HOME=音箱 home 专用，不污染用户 DSH）：
# 1. 补上 headless 树缺失的 storage/workspace 服务链（web 树里有，headless 没有）；
# 2. 挂载 Memory Evolve（reviewMode=auto：记忆直接落盘，无需人工确认）。
- insert:
    - id: storage
      name: '@deepseek-ai/dsh-storage'

    - id: storage-json
      name: '@deepseek-ai/dsh-storage-json'
      config:
        root: {{speaker_home}}/storages

    - id: storage-domain
      name: '@deepseek-ai/dsh-storage-domain'
      config:
        backend: json

    - id: workspace
      name: '@deepseek-ai/dsh-workspace'

    - id: dsh-memory-evolve
      name: 'dsh-memory-evolve'
      config:
        reviewMode: auto
"""


def generate_derived(cfg: dict) -> list[str]:
    """根据统一配置生成全部派生文件，返回生成的文件相对路径列表。"""
    llm = cfg["llm"]
    ha = cfg["home_assistant"]
    mi = cfg["xiaomi_account"]
    speaker = cfg["speaker"]
    mac = cfg["mac"]
    paths = cfg["paths"]
    reasoning = bool(llm.get("reasoning_support"))

    created: list[str] = []

    # 1. xiaogpt ASR 桥配置
    created.append(_write("bridge/xiaogpt-credentials",
        'export MI_USER="%s"\nexport MI_PASS="%s"\nexport MI_DEVICE_ID="%s"\n'
        % (_quote_sh(mi["username"]), _quote_sh(mi["password"]), _quote_sh(mi["device_id"])),
        mode=0o600))
    created.append(_write("bridge/xiaogpt-config.yml",
        _sub(XIAOGPT_CONFIG_TPL, {"did": speaker["did"]})))

    # 2. HA 环境（run-mcp.sh 使用）
    created.append(_write("bridge/.env",
        "HA_URL=%s\nHA_TOKEN=%s\n" % (ha["url"], ha["token"]), mode=0o600))

    # 3. 音箱端降级直连配置
    created.append(_write("speaker/config.env",
        _sub(SPEAKER_CONFIG_TPL, {
            "base_url": llm["base_url"].rstrip("/"),
            "api_key": llm["api_key"],
            "fast_model": llm["fast_model"],
            "mac_ip": mac["ip"],
        }), mode=0o600))
    prompt = llm.get("system_prompt", "").strip()
    if prompt and not prompt.endswith(("。", "！", "？")):
        prompt += "。"
    created.append(_write("speaker/system_prompt.txt",
        prompt + SYSTEM_PROMPT_DEGRADED_SUFFIX))

    # 4. 音箱 DSH（深通道）配置
    deep_compat = REASONING_COMPAT if reasoning else ""
    fast_compat = REASONING_COMPAT if reasoning else ""
    created.append(_write("dsh-speaker/settings.yaml",
        _sub(DSH_SETTINGS_TPL, {
            "deep_model": llm["deep_model"],
            "fast_model": llm["fast_model"],
            "base_url": llm["base_url"].rstrip("/"),
            "deep_compat": deep_compat,
            "fast_compat": fast_compat,
        })))
    created.append(_write("dsh-speaker/dsh-fast.patch.yml",
        _sub(DSH_FAST_PATCH_TPL, {
            "fast_model": llm["fast_model"],
            "fast_effort": "    reasoningEffort: off" if reasoning else "",
        })))
    created.append(_write("dsh-speaker/cordis.patch.yml",
        _sub(CORDIS_PATCH_TPL, {"speaker_home": paths["speaker_dsh_home"]})))
    created.append(_write("dsh-speaker/.credentials.yaml",
        "LLM_API_KEY: %s\n" % llm["api_key"], mode=0o600))

    return created


# ---------------------------------------------------------------- connectivity

def _http_json(url: str, headers: dict, method: str = "GET", body: bytes | None = None,
               timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2048).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def verify_llm(cfg: dict) -> dict:
    llm = cfg["llm"]
    base = llm["base_url"].rstrip("/")
    if not llm.get("api_key"):
        return {"ok": False, "error": "尚未填写 API Key"}
    code, body = _http_json(base + "/models",
                            {"Authorization": "Bearer " + llm["api_key"]})
    if 200 <= code < 300:
        return {"ok": True, "detail": "HTTP %d，端点可达" % code}
    if code in (401, 403):
        return {"ok": False, "error": "HTTP %d：API Key 无效或无权限" % code}
    if code == 404:
        return {"ok": True,
                "detail": "HTTP 404（该网关不提供 /models 列表，属正常），端点已连通"}
    if code:
        return {"ok": False, "error": "HTTP %d：%s" % (code, body[:200])}
    return {"ok": False, "error": "网络错误：%s" % body[:200]}


def verify_ha(cfg: dict) -> dict:
    ha = cfg["home_assistant"]
    base = ha["url"].rstrip("/")
    if not ha.get("token"):
        return {"ok": False, "error": "尚未填写 HA Token"}
    code, body = _http_json(base + "/api/",
                            {"Authorization": "Bearer " + ha["token"]})
    if 200 <= code < 300:
        return {"ok": True, "detail": "HTTP %d，HA 可达" % code}
    if code in (401, 403):
        return {"ok": False, "error": "HTTP %d：Token 无效" % code}
    if code:
        return {"ok": False, "error": "HTTP %d：%s" % (code, body[:200])}
    return {"ok": False, "error": "网络错误：%s" % body[:200]}


# ---------------------------------------------------------------- http server

class Handler(BaseHTTPRequestHandler):
    server_version = "xiaoai-dsh-admin/1.0"

    # ---- 安全：只服务本机，POST 校验 Origin
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # 非浏览器客户端（curl 等）
        host = self.headers.get("Host", "")
        return origin in ("http://%s" % host, "https://%s" % host,
                          "http://127.0.0.1:%d" % self.server.server_port,
                          "http://localhost:%d" % self.server.server_port)

    def _send(self, code: int, obj, ctype: str = "application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read().encode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "index.html 缺失"})
        if self.path == "/api/config":
            cfg, is_example = load_config()
            cfg["_is_example"] = is_example
            return self._send(200, cfg)
        if self.path == "/api/generated":
            out = []
            if os.path.isdir(GENERATED_DIR):
                for root, _, files in os.walk(GENERATED_DIR):
                    for fn in sorted(files):
                        rel = os.path.relpath(os.path.join(root, fn), GENERATED_DIR)
                        out.append(rel)
            return self._send(200, {"files": out})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._origin_ok():
            return self._send(403, {"error": "拒绝跨站请求（仅允许本机页面操作）"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2 * 1024 * 1024:
                return self._send(413, {"error": "请求过大"})
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "非法 JSON"})

        if self.path == "/api/config":
            save_config(body)
            try:
                created = generate_derived(body)
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": "生成配置失败：%s" % e})
            return self._send(200, {"ok": True, "generated": created})
        if self.path == "/api/verify":
            kind = body.get("type", "llm")
            if kind == "ha":
                return self._send(200, verify_ha(body.get("config") or load_config()[0]))
            return self._send(200, verify_llm(body.get("config") or load_config()[0]))
        return self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # 安静日志
        sys.stderr.write("[admin] %s\n" % (fmt % args))


def main():
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("xiaoai-dsh 配置后台已启动： http://127.0.0.1:%d" % port)
    print("配置保存在 %s" % LOCAL_CONFIG)
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
