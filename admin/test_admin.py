#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin/server.py 安全加固测试（纯标准库，可独立运行）：

    python3 admin/test_admin.py

覆盖：
  - DNS rebinding：恶意 Host / 错误端口 / 正确 Host / localhost / 缺失 Host / IPv6 字面量
  - Origin 校验：恶意 Origin 拒绝、无 Origin（curl）放行、https 同源变体放行
  - CSRF token：缺失 / 错误 / 正确
  - 配置保存成功：local.json 0600、派生文件齐全、bridge-secret 生成与保留
  - 生成失败回滚：local.json 与派生文件保持旧状态、临时目录清理
  - shell 注入防护：特殊字符凭据 shlex 往返、换行/控制字符拒绝、字符集校验
  - HTTP 细节：Content-Length 负数 400 / 缺失 411 / 超限 413 / 空 body 400
  - 页面：token 注入 + CSP nonce + 安全响应头
  - /api/config GET 敏感字段 + no-store
  - optional 段继承防误抹
"""

import json
import os
import re
import shlex
import shutil
import socket
import sys
import tempfile
import threading
import unittest

# 使 `import server` 可用（本文件位于 admin/ 下，与 server.py 同目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_INDEX = os.path.join(REPO_ROOT, "admin", "static", "index.html")

TEST_TOKEN = "test-admin-token-0123456789abcdef"


def valid_cfg() -> dict:
    return {
        "llm": {
            "base_url": "http://127.0.0.1:8322/v1",
            "api_key": "sk-test-key-123",
            "fast_model": "deepseek-chat",
            "deep_model": "deepseek-reasoner",
            "reasoning_support": True,
            "system_prompt": "你是小爱。",
        },
        "home_assistant": {
            "url": "http://192.168.1.10:8123",
            "token": "tok_abcdef0123456789",
        },
        "xiaomi_account": {
            "username": "user@example.com",
            "password": "pass123",
            "device_id": "did12345",
        },
        "speaker": {
            "ip": "192.168.1.100",
            "did": "1234567890",
            "tts_vendor": "XiaoMi_M88",
        },
        "mac": {"ip": "192.168.1.13"},
        "devices": {
            "main_light": "switch.abc", "ambient_light": "", "speaker_volume": "",
            "speaker_volume_2": "", "ac_temperature": "", "ac_turn_on": "",
            "ac_turn_off": "", "ac_mode": "", "ac_fan_up": "", "ac_fan_down": "",
            "fan_entity": "", "fan_delay_entity": "", "fan_angle_entity": "",
            "camera1_on": "", "camera2_on": "", "vacuum_entity": "",
            "vacuum_mode_entity": "",
        },
        "paths": {
            "node": "/usr/local/bin/node",
            "dsh_checkout": "/path/to/harness",
            "speaker_workspace": "/path/to/ws",
            "speaker_dsh_home": "/path/to/.dsh-speaker",
            "netease_music_cli": "/usr/bin/netease-music",
            "doubao_cli": "/usr/bin/doubao-ask",
            "ego_browser": "/usr/bin/ego-browser",
        },
        "optional": {},
    }


def example_cfg() -> dict:
    with open(os.path.join(REPO_ROOT, "config", "config.example.json"),
              encoding="utf-8") as f:
        return json.load(f)


class AdminServerTest(unittest.TestCase):
    """真实起一个 ThreadingHTTPServer，把 server 的路径常量指到临时目录。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="admin-test-")
        cls.config_dir = os.path.join(cls.tmp, "config")
        cls.static_dir = os.path.join(cls.tmp, "static")
        os.makedirs(cls.config_dir, exist_ok=True)
        os.makedirs(cls.static_dir, exist_ok=True)
        with open(os.path.join(cls.config_dir, "config.example.json"),
                  "w", encoding="utf-8") as f:
            json.dump(example_cfg(), f, ensure_ascii=False, indent=2)
        shutil.copy(REAL_INDEX, os.path.join(cls.static_dir, "index.html"))

        cls._orig = {k: getattr(server, k) for k in (
            "CONFIG_DIR", "LOCAL_CONFIG", "EXAMPLE_CONFIG",
            "GENERATED_DIR", "STATIC_DIR", "ADMIN_TOKEN_FILE")}
        server.CONFIG_DIR = cls.config_dir
        server.LOCAL_CONFIG = os.path.join(cls.config_dir, "local.json")
        server.EXAMPLE_CONFIG = os.path.join(cls.config_dir, "config.example.json")
        server.GENERATED_DIR = os.path.join(cls.config_dir, "generated")
        server.STATIC_DIR = cls.static_dir
        server.ADMIN_TOKEN_FILE = os.path.join(cls.config_dir, "local-admin.token")

        cls.httpd = server.AdminHTTPServer(("127.0.0.1", 0), server.Handler,
                                           admin_token=TEST_TOKEN)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls._orig.items():
            setattr(server, k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------- HTTP 工具 ----------------

    def raw(self, method, path, host=None, headers=None, body=b"", no_cl=False):
        """原始 socket 请求，可完全控制 Host / Content-Length。"""
        hdrs = dict(headers or {})
        if host is not None:
            hdrs["Host"] = host
        if not no_cl and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(body))
        lines = ["%s %s HTTP/1.1" % (method, path)]
        lines += ["%s: %s" % (k, v) for k, v in hdrs.items()]
        req = ("\r\n".join(lines) + "\r\n\r\n").encode("latin1") + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(req)
            resp = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp += chunk
        return resp

    def parse(self, resp):
        head, sep, rest = resp.partition(b"\r\n\r\n")
        self.assertTrue(sep, "响应缺少头部终止符")
        lines = head.decode("latin1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, _, v = ln.partition(":")
                hdrs[k.strip().lower()] = v.strip()
        body = rest[: int(hdrs.get("content-length", "0"))]
        return status, hdrs, body

    def get(self, path, host=None, headers=None):
        return self.parse(self.raw("GET", path, host=host, headers=headers))

    def post(self, path, body=None, host=None, headers=None, no_cl=False):
        if isinstance(body, str):
            payload = body.encode("utf-8")
        elif body is None:
            payload = b"{}"
        else:
            payload = json.dumps(body).encode("utf-8")
        return self.parse(self.raw("POST", path, host=host, headers=headers,
                                   body=payload, no_cl=no_cl))

    @property
    def h(self):
        return "127.0.0.1:%d" % self.port

    def post_cfg(self, cfg, token=TEST_TOKEN, origin=None, host=None):
        headers = {"X-Admin-Token": token, "Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        return self.post("/api/config", json.dumps(cfg), host=host or self.h,
                         headers=headers)

    # ---------------- Host 矩阵 ----------------

    def test_host_evil_domain_rejected(self):
        st, _, _ = self.get("/", host="evil.com")
        self.assertEqual(st, 403)
        st, _, _ = self.get("/api/config", host="evil.com")
        self.assertEqual(st, 403)
        st, _, _ = self.post_cfg(valid_cfg(), host="evil.com")
        self.assertEqual(st, 403)

    def test_host_wrong_port_rejected(self):
        st, _, _ = self.get("/", host="127.0.0.1:9999")
        self.assertEqual(st, 403)
        st, _, _ = self.get("/api/config", host="127.0.0.1:9999")
        self.assertEqual(st, 403)

    def test_host_no_port_rejected(self):
        # 严格：不带端口一律拒绝（无默认 80/443 兜底）
        st, _, _ = self.get("/", host="127.0.0.1")
        self.assertEqual(st, 403)

    def test_host_ok(self):
        st, _, body = self.get("/", host=self.h)
        self.assertEqual(st, 200)
        st, _, body = self.get("/api/config", host=self.h)
        self.assertEqual(st, 200)
        self.assertIn(b"llm", body)

    def test_host_localhost_ok(self):
        st, _, _ = self.get("/", host="localhost:%d" % self.port)
        self.assertEqual(st, 200)
        st, _, _ = self.post_cfg(valid_cfg(), host="localhost:%d" % self.port)
        self.assertEqual(st, 200)

    def test_host_ipv6_literal_ok(self):
        st, _, _ = self.get("/", host="[::1]:%d" % self.port)
        self.assertEqual(st, 200)
        st, _, _ = self.get("/api/config", host="[::1]:%d" % self.port)
        self.assertEqual(st, 200)

    def test_host_ipv6_wrong_port_rejected(self):
        st, _, _ = self.get("/", host="[::1]:9999")
        self.assertEqual(st, 403)

    def test_host_missing_rejected(self):
        st, _, _ = self.get("/", host=None, headers={})
        self.assertEqual(st, 403)

    def test_host_nonloopback_ip_rejected(self):
        st, _, _ = self.get("/", host="192.168.1.13:%d" % self.port)
        self.assertEqual(st, 403)

    # ---------------- Origin 矩阵 ----------------

    def test_origin_evil_rejected(self):
        st, _, body = self.post_cfg(valid_cfg(), origin="http://evil.com")
        self.assertEqual(st, 403)
        self.assertIn("跨站".encode(), body)

    def test_origin_wrong_port_rejected(self):
        st, _, _ = self.post_cfg(valid_cfg(), origin="http://127.0.0.1:9999")
        self.assertEqual(st, 403)

    def test_no_origin_curl_passes(self):
        # 无 Origin 的非浏览器客户端：Host + token 校验通过即放行
        st, _, _ = self.post_cfg(valid_cfg())
        self.assertEqual(st, 200)

    def test_origin_loopback_ok(self):
        st, _, _ = self.post_cfg(valid_cfg(), origin="http://%s" % self.h)
        self.assertEqual(st, 200)
        st, _, _ = self.post_cfg(valid_cfg(),
                                 origin="http://localhost:%d" % self.port)
        self.assertEqual(st, 200)

    def test_origin_https_variant_ok(self):
        st, _, _ = self.post_cfg(valid_cfg(), origin="https://%s" % self.h)
        self.assertEqual(st, 200)

    # ---------------- CSRF token ----------------

    def test_post_without_token_rejected(self):
        st, _, _ = self.post("/api/config", json.dumps(valid_cfg()), host=self.h,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(st, 403)

    def test_post_wrong_token_rejected(self):
        st, _, _ = self.post_cfg(valid_cfg(), token="wrong-token")
        self.assertEqual(st, 403)

    def test_post_with_token_ok(self):
        st, _, _ = self.post_cfg(valid_cfg())
        self.assertEqual(st, 200)

    # ---------------- 配置保存成功 ----------------

    def test_save_success_derived_and_perms(self):
        st, _, _ = self.post_cfg(valid_cfg())
        self.assertEqual(st, 200)
        gen = server.GENERATED_DIR
        expected = [
            "bridge/xiaogpt-credentials", "bridge/xiaogpt-config.yml",
            "bridge/.env", "speaker/config.env", "speaker/system_prompt.txt",
            "dsh-speaker/settings.yaml", "dsh-speaker/dsh-fast.patch.yml",
            "dsh-speaker/cordis.patch.yml", "dsh-speaker/.credentials.yaml",
            "bridge-secret",
        ]
        for rel in expected:
            self.assertTrue(os.path.isfile(os.path.join(gen, rel)),
                            "缺少派生文件 %s" % rel)
        # local.json 0600
        self.assertEqual(os.stat(server.LOCAL_CONFIG).st_mode & 0o777, 0o600)
        # 密钥类派生文件 0600
        for rel in ("bridge/xiaogpt-credentials", "bridge/.env",
                    "speaker/config.env", "bridge-secret",
                    "dsh-speaker/.credentials.yaml"):
            self.assertEqual(os.stat(os.path.join(gen, rel)).st_mode & 0o777, 0o600)
        # local.json 内容含 bridge.secret（32 hex）
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertRegex(saved["bridge"]["secret"], r"^[0-9a-f]{32}$")
        # bridge-secret 兜底文件与 local.json 一致、单行
        with open(os.path.join(gen, "bridge-secret"), encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, saved["bridge"]["secret"] + "\n")
        # 保存两次后 bridge.secret 不变（页面表单不带 bridge，服务端继承）
        st, _, _ = self.post_cfg(valid_cfg())
        self.assertEqual(st, 200)
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            saved2 = json.load(f)
        self.assertEqual(saved2["bridge"]["secret"], saved["bridge"]["secret"])
        # 显式传非法 bridge.secret 会重新生成；合法 32hex 保留
        bad = valid_cfg()
        bad["bridge"] = {"secret": "not-hex!"}
        st, _, _ = self.post_cfg(bad)
        self.assertEqual(st, 200)
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            saved3 = json.load(f)
        self.assertRegex(saved3["bridge"]["secret"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(saved3["bridge"]["secret"], "not-hex!")
        fixed = valid_cfg()
        fixed["bridge"] = {"secret": "a" * 32}
        st, _, _ = self.post_cfg(fixed)
        self.assertEqual(st, 200)
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            saved4 = json.load(f)
        self.assertEqual(saved4["bridge"]["secret"], "a" * 32)

    def test_credentials_roundtrip_special_chars(self):
        cfg = valid_cfg()
        cfg["xiaomi_account"]["username"] = "u'se$r`na\\me"
        cfg["xiaomi_account"]["password"] = "p'as\"s$w\\o`rd"
        st, _, _ = self.post_cfg(cfg)
        self.assertEqual(st, 200)
        path = os.path.join(server.GENERATED_DIR, "bridge/xiaogpt-credentials")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        env = {}
        for line in content.splitlines():
            parts = shlex.split(line)
            for p in parts[1:]:
                if "=" in p:
                    k, _, v = p.partition("=")
                    env[k] = v
        self.assertEqual(env.get("MI_USER"), cfg["xiaomi_account"]["username"])
        self.assertEqual(env.get("MI_PASS"), cfg["xiaomi_account"]["password"])
        self.assertEqual(env.get("MI_DEVICE_ID"), cfg["xiaomi_account"]["device_id"])
        # 文件可被 sh source 且值正确（反引号/$( 不被执行）
        import subprocess
        out = subprocess.run(["sh", "-c",
                              ". '%s'; printf '%%s|%%s' \"$MI_USER\" \"$MI_PASS\""
                              % path],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "%s|%s" % (cfg["xiaomi_account"]["username"],
                                                cfg["xiaomi_account"]["password"]))

    # ---------------- 校验拒绝 ----------------

    def test_newline_password_rejected(self):
        cfg = valid_cfg()
        cfg["xiaomi_account"]["password"] = "abc\ndef"
        st, _, body = self.post_cfg(cfg)
        self.assertEqual(st, 400)
        self.assertIn("控制字符".encode(), body)
        cfg2 = valid_cfg()
        cfg2["xiaomi_account"]["username"] = "a\x07b"
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 400)
        cfg3 = valid_cfg()
        cfg3["xiaomi_account"]["password"] = "x\x00y"
        st, _, _ = self.post_cfg(cfg3)
        self.assertEqual(st, 400)

    def test_bad_secret_charset_rejected(self):
        cfg = valid_cfg()
        cfg["llm"]["api_key"] = "sk-abc;rm -rf /"
        st, _, body = self.post_cfg(cfg)
        self.assertEqual(st, 400)
        self.assertIn("非法字符".encode(), body)
        cfg2 = valid_cfg()
        cfg2["home_assistant"]["token"] = "tok\ninjection"
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 400)

    def test_bad_url_scheme_rejected(self):
        cfg = valid_cfg()
        cfg["llm"]["base_url"] = "file:///etc/passwd"
        st, _, body = self.post_cfg(cfg)
        self.assertEqual(st, 400)
        self.assertIn("http".encode(), body)
        cfg2 = valid_cfg()
        cfg2["home_assistant"]["url"] = "ftp://evil.com"
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 400)

    def test_bad_mac_ip_rejected(self):
        cfg = valid_cfg()
        cfg["mac"]["ip"] = "1.2.3.4;rm -rf"
        st, _, _ = self.post_cfg(cfg)
        self.assertEqual(st, 400)
        cfg2 = valid_cfg()
        cfg2["mac"]["ip"] = "evil host"
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 400)

    def test_bad_device_id_rejected(self):
        cfg = valid_cfg()
        cfg["xiaomi_account"]["device_id"] = "abc;rm"
        st, _, _ = self.post_cfg(cfg)
        self.assertEqual(st, 400)
        cfg2 = valid_cfg()
        cfg2["speaker"]["did"] = '12"3'
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 400)

    def test_missing_section_rejected(self):
        cfg = valid_cfg()
        del cfg["speaker"]
        st, _, body = self.post_cfg(cfg)
        self.assertEqual(st, 400)
        self.assertIn("缺少配置段 speaker".encode(), body)
        cfg2 = valid_cfg()
        cfg2["mac"] = "not-a-dict"
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 400)

    # ---------------- 生成失败回滚 ----------------

    def test_generation_failure_rollback(self):
        st, _, _ = self.post_cfg(valid_cfg())
        self.assertEqual(st, 200)
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            old_local = f.read()
        creds = os.path.join(server.GENERATED_DIR, "bridge/xiaogpt-credentials")
        with open(creds, encoding="utf-8") as f:
            old_creds = f.read()

        orig = server.generate_derived

        def boom(cfg, target_dir=None):
            raise RuntimeError("injected generation failure")

        server.generate_derived = boom
        try:
            bad = valid_cfg()
            bad["mac"]["ip"] = "192.168.1.99"
            st, _, body = self.post_cfg(bad)
            self.assertEqual(st, 500)
            self.assertIn("生成配置失败".encode(), body)
        finally:
            server.generate_derived = orig

        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            self.assertEqual(f.read(), old_local)   # local.json 未被覆盖
        with open(creds, encoding="utf-8") as f:
            self.assertEqual(f.read(), old_creds)   # 派生文件未被部分替换
        # 临时目录已清理
        leftovers = [d for d in os.listdir(server.CONFIG_DIR)
                     if d.startswith(".save-")]
        self.assertEqual(leftovers, [])

    # ---------------- HTTP 细节 ----------------

    def test_negative_content_length_rejected(self):
        st, _, _ = self.post("/api/config", host=self.h,
                             headers={"Content-Length": "-5",
                                      "X-Admin-Token": TEST_TOKEN})
        self.assertEqual(st, 400)

    def test_large_content_length_rejected(self):
        st, _, _ = self.post("/api/config", host=self.h,
                             headers={"Content-Length": str(server.MAX_BODY + 1),
                                      "X-Admin-Token": TEST_TOKEN})
        self.assertEqual(st, 413)

    def test_missing_content_length_rejected(self):
        st, _, _ = self.post("/api/config", host=self.h, no_cl=True,
                             headers={"X-Admin-Token": TEST_TOKEN})
        self.assertEqual(st, 411)

    def test_empty_body_rejected_by_validation(self):
        # CL=0 → body {} → 必填段缺失 → 400（绝不静默清空配置）
        st, _, body = self.post_cfg({})
        self.assertEqual(st, 400)
        self.assertIn("缺少配置段".encode(), body)

    def test_bad_json_rejected(self):
        st, _, _ = self.post_cfg("not-json{")
        self.assertEqual(st, 400)

    # ---------------- 页面与响应头 ----------------

    def test_page_token_and_nonce(self):
        st, hdrs, body = self.get("/", host=self.h)
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("content-type"), "text/html; charset=utf-8")
        # token 注入
        self.assertIn(("window.__ADMIN_TOKEN__=\"%s\"" % TEST_TOKEN).encode(), body)
        # CSP nonce 与页面脚本 nonce 一致，且无 script unsafe-inline
        csp = hdrs.get("content-security-policy", "")
        m = re.search(r"script-src 'self' 'nonce-([A-Za-z0-9_\-]+)'", csp)
        self.assertTrue(m, "CSP 应含 script-src nonce: %s" % csp)
        self.assertIn(('<script nonce="%s">' % m.group(1)).encode(), body)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        # 安全头
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
        self.assertEqual(hdrs.get("referrer-policy"), "no-referrer")
        self.assertEqual(hdrs.get("cache-control"), "no-store")

    def test_api_csp_no_unsafe_inline_script(self):
        st, hdrs, _ = self.get("/api/config", host=self.h)
        self.assertEqual(st, 200)
        csp = hdrs.get("content-security-policy", "")
        self.assertIn("script-src 'self';", csp)

    def test_config_get_sensitive_fields(self):
        self.post_cfg(valid_cfg())
        st, hdrs, body = self.get("/api/config", host=self.h)
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("cache-control"), "no-store")
        cfg = json.loads(body)
        self.assertEqual(cfg["llm"]["api_key"], "sk-test-key-123")
        self.assertEqual(cfg["home_assistant"]["token"], "tok_abcdef0123456789")

    def test_generated_listing(self):
        self.post_cfg(valid_cfg())
        st, _, body = self.get("/api/generated", host=self.h)
        self.assertEqual(st, 200)
        files = json.loads(body)["files"]
        self.assertIn("bridge/xiaogpt-credentials", files)
        self.assertIn("bridge-secret", files)

    # ---------------- optional 继承 ----------------

    def test_optional_preserved(self):
        cfg = valid_cfg()
        cfg["optional"] = {"netease_cookie": "MUSIC_U=abc"}
        st, _, _ = self.post_cfg(cfg)
        self.assertEqual(st, 200)
        # 页面表单不带 optional 键 → 服务端继承上一份
        cfg2 = valid_cfg()
        cfg2["mac"]["ip"] = "192.168.1.14"
        st, _, _ = self.post_cfg(cfg2)
        self.assertEqual(st, 200)
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["optional"]["netease_cookie"], "MUSIC_U=abc")
        # 显式传空 optional（{}）同样继承，防 API 客户端误抹
        cfg3 = valid_cfg()
        cfg3["optional"] = {}
        st, _, _ = self.post_cfg(cfg3)
        self.assertEqual(st, 200)
        with open(server.LOCAL_CONFIG, encoding="utf-8") as f:
            saved3 = json.load(f)
        self.assertEqual(saved3["optional"]["netease_cookie"], "MUSIC_U=abc")

    # ---------------- verify scheme ----------------

    def test_verify_rejects_non_http_scheme(self):
        cfg = valid_cfg()
        cfg["llm"]["base_url"] = "file:///etc/passwd"
        st, _, body = self.post("/api/verify",
                                {"type": "llm", "config": cfg},
                                host=self.h,
                                headers={"X-Admin-Token": TEST_TOKEN,
                                         "Content-Type": "application/json"})
        self.assertEqual(st, 200)
        result = json.loads(body)
        self.assertFalse(result["ok"])
        self.assertIn("http", result["error"])

    def test_verify_requires_token(self):
        st, _, _ = self.post("/api/verify", {"type": "llm"}, host=self.h,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)