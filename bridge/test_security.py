#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""security 模块回归测试：播放 URL 校验（SSRF 防护）与日志脱敏。

运行：python3 -m unittest discover -s bridge -p 'test_*.py' -v
（原 test_bridge.py 的 TestValidateAudioUrl / TestSafeUrlLog 迁移至此。）
"""
import os
import sys
import unittest
from unittest import mock

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

import security  # noqa: E402


class TestValidateAudioUrl(unittest.TestCase):
    """SSRF 播放 URL 校验（#2 辅助 / 全链路收口）。"""

    def setUp(self):
        security.ALLOW_PRIVATE_URLS = True

    def _dns(self, ips=("93.184.216.34",)):
        return mock.patch.object(security, "_url_host_ip", return_value=set(ips))

    def test_public_url_ok(self):
        with self._dns():
            for u in ("https://example.com/a.mp3",
                      "http://mirrors.example.cn/x.y?token=abc",
                      "https://upos-hz-mirrorakam.bilivideo.com/upgcx/a.m4s"):
                self.assertEqual(security.validate_audio_url(u), u)

    def test_public_ip_literal_ok(self):
        with self._dns():
            self.assertEqual(security.validate_audio_url("http://93.184.216.34/a.mp3"),
                             "http://93.184.216.34/a.mp3")

    def test_private_lan_allowed_by_default(self):
        with self._dns():
            # relay URL 是私网地址，默认放行（链路依赖）
            self.assertEqual(security.validate_audio_url("http://192.168.1.13:4378/s/abc"),
                             "http://192.168.1.13:4378/s/abc")
            self.assertEqual(security.validate_audio_url("http://10.0.0.5:8000/a.mp3"),
                             "http://10.0.0.5:8000/a.mp3")

    def test_loopback_linklocal_metadata_rejected(self):
        with self._dns():
            for u in ("http://127.0.0.1:8123/api/states",
                      "http://localhost/x",
                      "http://0.0.0.0/x",
                      "http://169.254.169.254/latest/meta-data/",
                      "http://[::1]/x"):
                self.assertIsNone(security.validate_audio_url(u), u)

    def test_bad_scheme_userinfo_length(self):
        with self._dns():
            self.assertIsNone(security.validate_audio_url("ftp://example.com/a"))
            self.assertIsNone(security.validate_audio_url("file:///etc/passwd"))
            self.assertIsNone(security.validate_audio_url("http://user:pass@example.com/a"))
            self.assertIsNone(security.validate_audio_url("http://a b.com/x"))
            self.assertIsNone(security.validate_audio_url("http://" + "a" * 3000 + ".com/x"))

    def test_unresolvable_domain_rejected(self):
        with mock.patch.object(security, "_url_host_ip", return_value=set()):
            self.assertIsNone(security.validate_audio_url("http://nonexistent.invalid/a.mp3"))

    def test_strict_mode_rejects_private(self):
        with mock.patch.object(security, "ALLOW_PRIVATE_URLS", False), self._dns():
            self.assertIsNone(security.validate_audio_url("http://192.168.1.13:4378/s/abc"))
            self.assertEqual(security.validate_audio_url("https://example.com/a"),
                             "https://example.com/a")


class TestSafeUrlLog(unittest.TestCase):
    def test_redacts_query(self):
        self.assertEqual(security.safe_url("https://x.com/a?token=secret"),
                         "https://x.com/a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
