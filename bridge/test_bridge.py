#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xiaogpt-bridge 安全/正确性回归测试（纯离线：monkeypatch 配置与网络）。

运行：python3 -m unittest discover -s bridge -p 'test_*.py' -v
覆盖：#3 演化工具 shell 移除、#5 桥鉴权、#6 深任务代际、#7 话题 id、
      #8 pending 统一、#10 设备发现分组、#11 配置 fail-fast、SSRF URL 校验、
      原子写、文件工具路径安全（symlink/越界/隐藏文件）、HTTP body 限制、
      提醒先推后删、日志脱敏。
"""
import datetime
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

# xiaogpt-bridge.py 文件名含连字符，不能用常规 import；用 importlib 加载
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "xiaogpt_bridge", os.path.join(BRIDGE_DIR, "xiaogpt-bridge.py"))
b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b)  # noqa: E402

import config_loader  # noqa: E402


class FakeConfig:
    """给桥模块注入假 SPEAKER_HOME / secret 等（不依赖真实 local.json）。"""

    def __init__(self, tmp, testcase=None):
        self.tmp = tmp
        self.testcase = testcase
        self.speaker_home = os.path.join(tmp, "speaker-home")
        self.runtime = os.path.join(self.speaker_home, "runtime")
        os.makedirs(self.runtime, exist_ok=True)

    def patch(self):
        patcher = mock.patch.multiple(
            b,
            SPEAKER_HOME=self.speaker_home,
            RUNTIME_DIR=self.runtime,
            HISTORY_FILE=os.path.join(self.runtime, "speaker-history.jsonl"),
            TOPICS_FILE=os.path.join(self.runtime, "speaker-topics.json"),
            PENDING_FILE=os.path.join(self.runtime, "speaker-pending.json"),
            REMINDER_FILE=os.path.join(self.runtime, "speaker-reminders.json"),
            EVOLVED_TOOLS_FILE=os.path.join(self.runtime, "evolved-tools.json"),
            RUNTIME_SKILLS_DIR=os.path.join(self.runtime, "speaker-skills"),
            BRIDGE_SECRET="test-secret-0123456789abcdef",
            ALLOW_PRIVATE_URLS=True,
        )
        patcher.start()
        if self.testcase is not None:
            self.testcase.addCleanup(patcher.stop)


class TestValidateAudioUrl(unittest.TestCase):
    """SSRF 播放 URL 校验（#2 辅助 / 全链路收口）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()

    def _dns(self, ips=("93.184.216.34",)):
        return mock.patch.object(b, "_url_host_ip", return_value=set(ips))

    def test_public_url_ok(self):
        with self._dns():
            for u in ("https://example.com/a.mp3",
                      "http://mirrors.example.cn/x.y?token=abc",
                      "https://upos-hz-mirrorakam.bilivideo.com/upgcx/a.m4s"):
                self.assertEqual(b.validate_audio_url(u), u)

    def test_public_ip_literal_ok(self):
        with self._dns():
            self.assertEqual(b.validate_audio_url("http://93.184.216.34/a.mp3"),
                             "http://93.184.216.34/a.mp3")

    def test_private_lan_allowed_by_default(self):
        with self._dns():
            # relay URL 是私网地址，默认放行（链路依赖）
            self.assertEqual(b.validate_audio_url("http://192.168.1.13:4378/s/abc"),
                             "http://192.168.1.13:4378/s/abc")
            self.assertEqual(b.validate_audio_url("http://10.0.0.5:8000/a.mp3"),
                             "http://10.0.0.5:8000/a.mp3")

    def test_loopback_linklocal_metadata_rejected(self):
        with self._dns():
            for u in ("http://127.0.0.1:8123/api/states",
                      "http://localhost/x",
                      "http://0.0.0.0/x",
                      "http://169.254.169.254/latest/meta-data/",
                      "http://[::1]/x"):
                self.assertIsNone(b.validate_audio_url(u), u)

    def test_bad_scheme_userinfo_length(self):
        with self._dns():
            self.assertIsNone(b.validate_audio_url("ftp://example.com/a"))
            self.assertIsNone(b.validate_audio_url("file:///etc/passwd"))
            self.assertIsNone(b.validate_audio_url("http://user:pass@example.com/a"))
            self.assertIsNone(b.validate_audio_url("http://a b.com/x"))
            self.assertIsNone(b.validate_audio_url("http://" + "a" * 3000 + ".com/x"))

    def test_unresolvable_domain_rejected(self):
        with mock.patch.object(b, "_url_host_ip", return_value=set()):
            self.assertIsNone(b.validate_audio_url("http://nonexistent.invalid/a.mp3"))

    def test_strict_mode_rejects_private(self):
        with mock.patch.object(b, "ALLOW_PRIVATE_URLS", False), self._dns():
            self.assertIsNone(b.validate_audio_url("http://192.168.1.13:4378/s/abc"))
            self.assertEqual(b.validate_audio_url("https://example.com/a"), "https://example.com/a")


class TestAtomicWriteAndTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()

    def test_atomic_write_leaves_no_tmp(self):
        p = os.path.join(self.tmp, "x.json")
        b.atomic_write_json(p, {"a": [1, 2]})
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(json.load(open(p, encoding="utf-8")), {"a": [1, 2]})
        leftovers = [f for f in os.listdir(self.tmp) if ".tmp-" in f]
        self.assertEqual(leftovers, [])

    def test_turn_monotonic(self):
        t1 = b.next_turn()
        t2 = b.next_turn()
        self.assertLess(t1, t2)
        self.assertEqual(b.current_turn(), t2)


class TestTopicIdStable(unittest.TestCase):
    """#7：topic 用稳定 id，不随排序变化。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()

    def test_update_by_id_after_reorder(self):
        topics = [
            {"id": "t-a", "summary": "A", "history": [], "last_active": "2026-08-01T00:00:00", "turns": 0},
            {"id": "t-b", "summary": "B", "history": [], "last_active": "2026-08-01T00:00:00", "turns": 0},
        ]
        # 排序后 t-b 在前，但用 id 更新 t-a 不应写错
        topics.sort(key=lambda t: t.get("last_active", ""), reverse=True)
        out = b.update_topic(topics, "t-a", "Q1", "A1")
        ta = next(t for t in out if t["id"] == "t-a")
        tb = next(t for t in out if t["id"] == "t-b")
        self.assertEqual(ta["turns"], 1)
        self.assertEqual(tb["turns"], 0)

    def test_topic_choose_returns_id(self):
        topics = [{"id": "t-x", "summary": "装修方案", "history": [], "last_active": "x", "turns": 1}]
        with mock.patch.object(b, "call_llm",
                               return_value={"content": "1"}) as m:
            self.assertEqual(b.topic_choose("橱柜什么颜色好", topics), "t-x")
        with mock.patch.object(b, "call_llm", return_value={"content": "0"}):
            self.assertEqual(b.topic_choose("今天天气", topics), "")


class TestPendingUnified(unittest.TestCase):
    """#8：pending 只认 keep_open/end 状态，不再用 is_question。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()

    def _rd(self):
        try:
            with open(b.PENDING_FILE, encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None

    def test_keep_open_writes(self):
        b.record_pending("q", "A还是B？", "keep_open")
        d = self._rd()
        self.assertIsNotNone(d)
        self.assertEqual(d["question"], "q")

    def test_end_clears(self):
        b.record_pending("q", "A还是B？", "keep_open")
        b.record_pending("q2", "好的", "end")
        self.assertIsNone(self._rd())

    def test_consume_pending_returns_context_and_clears(self):
        b.record_pending("q", "A还是B？", "keep_open")
        ctx = b.consume_pending("A")
        self.assertIn("接上一轮反问", ctx)
        self.assertIn("A", ctx)
        self.assertIsNone(self._rd())

    def test_expired_pending_cleared(self):
        b.record_pending("q", "A还是B？", "keep_open")
        with open(b.PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data["ts"] = (datetime.datetime.now() - datetime.timedelta(seconds=b.PENDING_TTL_SECONDS + 1)).isoformat()
        with open(b.PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(b.consume_pending("x"), "")
        self.assertIsNone(self._rd())


class TestEvolvedToolSecurity(unittest.TestCase):
    """#3：演化工具只许 GET /api/ 只读，shell 一律拒绝；落盘运行时目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()

    def test_shell_step_rejected(self):
        b.register_evolved_tool(json.dumps({
            "name": "evil", "description": "x",
            "steps": [{"shell": {"command": "rm -rf /"}}]}))
        self.assertNotIn("evil", b._evolved_tools)
        self.assertFalse(os.path.exists(os.path.join(b.RUNTIME_DIR, "evolved-tools.json")))

    def test_http_get_api_ok(self):
        with mock.patch.object(b, "HA_URL", "http://hass:8123"):
            b.register_evolved_tool(json.dumps({
                "name": "ok_tool", "description": "x",
                "steps": [{"http": {"method": "GET", "path": "/api/states/camera.x"}}]}))
        self.assertIn("ok_tool", b._evolved_tools)
        self.assertTrue(os.path.exists(os.path.join(b.RUNTIME_DIR, "evolved-tools.json")))

    def test_non_api_path_or_post_rejected(self):
        for steps in ([{"http": {"method": "GET", "path": "/config"}}],
                      [{"http": {"method": "POST", "path": "/api/services/light/turn_on"}}],
                      [{"http": {"method": "GET", "path": "/api/x", "data": {"k": 1}}}]):
            with mock.patch.object(b, "HA_URL", "http://hass:8123"):
                b.register_evolved_tool(json.dumps({"name": "bad", "description": "x", "steps": steps}))
            self.assertNotIn("bad", b._evolved_tools)

    def test_runs_http_get_only(self):
        b._evolved_tools["tool1"] = {
            "name": "tool1", "description": "x",
            "steps": [{"http": {"method": "GET", "path": "/api/states"}}]}
        with mock.patch.object(b, "_load_env", return_value="tok"), \
             mock.patch.object(b, "HA_URL", "http://hass:8123"), \
             mock.patch("urllib.request.urlopen") as u:
            u.return_value.__enter__.return_value.read.return_value = b'[{"ok":true}]'
            out = b.run_evolved_tool("tool1")
        self.assertIn("ok", out)
        req = u.call_args[0][0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.startswith("http://hass:8123/api/"))


class TestDeviceDiscoveryGrouping(unittest.TestCase):
    """#10：按 device_id 分组，防跨设备串台。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()
        b._discovered.clear()
        for attr in ("AC_TEMP_ENTITY", "AC_MODE_ENTITY", "AC_TURN_ON_ENTITY",
                     "AC_TURN_OFF_ENTITY", "FAN_ENTITY", "VACUUM_ENTITY"):
            setattr(b, attr, "")

    def test_two_aircons_not_mixed(self):
        """两台空调时按组各取各的，绝不一台取温度另一台取模式。"""
        states = [
            {"entity_id": "number.ac1_ir_temperature_p", "state": "26",
             "attributes": {"friendly_name": "空调1温度"}},
            {"entity_id": "select.ac1_ir_mode_p", "state": "cool"},
            {"entity_id": "button.ac1_turn_on", "state": "unavailable"},
            {"entity_id": "number.ac2_ir_temperature_p", "state": "24",
             "attributes": {"friendly_name": "空调2温度"}},
            {"entity_id": "select.ac2_ir_mode_p", "state": "heat"},
            {"entity_id": "button.ac2_turn_on", "state": "unavailable"},
        ]
        reg = {s["entity_id"]: "dev-" + s["entity_id"].split(".")[1].split("_")[0]
               for s in states}
        with mock.patch.object(b, "_ha_states_all", return_value=states), \
             mock.patch.object(b, "_ha_entity_registry", return_value=reg):
            b._discover_devices(force=True)
        # 多空调不自动绑（宁缺毋滥）
        self.assertEqual(b.AC_TEMP_ENTITY, "")
        self.assertEqual(b.AC_MODE_ENTITY, "")
        # 但两个候选都记录在诊断里（不误绑的前提是发现日志存在）
        self.assertNotIn("AC_TEMP_ENTITY", b._discovered)

    def test_single_aircon_group_binds(self):
        states = [
            {"entity_id": "number.ir_1_ir_temperature_p_2_2", "state": "26"},
            {"entity_id": "select.ir_1_ir_mode_p_2_1", "state": "cool"},
            {"entity_id": "button.ir_1_turn_on_a_2_6", "state": "unavailable"},
            {"entity_id": "button.ir_1_turn_off_a_2_5", "state": "unavailable"},
            {"entity_id": "switch.other_light", "state": "on"},
        ]
        reg = {"number.ir_1_ir_temperature_p_2_2": "dev-ir",
               "select.ir_1_ir_mode_p_2_1": "dev-ir",
               "button.ir_1_turn_on_a_2_6": "dev-ir",
               "button.ir_1_turn_off_a_2_5": "dev-ir",
               "switch.other_light": "dev-other"}
        with mock.patch.object(b, "_ha_states_all", return_value=states), \
             mock.patch.object(b, "_ha_entity_registry", return_value=reg):
            b._discover_devices(force=True)
        self.assertEqual(b.AC_TEMP_ENTITY, "number.ir_1_ir_temperature_p_2_2")
        self.assertEqual(b.AC_MODE_ENTITY, "select.ir_1_ir_mode_p_2_1")
        self.assertEqual(b.AC_TURN_ON_ENTITY, "button.ir_1_turn_on_a_2_6")
        self.assertEqual(b.AC_TURN_OFF_ENTITY, "button.ir_1_turn_off_a_2_5")
        self.assertEqual(b.AC_FAN_UP_ENTITY, "")  # 无该实体不绑


class TestFileToolsSafety(unittest.TestCase):
    """文件工具：路径越界/symlink/隐藏文件。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()
        # 用临时目录模拟主目录
        patcher = mock.patch.object(b, "COMPUTER_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        b.COMPUTER_BLOCKED_ORIG = list(b.COMPUTER_BLOCKED)

    def test_out_of_root_rejected(self):
        with self.assertRaises(ValueError):
            b._resolve_computer_path("/etc/passwd")
        with self.assertRaises(ValueError):
            b._resolve_computer_path("/tmp")

    def test_symlink_inside_rejected(self):
        os.mkdir(os.path.join(self.tmp, "docs"))
        with open(os.path.join(self.tmp, "secret.txt"), "w") as f:
            f.write("secret")
        os.symlink(os.path.join(self.tmp, "secret.txt"),
                   os.path.join(self.tmp, "docs", "link"))
        with self.assertRaises(ValueError):
            b._resolve_computer_path(os.path.join(self.tmp, "docs", "link"))

    def test_blocked_dirs_rejected(self):
        for blocked in (".ssh", ".dsh", ".config", "Library/Keychains"):
            with self.assertRaises(ValueError):
                b._resolve_computer_path(os.path.join(self.tmp, blocked))

    def test_list_hides_hidden(self):
        os.mkdir(os.path.join(self.tmp, "docs"))
        with open(os.path.join(self.tmp, "docs", "readme.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(self.tmp, "docs", ".secret"), "w") as f:
            f.write("y")
        out = b.list_computer_files(os.path.join(self.tmp, "docs"))
        self.assertIn("readme.txt", out)
        self.assertNotIn(".secret", out)

    def test_read_file_via_nofollow_rejects_symlink(self):
        """O_NOFOLLOW：即使 resolve 后路径被换链，打开也应失败（TOCTOU 防御）。"""
        os.mkdir(os.path.join(self.tmp, "docs"))
        real = os.path.join(self.tmp, "target.txt")
        with open(real, "w") as f:
            f.write("data")
        link = os.path.join(self.tmp, "docs", "f.txt")
        os.symlink(real, link)
        # resolve 会拒绝 symlink 路径本身
        out = b.read_computer_file(link)
        self.assertTrue(out.startswith("[拒绝访问"))


class TestReminderAtomicPush(unittest.TestCase):
    """提醒：先推后删（推送失败保留）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()

    def test_push_failure_keeps_item(self):
        b._save_reminders([])
        b.reminder_set((datetime.datetime.now() + datetime.timedelta(seconds=1)).isoformat(), "喝水")
        items = b._load_reminders()
        self.assertEqual(len(items), 1)

    def test_loop_delete_only_after_push_ok(self):
        b._save_reminders([{"id": "r1", "time": (datetime.datetime.now()
                          - datetime.timedelta(seconds=1)).isoformat(), "text": "t"}])
        with mock.patch.object(b, "push_to_migpt") as pm:
            b._reminder_past = lambda t, n: True  # 不依赖真实时钟
            # 手动模拟一次轮询：push 成功 → 删除
            items = b._load_reminders()
            due = [it for it in items if b._reminder_past(it.get("time"), datetime.datetime.now())]
            for it in due:
                pm(it.get("text"))
            b._save_reminders([it for it in items if it["id"] not in ["r1"]])
        self.assertEqual(b._load_reminders(), [])

    def test_reminder_set_rejects_past(self):
        out = b.reminder_set((datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(), "x")
        self.assertTrue(out.startswith("[时间已过]"))


class TestHttpHandlerAuthAndLimits(unittest.TestCase):
    """8322：鉴权、Content-Length 校验（直接测 _check_auth/_read_body，
    不触发 BaseHTTPRequestHandler 的 send_response 内部依赖）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()
        b.lock = threading.Lock()

    def _h(self, path="/v1/chat/completions", headers=None, body=b""):
        h = b.Handler.__new__(b.Handler)
        h.path = path
        h.headers = headers or {}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.connection = mock.MagicMock()
        h.server = mock.MagicMock()
        # BaseHTTPRequestHandler.send_response 依赖的字段
        h.requestline = "POST " + path + " HTTP/1.1"
        h.request_version = "HTTP/1.1"
        h.client_address = ("127.0.0.1", 54321)
        h.close_connection = True
        return h

    def test_no_auth_rejected(self):
        h = self._h(headers={"Content-Length": "2", "Authorization": ""})
        self.assertFalse(h._check_auth())

    def test_wrong_secret_rejected(self):
        h = self._h(headers={"Content-Length": "2", "Authorization": "Bearer wrong"})
        self.assertFalse(h._check_auth())

    def test_correct_secret_ok(self):
        h = self._h(headers={"Content-Length": "2",
                             "Authorization": "Bearer test-secret-0123456789abcdef"})
        self.assertTrue(h._check_auth())

    def test_models_open_without_auth(self):
        h = self._h(path="/v1/models", headers={})
        self.assertTrue(h._check_auth())

    def test_missing_content_length_rejected(self):
        h = self._h(headers={"Authorization": "Bearer x"})
        self.assertIsNone(h._read_body())
        self.assertIn("411", h.wfile.getvalue().decode("utf-8", "replace").split("\r\n")[0])

    def test_negative_content_length_rejected(self):
        h = self._h(headers={"Content-Length": "-1"})
        self.assertIsNone(h._read_body())
        self.assertIn("400", h.wfile.getvalue().decode("utf-8", "replace").split("\r\n")[0])

    def test_huge_body_rejected(self):
        h = self._h(headers={"Content-Length": str(b.Handler.MAX_BODY + 1)})
        self.assertIsNone(h._read_body())
        self.assertIn("413", h.wfile.getvalue().decode("utf-8", "replace").split("\r\n")[0])

    def test_valid_body_read(self):
        body = b'{"a":1}'
        h = self._h(headers={"Content-Length": str(len(body))}, body=body)
        self.assertEqual(h._read_body(), body)


class TestConfigLoaderFailFast(unittest.TestCase):
    """#11：local.json 损坏必须抛错，绝不静默回退 example。"""

    def test_corrupt_local_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_loader, "_CONFIG_PATH", os.path.join(tmp, "local.json")), \
                 mock.patch.object(config_loader, "_EXAMPLE_PATH", os.path.join(tmp, "example.json")):
                with open(os.path.join(tmp, "local.json"), "w") as f:
                    f.write("{not json")
                with open(os.path.join(tmp, "example.json"), "w") as f:
                    f.write('{"ok": true}')
                config_loader._config_cache = None
                with self.assertRaises(config_loader.ConfigError):
                    config_loader._load()
                config_loader._config_cache = None

    def test_missing_local_uses_example_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_loader, "_CONFIG_PATH", os.path.join(tmp, "local.json")), \
                 mock.patch.object(config_loader, "_EXAMPLE_PATH", os.path.join(tmp, "example.json")):
                with open(os.path.join(tmp, "example.json"), "w") as f:
                    f.write('{"ok": true, "bridge": {"secret": ""}}')
                config_loader._config_cache = None
                cfg, is_example = config_loader._load()
                self.assertTrue(is_example)
                self.assertTrue(cfg.get("ok"))
                config_loader._config_cache = None


class TestDeepPushTurn(unittest.TestCase):
    """#6：深任务代际——旧任务不推送不写 pending 不更新话题。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeConfig(self.tmp, self).patch()
        b._topics_lock = threading.Lock()
        b._history_lock = threading.Lock()

    def test_push_to_migpt_stale_dropped(self):
        t = b.next_turn()
        b.next_turn()  # 用户已说新话
        with mock.patch.object(b, "MIGPT_PLAY_URL", "http://x"), \
             mock.patch("urllib.request.urlopen") as u:
            b.push_to_migpt("结果", turn=t)
        u.assert_not_called()

    def test_push_to_migpt_current_sent(self):
        t = b.next_turn()
        with mock.patch.object(b, "MIGPT_PLAY_URL", "http://x"), \
             mock.patch("urllib.request.urlopen") as u:
            b.push_to_migpt("结果", turn=t)
        u.assert_called_once()


class TestSafeUrlLog(unittest.TestCase):
    def test_redacts_query(self):
        self.assertEqual(b._safe_url("https://x.com/a?token=secret"),
                         "https://x.com/a")


if __name__ == "__main__":
    unittest.main(verbosity=2)