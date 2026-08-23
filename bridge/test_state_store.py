#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_store 模块回归测试：原子写与 turn 代际。

运行：python3 -m unittest discover -s bridge -p 'test_*.py' -v
（原 test_bridge.py 的 TestAtomicWriteAndTurn 迁移至此。）
"""
import json
import os
import sys
import tempfile
import unittest

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

import state_store  # noqa: E402


class TestAtomicWriteAndTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_atomic_write_leaves_no_tmp(self):
        p = os.path.join(self.tmp, "x.json")
        state_store.atomic_write_json(p, {"a": [1, 2]})
        self.assertTrue(os.path.isfile(p))
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": [1, 2]})
        leftovers = [f for f in os.listdir(self.tmp) if ".tmp-" in f]
        self.assertEqual(leftovers, [])

    def test_atomic_write_text_creates_dirs(self):
        p = os.path.join(self.tmp, "nested", "dir", "x.txt")
        state_store.atomic_write_text(p, "hi")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hi")

    def test_turn_monotonic(self):
        t1 = state_store.next_turn()
        t2 = state_store.next_turn()
        self.assertLess(t1, t2)
        self.assertEqual(state_store.current_turn(), t2)

    def test_turn_thread_safe(self):
        """并发 next_turn 不重复（线程安全）。"""
        seen = set()
        lock = __import__("threading").Lock()

        def worker():
            for _ in range(50):
                t = state_store.next_turn()
                with lock:
                    seen.add(t)

        import threading
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(seen), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
