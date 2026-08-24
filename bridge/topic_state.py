#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""话题档案 / 待答复 / 会话历史 的持久化与状态逻辑（纯文件操作，无 LLM）。

职责：
- 话题：load/save、过期清理、按 id 更新（update_topic）、上下文文本生成；
- 待答复：record/clear/consume（keep_open/end 语义 + TTL 过期）；
- 历史：record_history（append 到 speaker-history.jsonl）；
- 会话文件清理：cleanup_old_sessions / daily_cleanup（每天最多一次）。

不负责：
- 不调用大模型：话题相关性判定（topic_choose）与摘要生成（topic_summarize）
  留在 xiaogpt-bridge（本质是 LLM 编排）；update_topic 的摘要通过
  summarize_fn 参数注入；
- 不读配置：路径由 configure() 注入（主桥启动时调用一次）。

依赖：state_store（原子写 / 全局锁）、标准库。
"""
import datetime
import json
import os
import shutil
import threading
import time

from state_store import atomic_write_json, state_lock

# 运行时路径与参数（主桥启动时 configure() 注入；测试可重配到临时目录）
HISTORY_FILE = ""
TOPICS_FILE = ""
PENDING_FILE = ""
SESSION_ROOT = ""  # DSH 音箱 home 的 sessions 目录
TOPIC_MAX_AGE_DAYS = 7
TOPIC_MAX_ACTIVE = 5    # 判定话题关联时最多参考最近几个话题
TOPIC_HISTORY_ROUNDS = 3  # 继续话题时注入最近几轮问答
PENDING_TTL_SECONDS = 10 * 60

_history_lock = threading.Lock()
topics_lock = threading.Lock()  # 主桥持此锁保护「load→判定→update→save」复合操作
_last_cleanup_day = None


def configure(*, history_file: str, topics_file: str, pending_file: str,
              session_root: str, topic_max_age_days: int = 7,
              topic_max_active: int = 5, topic_history_rounds: int = 3,
              pending_ttl_seconds: int = 600) -> None:
    """注入运行时路径与参数（主桥启动时调用；测试重配到临时目录）。"""
    global HISTORY_FILE, TOPICS_FILE, PENDING_FILE, SESSION_ROOT
    global TOPIC_MAX_AGE_DAYS, TOPIC_MAX_ACTIVE, TOPIC_HISTORY_ROUNDS
    global PENDING_TTL_SECONDS
    HISTORY_FILE = history_file
    TOPICS_FILE = topics_file
    PENDING_FILE = pending_file
    SESSION_ROOT = session_root
    TOPIC_MAX_AGE_DAYS = topic_max_age_days
    TOPIC_MAX_ACTIVE = topic_max_active
    TOPIC_HISTORY_ROUNDS = topic_history_rounds
    PENDING_TTL_SECONDS = pending_ttl_seconds


def record_history(question: str, answer: str, mode: str) -> None:
    """把音箱问答写入桥自己的历史文件（不进用户的 DSH 会话列表）。
    加锁保证并发 append 不交错（后台深任务与前台流式可能同时写）。"""
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,          # fast / deep
        "question": question,
        "answer": answer[:500],
    }
    with _history_lock:
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_topics() -> list:
    try:
        with open(TOPICS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_topics(topics: list) -> None:
    """原子写话题档案（tmp+fsync+rename，防 crash 留半文件）。"""
    try:
        atomic_write_json(TOPICS_FILE, topics)
    except OSError:
        pass


def cleanup_old_topics(topics: list) -> list:
    """删除超过 TOPIC_MAX_AGE_DAYS 未活跃的话题档案（记忆已在 Memory Evolve 落盘）。"""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=TOPIC_MAX_AGE_DAYS)
    kept = []
    removed = 0
    for t in topics:
        try:
            last = datetime.datetime.fromisoformat(t.get("last_active", ""))
        except ValueError:
            removed += 1
            continue
        if last < cutoff:
            removed += 1
            continue
        kept.append(t)
    if removed:
        print(f"[bridge] 话题清理: 删除 {removed} 个过期话题", flush=True)
    return kept


def cleanup_old_sessions() -> None:
    """删除 DSH 音箱 home 下超过 TOPIC_MAX_AGE_DAYS 的 headless 会话文件（防爆炸）。"""
    cutoff = time.time() - TOPIC_MAX_AGE_DAYS * 86400
    root = SESSION_ROOT
    removed = 0
    try:
        for proj in os.listdir(root):
            proj_path = os.path.join(root, proj)
            if not os.path.isdir(proj_path):
                continue
            for sess in os.listdir(proj_path):
                sess_path = os.path.join(proj_path, sess)
                try:
                    if os.path.isdir(sess_path) and os.path.getmtime(sess_path) < cutoff:
                        shutil.rmtree(sess_path, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        pass
    if removed:
        print(f"[bridge] 会话清理: 删除 {removed} 个过期会话", flush=True)


def daily_cleanup() -> None:
    """每天最多执行一次过期清理（话题档案 + headless 会话文件）。"""
    global _last_cleanup_day
    today = datetime.date.today()
    with topics_lock:
        if _last_cleanup_day == today:
            return
        _last_cleanup_day = today
        save_topics(cleanup_old_topics(load_topics()))
    cleanup_old_sessions()


def topics_context_text(topics: list, topic_id: str) -> str:
    """生成深通道注入用的话题历史上下文（按 id 查找，找不到返回空）。"""
    t = next((x for x in topics if str(x.get("id", "")) == topic_id), None)
    if not t:
        return ""
    hist = t.get("history", [])[-TOPIC_HISTORY_ROUNDS:]
    if not hist:
        return ""
    return ("【继续之前的话题】用户之前在这个话题下聊过（按时间顺序，"
            "最后一条是最近一轮）：\n" + "\n".join(hist) +
            "\n请结合以上上下文回答用户的新问题。\n")


def update_topic(topics: list, topic_id: str, question: str, answer: str,
                 summarize_fn=None) -> list:
    """把本轮问答记入话题档案（按 id 更新；topic_id 为空则创建新话题）。
    返回更新后的列表。

    summarize_fn(history: list) -> str：把话题问答压缩成摘要的调用方注入函数
    （主桥传 topic_summarize，即 Flash 摘要）。不注入则沿用旧摘要。
    """
    round_text = f"问：{question}\n答：{answer[:400]}"

    def _new_topic():
        return {
            "id": "t-" + __import__("uuid").uuid4().hex[:12],
            "summary": question[:60],
            "history": [round_text],
            "last_active": _now_iso(),
            "turns": 1,
        }

    if not topic_id:
        topics.append(_new_topic())
    else:
        t = next((x for x in topics if str(x.get("id", "")) == topic_id), None)
        if t is None:
            # id 对应的旧话题已被清理/不存在：按新话题建（不写错别的话题）
            topics.append(_new_topic())
        else:
            t["history"] = (t.get("history", []) + [round_text])[-12:]
            t["last_active"] = _now_iso()
            t["turns"] = t.get("turns", 0) + 1
            if summarize_fn is not None:
                t["summary"] = summarize_fn(t["history"])
    topics.sort(key=lambda t: t.get("last_active", ""), reverse=True)
    return topics


def record_pending(question: str, answer: str, action: str) -> None:
    """记录上一轮对话上下文：每次回答后都写（TTL 内供下一轮消费，连续对话用）。

    question: 本轮用户问题；answer: 播报全文；action: 对话状态。
    kind=reply：反问结尾（keep_open），用户可直接回答「可以啊」时理解选择；
    kind=topic：普通回答，供「那后天呢」式承接短句补全话题。
    """
    with state_lock:
        try:
            atomic_write_json(PENDING_FILE, {
                "ts": _now_iso(),
                "question": question,
                "reply": answer[:800],
                "kind": "reply" if action == "keep_open" else "topic",
            })
        except OSError:
            pass


def clear_pending() -> None:
    """显式清除待答复状态（对话结束/新话题开始时调用）。"""
    with state_lock:
        try:
            os.remove(PENDING_FILE)
        except OSError:
            pass


# 应答词：对上一轮反问的直接回答（短词，不进承接判断）
_ANSWER_WORDS = ("可以", "可以啊", "好的", "好呀", "好啊", "好吧", "好", "行", "行啊",
                 "不用", "不需要", "不用了", "嗯", "在", "在的", "在呢", "要", "要啊",
                 "知道了", "哦", "哦哦", "对", "对的", "是的", "是", "算了吧", "算了")
# 承接特征词：短句追问省略了话题（如「那后天呢」「然后呢」「下大后天呢」）
_FOLLOWUP_WORDS = ("那", "然后", "再", "下", "明天", "后天", "昨天", "今天", "明后天",
                   "大后天", "它", "他", "这", "哪个", "哪")
# 强领域词：含这些词的短句视为完整问题（自带话题），不承接
_DOMAIN_WORDS = ("天气", "温度", "气温", "几点", "多少", "股票", "基金", "新闻", "音乐",
                 "歌", "闹钟", "灯", "空调", "风扇", "扫地", "播放", "暂停", "音量", "几度",
                 "湿度", "雨", "价钱", "价格", "快递", "上班", "回家", "红包", "转账")


def is_answer_word(text: str) -> bool:
    t = text.strip().rstrip("？?。.!！～~ ")
    if not t:
        return False
    if t in _ANSWER_WORDS:
        return True
    return len(t) <= 2 and t[0] in "好行要不要可" and t.endswith(("啊", "吧", "呀"))


def is_followup_short(text: str) -> bool:
    """承接短句判定：短（≤10 字）、无强领域词、含承接特征词。"""
    t = text.strip().rstrip("？?。.!！～~ ")
    if not t or len(t) > 10:
        return False
    if any(w in t for w in _DOMAIN_WORDS):
        return False
    return any(w in t for w in _FOLLOWUP_WORDS)


def consume_pending(now_question: str) -> str:
    """消费上一轮上下文（注入提示词用，加锁防并发）：

    - 应答词（可以啊/好/不用）→ 反问回答语义（消费并清除）；
    - 承接短句（那后天呢/然后呢）→ 话题承接语义（保留，支持连续追问）；
    - 完整新问题 → 上一轮上下文作废（清除），正常处理。
    """
    with state_lock:
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                data = json.load(f)
            ts = datetime.datetime.fromisoformat(data.get("ts", ""))
            if (datetime.datetime.now() - ts).total_seconds() > PENDING_TTL_SECONDS:
                try:
                    os.remove(PENDING_FILE)
                except OSError:
                    pass
                return ""
            if is_answer_word(now_question):
                ctx = (f"【接上一轮反问】上一轮用户问「{data.get('question', '')}」，"
                       f"你当时反问：{data.get('reply', '')}\n"
                       f"现在用户回答：「{now_question}」——请对照你的反问理解用户的选择，"
                       f"直接执行或回答，不要再问一遍。")
                try:
                    os.remove(PENDING_FILE)
                except OSError:
                    pass
                return ctx
            if is_followup_short(now_question):
                # 承接短句：注入上下文并保留 pending（支持「那后天呢→那大后天呢」连续追问）
                return (f"【承接上一轮对话】上一轮用户问「{data.get('question', '')}」，"
                        f"你回答：「{data.get('reply', '')}」。\n"
                        f"现在用户又说「{now_question}」——这是对同一话题的承接连问"
                        f"（短句省略了话题，例如上轮在聊天气、本轮「那后天呢」就是在问"
                        f"后天的天气）。请把它当作对上一话题的追问直接回答，不要反问。")
            try:
                os.remove(PENDING_FILE)  # 完整新问题：旧上下文作废
            except OSError:
                pass
            return ""
        except (OSError, ValueError, KeyError):
            return ""
