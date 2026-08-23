#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程内状态基础设施：原子写文件、JSON 状态文件、turn 代际计数。

职责：
- atomic_write_text / atomic_write_json：原子写（tmp + fsync + os.replace，
  防 crash 留半文件）；
- next_turn / current_turn：线程安全的单调 turn_id（深任务代际判定用）；
- state_lock：全局状态文件互斥锁（话题/待答复等共用）。

不负责：
- 不关心具体状态文件的格式与业务语义（话题/提醒/待答复的读写见 topic_state）；
- 不做任何网络/HTTP 操作。

依赖：仅标准库（os / json / threading）。
"""
import json
import os
import threading

# 话题/待答复/提醒等状态文件的全局互斥
state_lock = threading.Lock()


def atomic_write_text(path: str, content: str) -> None:
    """原子写文本：同目录 tmp + fsync + os.replace（同文件系统 rename 原子）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp-" + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: str, obj) -> None:
    """原子写 JSON（indent=2，ensure_ascii=False）。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# 对话代际（turn_id）：进程内单调递增，深任务捕获自己的代际。
# 判定「深任务结果是否仍属于当前轮次」用进程内计数器，绝不读历史文件猜——
# 历史文件会被旧任务的 record_history 覆盖，导致更晚的 Q2 被错误判过期。

_turn_seq = 0
_turn_seq_lock = threading.Lock()


def next_turn() -> int:
    """每次新用户轮次分配一个单调递增的 turn_id（线程安全）。"""
    global _turn_seq
    with _turn_seq_lock:
        _turn_seq += 1
        return _turn_seq


def current_turn() -> int:
    """当前最新 turn_id（后台深任务完成时用它判断自己是否仍 relevant）。"""
    with _turn_seq_lock:
        return _turn_seq
