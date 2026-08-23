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

安全设计：
  - DNS rebinding 防护：所有请求必须先通过 _host_ok()（Host 头仅接受
    127.0.0.1 / localhost / [::1]，且端口必须等于监听端口）；
  - CSRF/会话防护：启动时生成随机 admin token（持久化到
    config/local-admin.token，0600），随页面注入；所有 POST 必须携带
    X-Admin-Token 头，跨站页面因同源策略读不到 token；
  - POST 的 Origin 头只接受本机同源（http/https + 127.0.0.1/localhost/[::1]
    + 监听端口）；无 Origin 的非浏览器客户端在 Host 校验通过后放行；
  - 配置保存为原子事务：全部派生文件先在临时目录生成，成功后逐个
    os.replace 提交，最后原子写 local.json；任一步失败则旧配置原样保留；
  - 派生文件中的 shell 注入防护：xiaogpt-credentials 用 shlex.quote 包裹，
    其余字段先做字符集/URL/主机名校验，含换行或控制字符直接 400 拒绝。

用法：
    python3 admin/server.py [--port 8390]
"""

import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sys
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
LOCAL_CONFIG = os.path.join(CONFIG_DIR, "local.json")
EXAMPLE_CONFIG = os.path.join(CONFIG_DIR, "config.example.json")
GENERATED_DIR = os.path.join(CONFIG_DIR, "generated")
STATIC_DIR = os.path.join(ROOT, "admin", "static")
ADMIN_TOKEN_FILE = os.path.join(CONFIG_DIR, "local-admin.token")

DEFAULT_PORT = 8390
MAX_BODY = 2 * 1024 * 1024          # POST body 上限 2MB
MAX_WORKERS = 16                    # 并发连接信号量上限

# ---------------------------------------------------------------- 校验正则

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")                     # 控制字符/换行
_API_KEY_RE = re.compile(r"^[A-Za-z0-9._\-:/=+]+$")               # token/key 安全字符集
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/+-]+$")                   # 模型名
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]*$")                # miot DID
_DID_RE = re.compile(r"^[A-Za-z0-9._\-]*$")                       # 音箱 did（进 YAML 引号串）
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")
_IPV6_LOOPBACK = ("::1", "0:0:0:0:0:0:0:1")
_BRIDGE_SECRET_RE = re.compile(r"^[0-9a-fA-F]{32}$")

REQUIRED_SECTIONS = ("llm", "home_assistant", "xiaomi_account",
                     "speaker", "mac", "devices", "paths")


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


def _atomic_write(path: str, content: str, mode: int = 0o644) -> str:
    """同目录临时文件 + fsync + os.replace 的原子写；权限在 replace 前设置。"""
    full = os.path.abspath(path)
    d = os.path.dirname(full)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, full)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return full


def _sub(template: str, mapping: dict) -> str:
    out = template
    for k, v in mapping.items():
        out = out.replace("{{%s}}" % k, str(v))
    return out


def _write(path: str, content: str, mode: int = 0o644,
           target_dir: str | None = None) -> str:
    """把派生文件写入 target_dir（默认 GENERATED_DIR），原子写。"""
    full = os.path.join(target_dir or GENERATED_DIR, path)
    return _atomic_write(full, content, mode)


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


# ---------------------------------------------------------------- 校验

def check_http_url(url: str) -> str | None:
    """校验 http/https URL；返回错误信息或 None（空串视为未填写，放行）。"""
    if not isinstance(url, str) or not url.strip():
        return None
    if _CONTROL_RE.search(url) or any(c.isspace() for c in url):
        return "地址含换行/空白或控制字符"
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return "仅支持 http/https 协议"
    if not p.netloc:
        return "缺少主机名"
    if any(c in url for c in "*?[]"):
        return "地址不能包含 * ? [ ] 字符"
    return None


def check_ip_or_host(v: str) -> str | None:
    """MAC_IP 校验：IPv4/IPv6 字面量或主机名；返回错误信息或 None。"""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        ipaddress.ip_address(v)
        return None
    except ValueError:
        pass
    if _HOSTNAME_RE.match(v):
        return None
    return "必须是 IPv4/IPv6 地址或主机名（拒绝 shell 元字符）"


def validate_config(cfg) -> str | None:
    """校验待保存配置的结构与字段；返回错误信息或 None。

    覆盖：必填段存在且类型正确、所有入档字段禁止控制字符/换行、
    密钥/令牌安全字符集、URL 仅 http/https、模型名/设备 ID 字符集、
    MAC IP 为 IP 或主机名。任何失败调用方都应 400 拒绝保存。
    """
    if not isinstance(cfg, dict):
        return "配置必须是 JSON 对象"
    for name in REQUIRED_SECTIONS:
        if name not in cfg:
            return "缺少配置段 %s" % name
        if not isinstance(cfg[name], dict):
            return "配置段 %s 类型错误（应为对象）" % name

    # 所有非提示词字段禁止控制字符/换行（防注入派生文件与日志）
    for sec in REQUIRED_SECTIONS:
        for k, v in cfg[sec].items():
            if sec == "llm" and k == "system_prompt":
                continue  # 提示词允许换行（纯文本文件）
            if isinstance(v, str) and _CONTROL_RE.search(v):
                return "字段 %s.%s 不能包含换行或控制字符" % (sec, k)

    # 密钥/令牌安全字符集（会进入 shell 环境文件与 YAML）
    for label, val in (("llm.api_key", cfg["llm"].get("api_key")),
                       ("home_assistant.token", cfg["home_assistant"].get("token"))):
        if val and not (isinstance(val, str) and _API_KEY_RE.match(val)):
            return "%s 含非法字符（仅允许字母数字与 . _ - : / = +）" % label

    # URL 仅 http/https
    for label, val in (("llm.base_url", cfg["llm"].get("base_url")),
                       ("home_assistant.url", cfg["home_assistant"].get("url"))):
        if val:
            err = check_http_url(val)
            if err:
                return "%s：%s" % (label, err)

    # 模型名（进 YAML 与 .env）
    for label, val in (("llm.fast_model", cfg["llm"].get("fast_model")),
                       ("llm.deep_model", cfg["llm"].get("deep_model"))):
        if val and not (isinstance(val, str) and _MODEL_RE.match(val)):
            return "%s 含非法字符（仅允许字母数字与 . _ : / + -）" % label

    # 设备 ID / 音箱 DID（进 shell 环境文件 / YAML）
    if cfg["xiaomi_account"].get("device_id") and not (
            isinstance(cfg["xiaomi_account"]["device_id"], str)
            and _DEVICE_ID_RE.match(cfg["xiaomi_account"]["device_id"])):
        return "xiaomi_account.device_id 含非法字符（仅允许字母数字与 . _ : -）"
    if cfg["speaker"].get("did") and not (
            isinstance(cfg["speaker"]["did"], str)
            and _DID_RE.match(cfg["speaker"]["did"])):
        return "speaker.did 含非法字符（仅允许字母数字与 . _ -）"

    # MAC IP
    err = check_ip_or_host(cfg["mac"].get("ip"))
    if err:
        return "mac.ip：%s" % err

    if "bridge" in cfg and not isinstance(cfg["bridge"], dict):
        return "配置段 bridge 类型错误（应为对象）"
    return None


def ensure_bridge_secret(cfg: dict) -> str:
    """确保顶层 bridge.secret 为 32 位 hex；缺失/非法则生成，合法则保留。"""
    if not isinstance(cfg.get("bridge"), dict):
        cfg["bridge"] = {}
    cur = cfg["bridge"].get("secret")
    if isinstance(cur, str) and _BRIDGE_SECRET_RE.match(cur):
        return cur
    cfg["bridge"]["secret"] = secrets.token_hex(16)
    return cfg["bridge"]["secret"]


# ---------------------------------------------------------------- 派生文件

def generate_derived(cfg: dict, target_dir: str | None = None) -> list[str]:
    """根据统一配置生成全部派生文件，返回生成的文件绝对路径列表。

    target_dir 缺省为 GENERATED_DIR；保存事务先把全部文件生成到临时目录，
    全部成功后由调用方逐个 os.replace 提交。
    """
    target = target_dir or GENERATED_DIR
    llm = cfg["llm"]
    ha = cfg["home_assistant"]
    mi = cfg["xiaomi_account"]
    speaker = cfg["speaker"]
    mac = cfg["mac"]
    paths = cfg["paths"]
    reasoning = bool(llm.get("reasoning_support"))

    created: list[str] = []

    # 1. xiaogpt ASR 桥配置（run-xiaogpt.sh 会 source 此文件，必须 shell 引用）
    created.append(_write("bridge/xiaogpt-credentials",
        "export MI_USER=%s\nexport MI_PASS=%s\nexport MI_DEVICE_ID=%s\n"
        % (shlex.quote(mi["username"]), shlex.quote(mi["password"]),
           shlex.quote(mi["device_id"])),
        mode=0o600, target_dir=target))
    created.append(_write("bridge/xiaogpt-config.yml",
        _sub(XIAOGPT_CONFIG_TPL, {"did": speaker["did"]}),
        target_dir=target))

    # 2. HA 环境（run-mcp.sh 用 grep|cut 读取原始值，保持 KEY=VALUE 不带引号）
    created.append(_write("bridge/.env",
        "HA_URL=%s\nHA_TOKEN=%s\n" % (ha["url"].rstrip("/"), ha["token"]),
        mode=0o600, target_dir=target))

    # 3. 音箱端降级直连配置（字段先经校验：URL/http(s)、key 安全字符集、IP/主机名）
    created.append(_write("speaker/config.env",
        _sub(SPEAKER_CONFIG_TPL, {
            "base_url": llm["base_url"].rstrip("/"),
            "api_key": llm["api_key"],
            "fast_model": llm["fast_model"],
            "mac_ip": mac["ip"],
        }), mode=0o600, target_dir=target))
    prompt = llm.get("system_prompt", "").strip() or ""
    if prompt and not prompt.endswith(("。", "！", "？")):
        prompt += "。"
    created.append(_write("speaker/system_prompt.txt",
        prompt + SYSTEM_PROMPT_DEGRADED_SUFFIX,
        target_dir=target))

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
        }), target_dir=target))
    created.append(_write("dsh-speaker/dsh-fast.patch.yml",
        _sub(DSH_FAST_PATCH_TPL, {
            "fast_model": llm["fast_model"],
            "fast_effort": "    reasoningEffort: off" if reasoning else "",
        }), target_dir=target))
    created.append(_write("dsh-speaker/cordis.patch.yml",
        _sub(CORDIS_PATCH_TPL, {"speaker_home": paths["speaker_dsh_home"]}),
        target_dir=target))
    created.append(_write("dsh-speaker/.credentials.yaml",
        "LLM_API_KEY: %s\n" % llm["api_key"],
        mode=0o600, target_dir=target))

    # 5. 桥鉴权 secret 兜底文件（migpt/bridge 经 config_loader.cfg_bridge_secret 读取）
    secret = (cfg.get("bridge") or {}).get("secret", "")
    created.append(_write("bridge-secret", secret + "\n",
        mode=0o600, target_dir=target))

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
    llm = cfg.get("llm") or {}
    base = (llm.get("base_url") or "").rstrip("/")
    if not llm.get("api_key"):
        return {"ok": False, "error": "尚未填写 API Key"}
    err = check_http_url(base)
    if err:
        return {"ok": False, "error": "API 地址：%s" % err}
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
    ha = cfg.get("home_assistant") or {}
    base = (ha.get("url") or "").rstrip("/")
    if not ha.get("token"):
        return {"ok": False, "error": "尚未填写 HA Token"}
    err = check_http_url(base)
    if err:
        return {"ok": False, "error": "HA 地址：%s" % err}
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

# 页面 CSP：script 用 fresh nonce（token 注入脚本与页面内联脚本都带 nonce），
# 不开 script unsafe-inline；style 沿用现有 unsafe-inline。
_API_CSP = ("default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'")

_PAGE_CSP = ("default-src 'self'; script-src 'self' 'nonce-%s'; "
             "style-src 'self' 'unsafe-inline'")


class AdminHTTPServer(ThreadingHTTPServer):
    """带并发上限的 ThreadingHTTPServer（超出 max_workers 的连接立即拒绝）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass,
                 admin_token: str | None = None, max_workers: int = MAX_WORKERS):
        self.admin_token = admin_token or secrets.token_urlsafe(32)
        self._workers = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address):
        if not self._workers.acquire(blocking=False):
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()


