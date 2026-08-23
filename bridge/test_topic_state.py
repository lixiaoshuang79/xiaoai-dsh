#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""topic_state 模块回归测试：话题档案 / 待答复 / 会话历史持久化。

运行：python3 -m unittest discover -s bridge -p 'test_*.py' -v
（原 test_bridge.py 的 TestTopicIdStable / TestPendingUnified 迁移至此；
 topic_choose/topic_summarize 的 LLM 编排测试仍留在 test_bridge.py。）
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

import topic_state  # noqa: E402


class TopicStateTestCase(unittest.TestCase):
    """把 topic_state 重配到临时目录（每测试隔离）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        topic_state.configure(
            history_file=os.path.join(self.tmp, "speaker-history.jsonl"),
            topics_file=os.path.join(self.tmp, "speaker-topics.json"),
            pending_file=os.path.join(self.tmp, "speaker-pending.json"),
            session_root=os.path.join(self.tmp, "sessions"),
        )


class TestTopicIdStable(TopicStateTestCase):
    """#7：topic 用稳定 id，不随排序变化。"""

    def test_update_by_id_after_reorder(self):
        topics = [
            {"id": "t-a", "summary": "A", "history": [], "last_active": "2026-08-01T00:00:00", "turns": 0},
            {"id": "t-b", "summary": "B", "history": [], "last_active": "2026-08-01T00:00:00", "turns": 0},
        ]
        # 排序后 t-b 在前，但用 id 更新 t-a 不应写错
        topics.sort(key=lambda t: t.get("last_active", ""), reverse=True)
        out = topic_state.update_topic(topics, "t-a", "Q1", "A1")
        ta = next(t for t in out if t["id"] == "t-a")
        tb = next(t for t in out if t["id"] == "t-b")
        self.assertEqual(ta["turns"], 1)
        self.assertEqual(tb["turns"], 0)

    def test_new_topic_created_when_empty_id(self):
        topics = []
        out = topic_state.update_topic(topics, "", "Q", "A")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["id"].startswith("t-"))
        self.assertEqual(out[0]["turns"], 1)

    def test_summarize_fn_injected(self):
        """update_topic 的摘要通过 summarize_fn 注入（主桥传 Flash 摘要）。"""
        topics = [{"id": "t-a", "summary": "旧", "history": ["问：q\n答：a"],
                   "last_active": "2026-08-01T00:00:00", "turns": 1}]
        out = topic_state.update_topic(topics, "t-a", "Q2", "A2",
                                       summarize_fn=lambda hist: f"摘要{len(hist)}")
        self.assertEqual(out[0]["summary"], "摘要2")
        self.assertEqual(out[0]["turns"], 2)

    def test_unknown_id_creates_new(self):
        topics = []
        out = topic_state.update_topic(topics, "t-gone", "Q", "A")
        self.assertEqual(len(out), 1)
        self.assertNotEqual(out[0]["id"], "t-gone")


class TestPendingUnified(TopicStateTestCase):
    """#8：pending 只认 keep_open/end 状态，不再用 is_question。"""

    def _rd(self):
        try:
            with open(topic_state.PENDING_FILE, encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None

    def test_keep_open_writes(self):
        topic_state.record_pending("q", "A还是B？", "keep_open")
        d = self._rd()
        self.assertIsNotNone(d)
        self.assertEqual(d["question"], "q")

    def test_end_clears(self):
        topic_state.record_pending("q", "A还是B？", "keep_open")
        topic_state.record_pending("q2", "好的", "end")
        self.assertIsNone(self._rd())

    def test_consume_pending_returns_context_and_clears(self):
        topic_state.record_pending("q", "A还是B？", "keep_open")
        ctx = topic_state.consume_pending("A")
        self.assertIn("接上一轮反问", ctx)
        self.assertIn("A", ctx)
        self.assertIsNone(self._rd())

    def test_expired_pending_cleared(self):
        topic_state.record_pending("q", "A还是B？", "keep_open")
        with open(topic_state.PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data["ts"] = (datetime.datetime.now()
                      - datetime.timedelta(seconds=topic_state.PENDING_TTL_SECONDS + 1)).isoformat()
        with open(topic_state.PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(topic_state.consume_pending("x"), "")
        self.assertIsNone(self._rd())


class TestHistoryAppend(TopicStateTestCase):
    def test_record_history_appends_jsonl(self):
        topic_state.record_history("q1", "a1", "fast")
        topic_state.record_history("q2", "a2", "deep")
        with open(topic_state.HISTORY_FILE, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["question"], "q1")
        self.assertEqual(first["mode"], "fast")


class TestDailyCleanup(TopicStateTestCase):
    def test_cleanup_old_topics_removes_stale(self):
        topics = [
            {"id": "old", "summary": "x", "last_active": "2020-01-01T00:00:00"},
            {"id": "new", "summary": "y", "last_active": datetime.datetime.now().isoformat()},
        ]
        kept = topic_state.cleanup_old_topics(topics)
        self.assertEqual([t["id"] for t in kept], ["new"])

    def test_cleanup_old_sessions_removes_stale_dirs(self):
        import time
        root = os.path.join(self.tmp, "sessions")
        old = os.path.join(root, "proj", "old-sess")
        new = os.path.join(root, "proj", "new-sess")
        os.makedirs(old)
        os.makedirs(new)
        old_time = time.time() - (topic_state.TOPIC_MAX_AGE_DAYS + 2) * 86400
        os.utime(old, (old_time, old_time))
        topic_state.cleanup_old_sessions()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))


if __name__ == "__main__":
    unittest.main(verbosity=2)