def load_or_create_token() -> str:
    """读取 config/local-admin.token；不存在/非法则生成并原子落盘（0600）。"""
    try:
        if os.path.exists(ADMIN_TOKEN_FILE):
            with open(ADMIN_TOKEN_FILE, encoding="utf-8") as f:
                tok = f.read().strip()
            if tok and re.fullmatch(r"[A-Za-z0-9_\-]+", tok):
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _atomic_write(ADMIN_TOKEN_FILE, tok + "\n", 0o600)
    except OSError:
        pass  # 写不进去就只存内存（页面仍能拿到并用于本轮会话）
    return tok


class Handler(BaseHTTPRequestHandler):
    server_version = "xiaoai-dsh-admin/1.0"

    # ---- 安全：Host / Origin / token 三重校验

    def _host_ok(self) -> bool:
        """Host 头强校验：仅 127.0.0.1 / localhost / [::1]（含端口，可带 IPv6 字面量）。
        端口必须显式给出且等于监听端口（不带端口一律拒绝）。"""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        port = None
        if host.startswith("["):  # IPv6 字面量：[::1]:8390
            m = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::(\d+))?", host)
            if not m:
                return False
            if m.group(1).lower() not in _IPV6_LOOPBACK:
                return False
            port = m.group(2)
        else:
            if host.count(":") > 1:      # 裸 IPv6 不带方括号，拒绝
                return False
            if ":" in host:
                name, _, p = host.rpartition(":")
                if not p.isdigit():
                    return False
                port = p
            else:
                name = host
            if name not in ("127.0.0.1", "localhost"):
                return False
        # 严格：必须显式端口且等于监听端口（"无端口默认 80/443" 一律拒绝）
        if port is None:
            return False
        return int(port) == self.server.server_port

    def _origin_ok(self) -> bool:
        """Origin 校验：不信任攻击者 Host，只认本机字面量同源。
        无 Origin 视为非浏览器客户端（curl），放行（Host 校验已通过）。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        port = self.server.server_port
        return origin in (
            "http://127.0.0.1:%d" % port, "https://127.0.0.1:%d" % port,
            "http://localhost:%d" % port, "https://localhost:%d" % port,
            "http://[::1]:%d" % port, "https://[::1]:%d" % port,
        )

    def _token_ok(self) -> bool:
        got = self.headers.get("X-Admin-Token", "")
        if not got:
            return False
        return hmac.compare_digest(got, self.server.admin_token)

    def _deny(self, code: int, msg: str):
        self._send(code, {"error": msg})

    # ---- 响应

    def _send(self, code: int, obj, ctype: str = "application/json; charset=utf-8",
              headers: dict | None = None):
        """统一出参：CSP / nosniff / referrer / no-store 安全头 + 可选覆盖头。"""
        body = obj if isinstance(obj, bytes) else \
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        extra = dict(headers or {})
        csp = extra.pop("Content-Security-Policy", _API_CSP)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _page(self) -> tuple[bytes, str]:
        """动态输出 index.html：注入带 nonce 的 token 脚本，并给页面内联脚本补 nonce。"""
        nonce = secrets.token_urlsafe(16)
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        snippet = ('<script nonce="%s">window.__ADMIN_TOKEN__=%s;</script>'
                   % (nonce, json.dumps(self.server.admin_token)))
        if "__DSH_ADMIN_NONCE__" in html:
            html = html.replace("__DSH_ADMIN_NONCE__", nonce)
        if "</head>" in html:
            html = html.replace("</head>", snippet + "</head>", 1)
        else:
            html += snippet
        return html.encode("utf-8"), nonce

    # ---- GET

    def do_GET(self):
        if not self._host_ok():
            return self._deny(403, "拒绝非本机 Host（仅允许 127.0.0.1/localhost/[::1] 且端口匹配）")
        if self.path in ("/", "/index.html"):
            try:
                page, nonce = self._page()
            except OSError:
                return self._deny(500, "index.html 缺失")
            return self._send(200, page, "text/html; charset=utf-8",
                              {"Content-Security-Policy": _PAGE_CSP % nonce})
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

    # ---- POST

    def do_POST(self):
        if not self._host_ok():
            return self._deny(403, "拒绝非本机 Host（仅允许 127.0.0.1/localhost/[::1] 且端口匹配）")
        if not self._origin_ok():
            return self._deny(403, "拒绝跨站请求（Origin 仅允许本机同源页面）")
        if not self._token_ok():
            return self._deny(403, "缺少或错误的 X-Admin-Token")

        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            return self._deny(411, "缺少 Content-Length 请求头")
        try:
            length = int(length_raw.strip())
        except ValueError:
            return self._deny(400, "Content-Length 非法")
        if length < 0:
            return self._deny(400, "Content-Length 非法（负数）")
        if length > MAX_BODY:
            return self._deny(413, "请求过大（超过 2MB）")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._deny(400, "非法 JSON")

        if self.path == "/api/config":
            return self._save_config(body)
        if self.path == "/api/verify":
            kind = body.get("type", "llm") if isinstance(body, dict) else "llm"
            cfg = body.get("config") if isinstance(body, dict) else None
            base = cfg or load_config()[0]
            if kind == "ha":
                return self._send(200, verify_ha(base))
            return self._send(200, verify_llm(base))
        return self._send(404, {"error": "not found"})

    # ---- 配置保存（原子事务）

    def _save_config(self, body):
        err = validate_config(body)
        if err:
            return self._deny(400, err)
        prev, _ = load_config()
        # 页面表单不含 optional（或传空）：继承上一份配置，避免误抹
        if not isinstance(body.get("optional"), dict) or not body["optional"]:
            if isinstance(prev.get("optional"), dict):
                body["optional"] = prev["optional"]
        # bridge.secret：incoming 无合法 secret 时保留上一份（防 secret 轮换打断桥鉴权）
        cur_secret = body.get("bridge", {}).get("secret") \
            if isinstance(body.get("bridge"), dict) else None
        if not (isinstance(cur_secret, str) and _BRIDGE_SECRET_RE.match(cur_secret)) \
                and isinstance(prev.get("bridge"), dict) \
                and isinstance(prev["bridge"].get("secret"), str) \
                and _BRIDGE_SECRET_RE.match(prev["bridge"]["secret"]):
            body["bridge"] = dict(prev["bridge"])
        ensure_bridge_secret(body)

        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix=".save-", dir=CONFIG_DIR)
        generated: list[str] = []
        try:
            # ① 全部派生文件先生成到临时目录（失败则什么都不落盘）
            generate_derived(body, target_dir=tmpdir)
            # ② 全部成功后逐个原子提交（同文件系统 os.replace）
            for root, _, files in os.walk(tmpdir):
                for fn in sorted(files):
                    src = os.path.join(root, fn)
                    rel = os.path.relpath(src, tmpdir)
                    dst = os.path.join(GENERATED_DIR, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.replace(src, dst)
                    generated.append(dst)
            # ③ 最后原子写 local.json（权限 0600）
            _atomic_write(LOCAL_CONFIG,
                          json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                          0o600)
        except Exception as e:  # noqa: BLE001
            return self._deny(500, "生成配置失败：%s" % e)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return self._send(200, {"ok": True, "generated": generated})

    def log_message(self, fmt, *args):  # 安静日志
        sys.stderr.write("[admin] %s\n" % (fmt % args))


def main():
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    token = load_or_create_token()
    server = AdminHTTPServer(("127.0.0.1", port), Handler, admin_token=token)
    print("xiaoai-dsh 配置后台已启动： http://127.0.0.1:%d" % port)
    print("配置保存在 %s" % LOCAL_CONFIG)
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()