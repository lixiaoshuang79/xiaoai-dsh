#!/usr/bin/env python3
"""xiaogpt ↔ 本地大脑桥（v3：快慢路由）

快速通道：桥内直连大模型的快模型（不思考，实测 1-5 秒），
          带 hass-mcp 工具调用循环（查状态、开关设备）。
深度通道：dsh --profile headless（深模型深度推理，带工具）。

路由：所有问题先走快速通道；快速模型判定需要深度思考（回复以「深」开头）
      → 自动升级深度通道。快速通道异常 → 退回 dsh 快速模式。

用法: .venv/bin/python xiaogpt-bridge.py [--port 8322]
"""
import concurrent.futures
import datetime
import hmac
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config_loader import (
    ConfigError, cfg_llm, cfg_ha, cfg_devices, cfg_paths, cfg_playback,
    cfg_bridge_secret, is_example, repo_root,
)

# 叶子模块（纯逻辑/存储/发现，不反向依赖本桥）：
#   security —— URL/IP 安全校验与日志脱敏
#   state_store —— 原子写 / turn 代际 / 全局状态锁
#   topic_state —— 话题档案 / 待答复 / 会话历史持久化（LLM 编排留在本桥）
#   device_discovery —— HA 设备自动发现（只返回候选，由本桥应用）
import device_discovery  # noqa: E402
import security  # noqa: E402
import state_store  # noqa: E402
import topic_state  # noqa: E402
from state_store import atomic_write_json, atomic_write_text, current_turn, next_turn  # noqa: E402

PORT = 8322
_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = repo_root()

# —— 本地工具与路径（config/local.json 的 paths 段可覆盖）——
NODE = cfg_paths("node") or "node"
CHECKOUT = cfg_paths("dsh_checkout") or ""
CLI = os.path.join(CHECKOUT, "apps", "cli", "src", "bin.ts") if CHECKOUT else ""
LOG_DIR = os.path.join(_REPO_ROOT, "logs")
FAST_PATCH = os.path.join(_REPO_ROOT, "config", "generated", "dsh-speaker", "dsh-fast.patch.yml")

# —— 大模型（OpenAI 兼容端点，由 localhost 配置后台统一配置）——
LLM_BASE = (cfg_llm("base_url") or "").rstrip("/")
LLM_MODEL = cfg_llm("fast_model") or ""
MCP_URL = "http://127.0.0.1:8321/mcp"
# 音箱发起的 DSH 会话用隔离的 DSH_HOME 存储（不出现在用户的 DSH 会话列表里）
DSH_HOME_SPEAKER = cfg_paths("speaker_dsh_home") or os.path.expanduser("~/.dsh-speaker")
# 音箱的项目文件夹：对话记录、话题档案、项目记忆都放这里（深通道的 cwd）
SPEAKER_HOME = cfg_paths("speaker_workspace") or os.path.expanduser("~/xiaogpt-speaker")
# 话题档案路径等由 topic_state.configure() 在启动时注入（见 main()）
TOPIC_MAX_ACTIVE = 5  # 判定话题关联时最多参考最近几个话题（topic_choose 用）
# 待答复状态：小爱反问用户（如 A 还是 B）后，下一轮用户直接回答「A」时
# 把反问全文注入上下文，让小爱理解「A」是对上一轮反问的回答（存储见 topic_state）
# 音箱专用技能库（仓库内人工维护的只读精选；模型沉淀的技能写 RUNTIME_SKILLS_DIR）
SKILLS_DIR = os.path.join(_REPO_ROOT, "skills", "speaker-skills")
# 音箱自己的长期记忆（Memory Evolve，存于隔离 home）；用户 DSH 主记忆（只读兜底）
SPEAKER_MEMORY_DIR = os.path.join(DSH_HOME_SPEAKER, "memories")
USER_MAIN_MEMORY_DIR = os.path.expanduser("~/.dsh/memories")
HTTP_TIMEOUT = 45
MAX_TOOL_TURNS = 6
TIMEOUT_DEEP = 480

# —— 关键设备实体（config/local.json 的 devices 段）——
MAIN_LIGHT_ENTITY = cfg_devices("main_light") or ""
AMBIENT_LIGHT_ENTITY = cfg_devices("ambient_light") or ""
AC_TEMP_ENTITY = cfg_devices("ac_temperature") or ""
AC_TURN_ON_ENTITY = cfg_devices("ac_turn_on") or ""
AC_TURN_OFF_ENTITY = cfg_devices("ac_turn_off") or ""
AC_MODE_ENTITY = cfg_devices("ac_mode") or ""
AC_FAN_UP_ENTITY = cfg_devices("ac_fan_up") or ""
AC_FAN_DOWN_ENTITY = cfg_devices("ac_fan_down") or ""

# ---------- 扫地机器人（石头 G10S 2）确定性短路 ----------

VACUUM_ENTITY = cfg_devices("vacuum_entity") or ""
VACUUM_MODE_ENTITY = cfg_devices("vacuum_mode_entity") or ""

# ---------- 塔扇 / 摄像头确定性短路 ----------

FAN_ENTITY = cfg_devices("fan_entity") or ""
FAN_DELAY_ENTITY = cfg_devices("fan_delay_entity") or ""
FAN_ANGLE_ENTITY = cfg_devices("fan_angle_entity") or ""
CAM1_ON_ENTITY = cfg_devices("camera1_on") or ""
CAM2_ON_ENTITY = cfg_devices("camera2_on") or ""

# ---------- 运行时数据目录（自我进化产物与状态文件的落盘位置） ----------
# 公开仓库里 skills/speaker-skills 是人工维护的只读精选技能；模型沉淀的技能/工具
# 一律写运行时数据目录（SPEAKER_HOME 下），绝不写回公开仓库（防 git dirty 与
# prompt injection 污染）。Evolved tools 同理不再放 bridge/。
RUNTIME_DIR = os.path.join(SPEAKER_HOME, "runtime")
RUNTIME_SKILLS_DIR = os.path.join(RUNTIME_DIR, "speaker-skills")
EVOLVED_TOOLS_FILE = os.path.join(RUNTIME_DIR, "evolved-tools.json")
_REMINDERS_FILE = os.path.join(RUNTIME_DIR, "reminders.json")

# ---------- 桥鉴权 secret（本机随机/配置生成，migpt 与桥共用） ----------
BRIDGE_SECRET = cfg_bridge_secret()
# 4299 之后 migpt 的 /play /play_url /native /exec 也要求同一 secret

# ---------- 播放 URL 安全策略（SSRF 防护） ----------
# 默认放行私网 LAN（音箱播放的 relay URL = http://<Mac-IP>:4378/stream 是私网地址，
# 属正常链路）；loopback/link-local/metadata 永远拒绝。
# playback.allow_private_urls=false 可开严格模式（连私网 LAN 也拒绝）。
# 校验逻辑在 security 模块；这里只注入配置。
security.ALLOW_PRIVATE_URLS = bool(cfg_playback("allow_private_urls", True))

def _vacuum_shortcut(question: str) -> str | None:
    """扫地机器人确定性短路：清扫模式/启停/回充直连 HA，一次完成。
    2026-08-23 事故：模型绕不出「只扫不拖」掉进 native_device_command，
    官方小爱按默认扫拖启动了机器人（又吵又错）。机器人指令绝不走模型、绝不走官方。
    模式实体 cleaning_mode：vacuum=只扫 vac_and_mop=扫拖 mop=只拖。"""
    if not VACUUM_ENTITY or not VACUUM_MODE_ENTITY:
        return None  # 未配置设备实体（config/local.json 的 devices 段）
    if not re.search(r"扫地机|机器人|石头", question):
        return None  # 无机器人语境（“拖地”这种模糊词不触发）
    if re.search(r"客厅|卧室|厨房|卫生间|厕所|书房|阳台|餐厅|饭厅|房间|次卧|主卧", question):
        return None  # 按房间清扫：HA 未暴露房间 ID，短路启动会误清全屋，走模型如实处理
    if re.search(r"吗|有没有|是不是|还在|在不在|状态|电量|多少|几[个下]|没电|滤网|记录|面积|多久", question):
        return None  # 查询类（还在扫吗/电量多少…）走模型读实体，绝不触发启动
    if re.search(r"暂停|停一下|歇一下", question):
        _ha_service("vacuum", "pause", {"entity_id": VACUUM_ENTITY})
        print("[bridge] 扫地机短路: 暂停", flush=True)
        return "已经让它暂停了，先生。"
    if re.search(r"停止|别扫了|别拖了|停下|取消", question):
        _ha_service("vacuum", "stop", {"entity_id": VACUUM_ENTITY})
        print("[bridge] 扫地机短路: 停止", flush=True)
        return "已经让它停下了，先生。"
    if re.search(r"回充|回基站|回家|充电|归位", question):
        _ha_service("vacuum", "return_to_base", {"entity_id": VACUUM_ENTITY})
        print("[bridge] 扫地机短路: 回充", flush=True)
        return "已经让它回基站了，先生。"
    mode = None
    what = None
    if re.search(r"只扫|仅扫|单扫|光扫|不拖地|不拖|纯扫", question):
        mode, what = "vacuum", "只扫不拖"
    elif re.search(r"扫拖|边扫边拖|又扫又拖|扫和拖|扫地拖地|扫抹", question):
        mode, what = "vac_and_mop", "扫拖一起"
    elif re.search(r"只拖|仅拖|单拖|光拖|拖地|拖一下|拖拖|不扫地|不扫|纯拖", question):
        mode, what = "mop", "只拖不扫"
    if mode:
        _ha_service("select", "select_option",
                    {"entity_id": VACUUM_MODE_ENTITY, "option": mode})
        _ha_service("vacuum", "start", {"entity_id": VACUUM_ENTITY})
        print(f"[bridge] 扫地机短路: 模式{mode} 启动", flush=True)
        return f"开始{what}了，先生。"
    # 裸启动：剥离设备名后再看有没有动作词（防「扫地机器人」三字自带「扫地」误触发）
    q_act = re.sub(r"扫地机器?人?|石头", "", question)
    if re.search(r"扫地|打扫|清扫|大扫除|扫一下|扫扫|扫个|扫一会|干活|启动|开始", q_act):
        # 没指明模式：按当前模式直接启动，并如实播报
        try:
            st = device_discovery.ha_state(VACUUM_MODE_ENTITY, _load_env)
            cur = st.get("state", "unknown")
            cur_cn = {"vacuum": "只扫", "vac_and_mop": "扫拖",
                      "mop": "只拖"}.get(cur, "当前模式")
        except Exception:
            cur_cn = "当前模式"
        _ha_service("vacuum", "start", {"entity_id": VACUUM_ENTITY})
        print("[bridge] 扫地机短路: 当前模式启动", flush=True)
        return f"开始扫地了（{cur_cn}），先生。"
    return None  # 其余（问电量/滤网寿命等查询）走模型工具链

def _apply_discovery() -> None:
    """调用 device_discovery 并应用结果到本桥全局（只填空缺，绝不覆盖已配置）。"""
    discovered = device_discovery.discover_devices(
        {k: globals().get(k, "") for k in (
            "AC_TEMP_ENTITY", "AC_MODE_ENTITY", "AC_TURN_ON_ENTITY",
            "AC_TURN_OFF_ENTITY", "AC_FAN_UP_ENTITY", "AC_FAN_DOWN_ENTITY",
            "FAN_ENTITY", "FAN_DELAY_ENTITY", "FAN_ANGLE_ENTITY",
            "CAM1_ON_ENTITY", "CAM2_ON_ENTITY",
            "VACUUM_ENTITY", "VACUUM_MODE_ENTITY",
            "MAIN_LIGHT_ENTITY", "AMBIENT_LIGHT_ENTITY", "SPEAKER_PLAYER",
        )},
        _load_env,
    )
    for k, v in discovered.items():
        globals()[k] = v

def router_instruction() -> str:
    """快速通道提示词：基础纪律（静态）+ 设备规则（按配置/自动发现的实体渲染）。"""
    _apply_discovery()  # 幂等：config 留空的设备实体在首次渲染时自动从 HA 发现
    return _ROUTER_BASE + _router_device_rules()

_ROUTER_BASE = (
    "你是家庭语音管家的快速通道。\n"
    "日常问题直接回答或调用工具（开灯、调温度、查状态、简单算术、常识问答、"
    "单位换算等），尽量少调用工具，回答口语化、简短。\n"
    "本轮需要调用工具时：直接输出工具调用，一个字都不要说——不要说"
    "「让我查一下/我来看看/稍等，我看看/I'll help you」之类的任何叙述"
    "（中文英文都不行），"
    "思考过程也绝不允许输出。所有输出必须中文。\n"
    "工具结果返回后只给最终结论，不要念出设备技术编号或英文 ID。\n"
    "输出纪律——只答一遍：每个回答只说一遍结论，绝对禁止说完之后再"
    "换一种说法重复总结一遍（如「已经把氛围灯关掉了」后面又跟一句"
    "「好的，氛围灯已经关掉了」），一句话结尾就是结尾，直接停。\n"
    "铁律——绝不回绝：家里设备查不到（HA 里确实没有这个设备）就直说"
    "「家里没有这个设备」；除此之外的任何任务，只要你没有现成工具、"
    "没把握、或判断它超出了你的能力（电脑操作、编程、文件处理、深度分析、"
    "复杂多步任务等），绝不允许说「查不到/看不到/做不了/帮不了」之类的"
    "回绝话术——只回复一个字：深，交给本地大脑 DSH 去完成。\n"
    "能力清单（必须用工具查，不要凭记忆编）：\n"
    "· 时间/日期/星期：用 get_now_time 工具（不要自己编时间）。\n"
    "· 天气/气温/下雨/风力/适不适合出门：用 get_weather 工具（含未来几天预报）。\n"
    "· 电脑电量：用 search_entities_tool 找名字带「电量/电池」的传感器，再用 get_entity 查。\n"
    "· 电脑上的文件/文件夹：用 list_computer_files（看目录里有什么）、"
    "read_computer_file（读文件内容）、search_computer_files（按名字找文件）。"
    "「桌面有什么」「下载里有什么」「某个文件写了什么」「找某个文件」直接查，不要推脱。\n"
    "· 家里的设备状态与控制：用 get_entity / entity_action / list_entities / search_entities_tool。\n"
    "· 设备指令（开灯/关灯/打开/关闭/调到/温度/模式/风速/亮度…）直接执行，"
    "绝不反问「开哪个灯」：\n"
    "  官方小爱不再出声、不再执行，你就是家里唯一的设备执行者，"
    "用户说出的每一条设备指令都由你通过 HA 工具（list_entities 查实体 → entity_action 执行）来完成。\n"
    "  native_device_command 工具仍然可用（走音箱原生日志通道），"
    "但首选 HA 的 entity_action（本地直连、状态可回读验证）。\n"
    "· 音量指令（声音调到50%/音量25%/大点声/小声点/音箱音量…）："
    "直接用 set_speaker_volume 工具执行，percent 从用户话里提取；"
    "没指明哪台音箱就调正在说话的这台（which 不传）。"
    "【绝对禁止】反问「您是要调音量吗」「哪台音箱」「调到多少」——直接做。\n"
    "· 播放类需求铁律：用户要求播放某首具体的歌/音乐/音频片段（含歌名、角色名、"
    "场景名等具体指向，如「播放XX的歌」），具体歌名一律先调 netease_music_play "
    "（网易云正版音源）；网易云失败（搜不到/无版权/风控）再调 web_audio_play；都失败才回一个字：深。"
    "「放每日推荐/放我喜欢的歌」调 netease_music_personal；「放我的XX歌单」调 netease_music_playlist。"
    "白噪音/助眠音/电台调 web_audio_play。"
    "绝不允许自己编答案，更不允许说「我放不了/去手机听」这种回绝话术。\n"
    "铁律——绝不反问：所有指令类问题（调音量、调温度、开关设备、设提醒等）"
    "有明确动作就直接执行；哪怕有一点歧义，也选最合理的默认值执行，"
    "执行结果里带一句简短确认即可。只有信息严重缺失到无法执行时才反问，"
    "且反问时给出具体选项（如「开卧室灯还是客厅灯」）而不是开放式提问。\n"
    "对话节奏：你只需要自然地说话——想继续聊就反问/邀请，不想聊就自然收尾。"
    "对话结束后音箱会不会继续听，由框架自动识别你的话判断，你不用管。\n"
    "· 行情/股市/基金类回答：把大盘指数、涨跌、成交额、主力资金/北向资金/板块"
    "等用户可能接着追问的信息一次性说完（有就带，没有就略过）。\n"
    "· 用户说告别/终结词（没事了、不用了、退下、拜拜、再见、就这样、你休息吧、"
    "没别的事了）：简短收尾（如「好的先生，随时叫我」），"
    "绝不用问句结尾——用户要结束对话，不允许再抛问题把对话拖下去。\n"
    "需要转交本地大脑 DSH（只回复一个字：深，不要尝试自己做，"
    "也不要任何解释或铺垫——就是只输出「深」这一个字）：\n"
    "· 时效性数据一律不许凭记忆编：限行/路况/快递/股票基金行情/体育比分/"
    "实时新闻/商品价格/演出信息/游戏版本更新/新角色新内容等，你没有工具查就回：深；\n"
    "· 需要深入思考的问题：方案设计、深度分析、复杂逻辑推理、专业写作、代码、数据处理；\n"
    "· 「帮我分析/分析一下/看看家里/检查一下/盘点/总结/评估」等多步查询、反复对比的任务；\n"
    "· 涉及电脑操作的任务：创建/修改/移动/整理/删除文件、跑程序、查系统信息、"
    "批量处理等——你没有写文件的工具，不要用只读工具试探，直接回复：深；\n"
    "· 涉及用户个人信息或家庭背景（邮箱、电话、姓名、家庭成员的安排、过往约定等）而你记不清的问题；\n"
    "· 任何你没有工具、没把握、拿不准的任务——宁可转交，绝不可回绝用户。"
)

def _router_device_rules() -> str:
    """设备规则段：只渲染已知的实体（配置或自动发现），不知道的就写通用做法。"""
    parts = []
    if MAIN_LIGHT_ENTITY:
        parts.append(
            "设备规则（实体已配置/自动发现，直接用，别去搜索）：\n"
            "  灯的铁律：用户只说「开灯/关灯」没指明哪个灯时，"
            "只操作主灯 = " + MAIN_LIGHT_ENTITY + "；绝不碰氛围灯，绝不碰任何指示灯。\n"
        )
        if AMBIENT_LIGHT_ENTITY:
            parts.append(
                "  用户明说「氛围灯」时操作 " + AMBIENT_LIGHT_ENTITY + "；"
                "明说其他灯时才按名字找对应实体。\n"
            )
    else:
        parts.append(
            "设备规则：\n"
            "  灯：用户只说「开灯/关灯」没指明哪个灯时，用 list_entities 找一个"
            "名字带「灯」且不带「氛围」「指示」的实体操作（优先 switch/light 域），"
            "绝不反问「开哪个灯」。\n"
        )
    if AC_TEMP_ENTITY:
        rule = (
            "  空调的铁律：空调温度只有一个 number 实体 = " + AC_TEMP_ENTITY + "，"
            "调温度直接用 entity_action 给它 set_value（如 24），绝不按温度±按钮，"
            "绝不反复查询验证（红外空调没有状态回读，查了也没用）。"
        )
        if AC_TURN_ON_ENTITY:
            rule += "「开空调」= 先 press " + AC_TURN_ON_ENTITY + "。\n"
        else:
            rule += "\n"
        parts.append(rule)
    if VACUUM_ENTITY:
        parts.append(
            "  扫地机器人：模式/启动/暂停/停止/回充由桥侧确定性短路直接执行，"
            "你不需要用工具操作 vacuum.* 或 cleaning_mode 相关实体；"
            "【绝对禁止】用 native_device_command 转发扫地机器人指令（官方小爱会按默认扫拖乱来）。"
            "只有查询类问题（电量/滤网寿命/清扫记录）才用工具读实体。\n"
        )
    parts.append(
        "  离线设备原则：get_entity 返回 unavailable 就如实说「XX现在离线了」，"
        "绝不反复重试、绝不编造。\n"
        "  执行完简短确认（如「灯已经打开了，先生」）；因为垫场词已经说过「好的」，"
        "你的回复不要以「好的」开头。\n"
    )
    return "".join(parts)

lock = threading.Lock()
_llm_key: str | None = None
_mcp_session: str | None = None
_tools_cache: list | None = None

def load_persona() -> str:
    """人设提示词：优先读仓库内 bridge/xiaogpt-persona.txt（如存在），
    否则用 localhost 后台配置的 llm.system_prompt。"""
    persona_file = os.path.join(_BRIDGE_DIR, "xiaogpt-persona.txt")
    try:
        with open(persona_file, encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    except OSError:
        pass
    return cfg_llm("system_prompt") or ""

def load_llm_key() -> str:
    global _llm_key
    if not _llm_key:
        _llm_key = os.environ.get("LLM_API_KEY") or cfg_llm("api_key") or ""
        if not _llm_key:
            print("[bridge] ⚠️ 未配置大模型 API Key（config/local.json 的 llm.api_key），"
                  "大模型直连不可用", flush=True)
    return _llm_key

# ---------- MCP（hass-mcp，Streamable HTTP） ----------

def mcp_rpc(method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    """发一次 JSON-RPC 到 hass-mcp，维护 Mcp-Session-Id，返回 result。"""
    global _mcp_session
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        payload["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _mcp_session:
        headers["Mcp-Session-Id"] = _mcp_session
    req = urllib.request.Request(
        MCP_URL, json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            _mcp_session = sid
        body = resp.read().decode("utf-8")
    data = json.loads(body)
    if "error" in data:
        raise RuntimeError(f"MCP {method} 错误: {data['error']}")
    return data.get("result", {})

def get_openai_tools() -> list:
    """获取工具列表并转成 OpenAI function 格式（带缓存）：
    hass-mcp 工具 + 本地天气工具 + 自我进化沉淀的工具。"""
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache
    mcp_rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "xiaogpt-bridge", "version": "5.0"}}, 1)
    result = mcp_rpc("tools/list", rpc_id=2)
    tools = result.get("tools", [])
    _tools_cache = [
        {"type": "function",
         "function": {"name": t["name"],
                      "description": t.get("description", "")[:1000],
                      "parameters": t.get("inputSchema", {"type": "object", "properties": {}})}}
        for t in tools
    ]
    _tools_cache.append(WEATHER_TOOL)
    _tools_cache.append(TIME_TOOL)
    _tools_cache.extend(COMPUTER_TOOLS)
    _tools_cache.extend(SPEAKER_VOLUME_TOOLS)
    _tools_cache.append(NATIVE_DEVICE_TOOL)
    _tools_cache.append(WEB_AUDIO_PLAY_TOOL)
    _tools_cache.append(SPEAKER_MUSIC_TOOL)
    _tools_cache.append(NETEASE_PLAY_TOOL)
    _tools_cache.append(NETEASE_PERSONAL_TOOL)
    _tools_cache.append(NETEASE_PLAYLIST_TOOL)
    _tools_cache.append(NETEASE_LYRIC_TOOL)
    _tools_cache.append(REMINDER_SET_TOOL)
    _tools_cache.append(REMINDER_LIST_TOOL)
    _tools_cache.append(REMINDER_CANCEL_TOOL)
    for spec in _evolved_tools.values():
        _tools_cache.append({
            "type": "function",
            "function": {"name": spec["name"],
                         "description": spec["description"],
                         "parameters": {"type": "object", "properties": {}, "required": []}},
        })
    return _tools_cache

# ---------- 本地天气工具（直连 HA REST，hass-mcp 服务调用拿不到预报返回） ----------

WEATHER_ENTITY = "weather.forecast_wo_de_jia"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
CONDITIONS = {
    "clear-night": "晴朗", "clear": "晴", "sunny": "晴", "partlycloudy": "多云",
    "cloudy": "阴", "fog": "有雾", "rainy": "雨", "pouring": "大雨",
    "lightning-rainy": "雷雨", "snowy": "雪", "snowy-rainy": "雨夹雪",
    "windy": "大风", "hail": "冰雹", "exceptional": "特殊天气",
}

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "查询北京的天气预报（今天、明天、后天等未来几天的天气、气温、降水、湿度）。"
            "天气类问题（天气怎么样/明天下雨吗/气温多少/适合出门吗）都用这个工具，"
            "不要用 get_entity 查天气实体（实体里没有预报数据）。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# ---------- 本地时间工具（高频场景，直接取本机时间，不依赖 HA） ----------

def get_now_time() -> str:
    now = datetime.datetime.now()
    week = WEEKDAYS[now.weekday()]
    return f"现在是 {now.month}月{now.day}日 {week}，{now.hour}点{now.minute}分"

TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_now_time",
        "description": (
            "查询当前时间和日期。用户问「现在几点了」「今天几号」「今天星期几」时用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# ---------- 音箱音量工具（直连 HA media_player，高频场景必须直接做、不反问） ----------

SPEAKER_PLAYER = cfg_devices("speaker_volume") or ""  # 正在使用的音箱
SPEAKER_PLAYER_2 = cfg_devices("speaker_volume_2") or ""  # 另一台音箱（可选）

def _ac_shortcut(question: str) -> str | None:
    """空调确定性短路（红外无状态回读，绝不查状态验证）：
    温度（16-30 度）、模式（制冷/制热/除湿/送风/自动）、风速±、开关机。
    模式/温度组合（制冷26度）一次完成；带「开」字先发开机码（离散开机码幂等）。"""
    if not (AC_TEMP_ENTITY and AC_TURN_ON_ENTITY):
        return None  # 未配置
    if "空调" not in question:
        return None
    if re.search(r"吗|有没有|是不是|多少度|几度|状态", question):
        return None  # 查询 → 模型（红外查不到状态，模型按 ROUTER 规则如实说）
    # 温度（可与模式组合）；取句中最后一个温度数字（防「16度以下不行15度」误取16）；
    # 「调到/开到26」不带度字也算温度
    m = re.findall(r"(\d{1,2})\s*度", question)
    if not m:
        m2 = re.search(r"(?:调到|开到|设到|设置|设定|设为)\s*(\d{1,2})\s*$", question)
        if m2:
            m = [m2.group(1)]
    if m:
        temp = int(m[-1])
        if not (16 <= temp <= 30):
            return f"空调只支持 16 到 30 度，{temp} 度没法设，先生。"
        if re.search(r"开|启动", question):
            _ha_service("button", "press", {"entity_id": AC_TURN_ON_ENTITY})
            time.sleep(0.5)  # 红外码间隔，防串码
        mode = None
        mode_cn = None
        if re.search(r"制冷", question):
            mode, mode_cn = "Cool", "制冷"
        elif re.search(r"制热|取暖", question):
            mode, mode_cn = "Heat", "制热"
        elif re.search(r"除湿|抽湿", question):
            mode, mode_cn = "Dry", "除湿"
        elif re.search(r"送风|通风", question):
            mode, mode_cn = "Fan", "送风"
        if mode:
            if not re.search(r"开|启动", question):
                _ha_service("button", "press", {"entity_id": AC_TURN_ON_ENTITY})
                time.sleep(0.5)
            _ha_service("select", "select_option",
                        {"entity_id": AC_MODE_ENTITY, "option": mode})
            time.sleep(0.5)
        _ha_service("number", "set_value", {"entity_id": AC_TEMP_ENTITY, "value": temp})
        print(f"[bridge] 空调短路: {temp}度{mode or ''}", flush=True)
        return (f"空调已经调到 {temp} 度、{mode_cn}模式了，先生。" if mode
                else f"空调已经调到 {temp} 度，先生。")
    # 模式
    for pat, val, cn in [("制冷", "Cool", "制冷"), ("制热|取暖", "Heat", "制热"),
                         ("除湿|抽湿", "Dry", "除湿"), ("送风|通风|换气", "Fan", "送风"),
                         ("自动", "Auto", "自动")]:
        if re.search(pat, question):
            _ha_service("button", "press", {"entity_id": AC_TURN_ON_ENTITY})
            time.sleep(0.5)
            _ha_service("select", "select_option",
                        {"entity_id": AC_MODE_ENTITY, "option": val})
            print(f"[bridge] 空调短路: 模式{val}", flush=True)
            return f"空调已经调到{cn}模式了，先生。"
    # 风速±（红外按钮，无回读）
    if re.search(r"风速|风量", question):
        if re.search(r"大|高|加|升|快", question):
            _ha_service("button", "press", {"entity_id": AC_FAN_UP_ENTITY})
            print("[bridge] 空调短路: 风速+", flush=True)
            return "空调风速已经调大了，先生。"
        if re.search(r"小|低|减|降|慢|弱", question):
            _ha_service("button", "press", {"entity_id": AC_FAN_DOWN_ENTITY})
            print("[bridge] 空调短路: 风速-", flush=True)
            return "空调风速已经调小了，先生。"
    # 开关机
    if re.search(r"关|停", question):
        _ha_service("button", "press", {"entity_id": AC_TURN_OFF_ENTITY})
        print("[bridge] 空调短路: 关机", flush=True)
        return "空调已经关了，先生。"
    if re.search(r"开|启动", question):
        _ha_service("button", "press", {"entity_id": AC_TURN_ON_ENTITY})
        print("[bridge] 空调短路: 开机", flush=True)
        return "空调已经开了，先生。"
    return None

def _fan_shortcut(question: str) -> str | None:
    """塔扇确定性短路：定时关机/扫风夹角/摇头/开关/模式（直吹风/自然风/睡眠风）/
    风速（百分比/档位/大点小点）。风扇是本地直连有状态回读，大点小点读当前值再调。"""
    if not (FAN_ENTITY and FAN_DELAY_ENTITY):
        return None  # 未配置
    if not re.search(r"风扇|塔扇|电扇|电风扇", question):
        return None
    if re.search(r"吗|有没有|是不是|状态|开了没|转了没", question):
        return None  # 查询 → 模型
    # 定时/延时关机（放最前：避免「10分钟后关」被「关」分支直接关机）
    m = re.search(r"(\d+)\s*(分钟|分|小时|时|h)", question)
    if m and re.search(r"延时|定时|后关|后停|倒计时|再关", question):
        n = int(m.group(1))
        if re.search(r"小时|时|h", m.group(2)):
            n *= 60
        n = max(0, min(480, n))
        _ha_service("number", "set_value", {"entity_id": FAN_DELAY_ENTITY, "value": n})
        print(f"[bridge] 风扇短路: 延时{n}分", flush=True)
        return f"已设 {n} 分钟后自动关风扇，先生。"
    # 扫风夹角（30/60/90/120/150 取最近合法值）
    m2 = re.search(r"(\d+)\s*度", question)
    if m2 and re.search(r"夹角|扫风角度|摆动角|摆角", question):
        ang = min([30, 60, 90, 120, 150], key=lambda a: abs(a - int(m2.group(1))))
        _ha_service("select", "select_option",
                    {"entity_id": FAN_ANGLE_ENTITY, "option": str(ang)})
        print(f"[bridge] 风扇短路: 夹角{ang}", flush=True)
        return f"扫风夹角调到 {ang} 度了，先生。"
    # 摇头/摆头/扫风
    if re.search(r"摇头|摆头|扫风|摆动", question):
        off = bool(re.search(r"别|不要|关|停|取消|不了", question))
        _ha_service("fan", "oscillate",
                    {"entity_id": FAN_ENTITY, "oscillating": not off})
        print(f"[bridge] 风扇短路: 摇头{'关' if off else '开'}", flush=True)
        return ("风扇停止摇头了，先生。" if off else "风扇开始摇头了，先生。")
    # 关机
    if re.search(r"关|停|闭", question):
        _ha_service("fan", "turn_off", {"entity_id": FAN_ENTITY})
        print("[bridge] 风扇短路: 关", flush=True)
        return "风扇已经关了，先生。"
    # 模式
    preset = None
    if re.search(r"自然风", question):
        preset = "自然风"
    elif re.search(r"直吹", question):
        preset = "直吹风"
    elif re.search(r"睡眠风|睡眠", question):
        preset = "睡眠风"
    if preset:
        _ha_service("fan", "set_preset_mode",
                    {"entity_id": FAN_ENTITY, "preset_mode": preset})
        print(f"[bridge] 风扇短路: 模式{preset}", flush=True)
        return f"风扇切到{preset}了，先生。"
    # 风速
    pct = None
    if re.search(r"最大|最高|拉满|开到顶", question):
        pct = 100
    elif re.search(r"最小|最低", question):
        pct = 10
    elif (mm := re.search(r"(\d{1,3})\s*[%％]", question)):
        pct = int(mm.group(1))
    elif (mm := re.search(r"(\d{1,2})\s*档", question)):
        n = int(mm.group(1))
        pct = n * 20 if n <= 5 else n
    elif re.search(r"风速|风量|调到|开到", question) and (mm := re.search(r"(\d{1,3})(?![分时hH秒])", question)):
        pct = int(mm.group(1))
    elif re.search(r"风大点|大点|风大|调大|高点|加大|加.*档", question):
        try:
            pct = device_discovery.ha_state(FAN_ENTITY, _load_env).get("attributes", {}).get("percentage", 50) + 20
        except Exception:
            pct = 70
    elif re.search(r"风小点|小点|风小|调小|低点|减小|减.*档", question):
        try:
            pct = device_discovery.ha_state(FAN_ENTITY, _load_env).get("attributes", {}).get("percentage", 50) - 20
        except Exception:
            pct = 30
    if pct is not None:
        pct = max(10, min(100, int(pct)))
        _ha_service("fan", "set_percentage",
                    {"entity_id": FAN_ENTITY, "percentage": pct})
        print(f"[bridge] 风扇短路: 风速{pct}%", flush=True)
        return f"风扇风速调到 {pct}% 了，先生。"
    # 开机
    if re.search(r"开|启动", question):
        _ha_service("fan", "turn_on", {"entity_id": FAN_ENTITY})
        print("[bridge] 风扇短路: 开", flush=True)
        return "风扇已经开了，先生。"
    return None

def _camera_shortcut(question: str) -> str | None:
    """摄像头确定性短路：只处理开关（隐私遮蔽）。默认摄像头1号；
    明说 2号/第二个 才动 2 号。看画面/配置类（夜视/侦测/巡航）走模型。"""
    if not (CAM1_ON_ENTITY or CAM2_ON_ENTITY):
        return None  # 未配置
    if not re.search(r"摄像头|摄像机", question):
        return None
    if re.search(r"吗|有没有|是不是|状态|在不在|画面|看|夜视|侦测|巡航|灵敏度|音量|静音", question):
        return None  # 查询/看画面/高级配置 → 模型
    ent = CAM2_ON_ENTITY if re.search(r"摄像头\s*2|第二个|二号|2号", question) else CAM1_ON_ENTITY
    if not ent:
        return None
    if re.search(r"关|休眠|停", question):
        _ha_service("switch", "turn_off", {"entity_id": ent})
        print(f"[bridge] 摄像头短路: 关 {ent[-10:]}", flush=True)
        return "摄像头已经关了，先生。"
    if re.search(r"开", question):
        _ha_service("switch", "turn_on", {"entity_id": ent})
        print(f"[bridge] 摄像头短路: 开 {ent[-10:]}", flush=True)
        return "摄像头已经打开了，先生。"
    return None

def _ha_service(domain: str, service: str, data: dict) -> str:
    token = _load_env("HA_TOKEN")
    req = urllib.request.Request(
        f"{HA_URL}/api/services/{domain}/{service}",
        json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    return "ok"

def set_speaker_volume(percent: int, which: str = "self") -> str:
    """调音量：which=self=正在说话的这台音箱（默认），other=家里另一台音箱。"""
    entity = SPEAKER_PLAYER if which != "other" else SPEAKER_PLAYER_2
    level = max(0, min(100, int(percent))) / 100.0
    _ha_service("media_player", "volume_set",
                {"entity_id": entity, "volume_level": level})
    return f"音量已调到 {percent}%"

def get_speaker_volume() -> str:
    token = _load_env("HA_TOKEN")
    req = urllib.request.Request(
        f"{HA_URL}/api/states/{SPEAKER_PLAYER}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        state = json.loads(resp.read().decode("utf-8"))
    level = state.get("attributes", {}).get("volume_level")
    if level is None:
        return "音量信息暂不可用"
    return f"当前音量 {round(level * 100)}%"

SPEAKER_VOLUME_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_speaker_volume",
            "description": (
                "调整音箱音量。percent=音量百分比（0-100）。"
                "用户说「声音调到50%」「音量25%」「大点声」「小声点」都用这个工具。"
                "用户没指明哪台音箱时默认调正在说话的这一台（which 不传或 self），"
                "明确说另一台音箱时才传 other。"
                "【铁律】音量类指令直接执行，绝对不要反问「您是要调音量吗」「哪台音箱」之类的问题。"
            ),
            "parameters": {"type": "object", "properties": {
                "percent": {"type": "integer", "description": "音量百分比 0-100"},
                "which": {"type": "string", "enum": ["self", "other"],
                          "description": "默认 self（正在说话的这台）"},
            }, "required": ["percent"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_speaker_volume",
            "description": "查询正在说话的这台音箱的当前音量。用户问「现在音量多少」时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# ---------- 意图识别框架（完整意图体系，驱动路由/工具/对话管理） ----------
# 用户每句话先经 classify_intent 分类到意图体系（域→意图→实体→路由），
# 框架按意图调度：哪些走 Flash 直答、哪些进深通道、哪些放行原生、
# 哪些带 HA 工具、对话该继续还是结束。全部由模型意图识别驱动，无关键词表。

INTENT_SCHEMA = {
    "domain": "意图域",
    "intent": "具体意图",
    "entities": "实体参数（设备名/媒体名/时间等）",
    "route": "路由建议",
    "dialog_expected": "对话状态预期",
}

# 意图体系（域 → 意图 → 处理策略）
# route: native=放行官方小爱 / flash=Flash直答 / deep=深通道 / flash_tools=Flash+工具
INTENT_TAXONOMY = {
    "device_control": {
        "turn_on_off": {"route": "flash_tools", "desc": "开关设备（灯/空调/风扇/扫地机器人等）"},
        "adjust": {"route": "flash_tools", "desc": "调节参数（温度/亮度/风速/模式等）"},
        "query_state": {"route": "flash_tools", "desc": "查询设备状态（温度多少/灯开着没）"},
        "scene": {"route": "flash_tools", "desc": "场景联动（离家模式/回家模式等）"},
    },
    "media": {
        "play_music": {"route": "flash_tools", "desc": "点歌/放歌/来点音乐/播放某首歌/听XX的歌——网易云优先，搜不到用 web_audio_play；对播放的抱怨（没加载出来/一直卡着/放不出来/怎么没声音）也归此类，重跑工具重试"},
        "personal_playlist": {"route": "flash_tools", "desc": "每日推荐/我喜欢的歌/我的歌单——网易云账号专属"},
        "play_story": {"route": "flash", "desc": "讲故事/儿歌/相声——AI 直接讲"},
        "play_radio": {"route": "flash_tools", "desc": "电台/广播——本地 web_audio_play 搜索电台流"},
        "playback_control": {"route": "flash_tools", "desc": "暂停/继续/音量——本地 speaker_music_control"},
        "play_audio_resource": {"route": "deep", "desc": "播放特殊音频资源（角色语音片段/特定音效/预告片等网易云B站都搜不到的东西）"},
        "ambient_sound": {"route": "flash_tools", "desc": "白噪音/下雨声/助眠音——本地 web_audio_play 搜索"},
    },
    "reminder": {
        "set_alarm": {"route": "flash_tools", "desc": "设闹钟/叫我起床——本地提醒队列"},
        "set_reminder": {"route": "flash_tools", "desc": "提醒我做什么——本地提醒队列"},
        "set_timer": {"route": "flash_tools", "desc": "倒计时/计时——本地提醒队列"},
        "query_alarm": {"route": "flash_tools", "desc": "查闹钟/还剩多久——reminder_list"},
        "cancel_alarm": {"route": "flash_tools", "desc": "取消闹钟/提醒——reminder_cancel"},
    },
    "query_time": {
        "ask_time": {"route": "flash_tools", "desc": "现在几点"},
        "ask_date": {"route": "flash_tools", "desc": "今天几号/星期几"},
    },
    "weather": {
        "ask_weather": {"route": "flash_tools", "desc": "天气/气温/下雨/穿衣建议"},
    },
    "knowledge": {
        "encyclopedia": {"route": "flash", "desc": "百科/常识/概念解释（我知道的）"},
        "general_qa": {"route": "flash", "desc": "一般问答"},
    },
    "realtime": {
        "news": {"route": "doubao", "desc": "新闻/热搜/时事"},
        "traffic": {"route": "doubao", "desc": "路况/限行/出行"},
        "stock_finance": {"route": "doubao", "desc": "股票/基金/行情/汇率"},
        "sports_score": {"route": "doubao", "desc": "体育比分/赛况"},
        "package_logistics": {"route": "deep", "desc": "快递/物流查询"},
        "price_info": {"route": "doubao", "desc": "商品价格/演出信息"},
        "game_updates": {"route": "deep", "desc": "游戏版本/新角色/新内容"},
    },
    "chitchat": {
        "greet": {"route": "flash", "desc": "你好/早上好/打招呼"},
        "farewell": {"route": "flash", "desc": "再见/拜拜/退下/没事了——结束对话"},
        "self_intro": {"route": "flash", "desc": "你是谁/你叫什么/你能做什么"},
        "emotion": {"route": "flash", "desc": "表达心情（开心/难过/无聊）"},
        "small_talk": {"route": "flash", "desc": "闲聊陪伴"},
    },
    "dialogue_mgmt": {
        "confirm": {"route": "flash", "desc": "好的/行/可以/要——确认上一轮反问"},
        "deny": {"route": "flash", "desc": "不用/算了/不要——否定上一轮反问"},
        "stop_interrupt": {"route": "native_instant", "desc": "闭嘴/停下/别说了——立即打断"},
        "cancel": {"route": "flash", "desc": "取消当前操作"},
        "repeat": {"route": "flash", "desc": "再说一遍/没听清"},
        "continue_talk": {"route": "flash", "desc": "然后呢/继续说"},
    },
    "deep_task": {
        "analysis_plan": {"route": "deep", "desc": "分析/方案/评估/盘点——需要深度思考"},
        "search_research": {"route": "deep", "desc": "联网搜索/查资料/研究"},
        "file_operation": {"route": "deep", "desc": "电脑文件操作（创建/修改/整理）"},
        "creative_writing": {"route": "deep", "desc": "写作/创作/代码"},
        "multi_step": {"route": "deep", "desc": "多步复杂任务"},
    },
    "fallback": {
        "unknown": {"route": "flash", "desc": "无法分类——Flash 保守直答"},
    },
}

INTENT_PROMPT = (
    "你是家庭语音管家的意图分类器。把用户的话分类到意图体系，只输出一个 JSON 对象，"
    "不要任何解释。\n"
    "意图体系（domain.intent）：\n"
    "· device_control.turn_on_off（开关设备）/ device_control.adjust（调温度亮度风速等）"
    "/ device_control.query_state（查设备状态）/ device_control.scene（场景）\n"
    "· media.play_music / media.personal_playlist（每日推荐/我喜欢的歌/我的歌单）"
    " / media.play_story / media.play_radio（电台/广播点播，不含新闻）"
    "/ media.playback_control（暂停继续上下曲音量）"
    "/ media.play_audio_resource（角色语音片段/特定音效等特殊音频资源）/ media.ambient_sound（白噪音助眠音"
    "以及晚安/早安/起床这类就寝问候——官方小爱有专属响应）\n"
    "· reminder.set_alarm / reminder.set_reminder / reminder.set_timer / reminder.query_alarm / reminder.cancel_alarm\n"
    "· query_time.ask_time / query_time.ask_date\n"
    "· weather.ask_weather\n"
    "· knowledge.encyclopedia / knowledge.general_qa\n"
    "· realtime.news（新闻/资讯/热搜/时事/大新闻——本地豆包快速搜索播报）"
    "/ realtime.traffic / realtime.stock_finance / realtime.sports_score"
    "/ realtime.package_logistics / realtime.price_info / realtime.game_updates（时效资讯，需要联网）\n"
    "· chitchat.greet / chitchat.farewell（告别结束对话）/ chitchat.self_intro / chitchat.emotion / chitchat.small_talk\n"
    "· dialogue_mgmt.confirm（好的行可以）/ dialogue_mgmt.deny（不用算了）/ dialogue_mgmt.stop_interrupt（闭嘴停下）"
    "/ dialogue_mgmt.cancel / dialogue_mgmt.repeat / dialogue_mgmt.continue_talk（然后呢继续说）\n"
    "· deep_task.analysis_plan / deep_task.search_research / deep_task.file_operation"
    "/ deep_task.creative_writing / deep_task.multi_step（复杂任务）\n"
    "· fallback.unknown（实在分不了）\n\n"
    "输出 JSON 字段：\n"
    '{"domain": "域", "intent": "意图", "entities": {"关键实体": "值"}, '
    '"route": "native|flash|flash_tools|deep|doubao|native_instant", "dialog_expected": "continue|end"}'
    "\n路由建议：设备控制/天气/时间 flash_tools；"
    "国内时效资讯（新闻/行情/路况/比分/价格）doubao（豆包快速搜索）；"
    "快递物流/游戏更新等需要深度检索的时效任务与复杂任务 deep；"
    "媒体播放/提醒（点歌/暂停/闹钟）flash_tools（本地工具）；闲聊知识对话 flash。\n"
    "dialog_expected：用户这话说完，本轮播报后音箱该继续听（continue，比如在聊天中）"
    "还是结束对话（end，比如告别、一次性指令）。\n"
    "只输出 JSON："
)

def classify_intent(text: str, pending_ctx: str = "") -> dict:
    """意图识别：把用户输入分类到意图体系。失败返回保守 fallback（flash 直答）。"""
    prompt = INTENT_PROMPT + "\n\n" + (pending_ctx + "\n" if pending_ctx else "") \
        + f"用户说：{text}"
    try:
        data = call_llm([{"role": "user", "content": prompt}], None)
        reply = (data.get("content") or "").strip()
        # 容错提取 JSON（模型可能带 ```json 围栏）
        m = re.search(r"\{.*\}", reply, re.DOTALL)
        if not m:
            raise ValueError(f"无 JSON: {reply[:80]}")
        result = json.loads(m.group(0))
        domain = str(result.get("domain", "fallback"))
        intent = str(result.get("intent", "unknown"))
        # 容错：模型偶发输出 domain.domain.intent 三段式（如 chitchat.chitchat.farewell）
        if "." in intent:
            intent = intent.rsplit(".", 1)[-1]
        if domain not in INTENT_TAXONOMY or intent not in INTENT_TAXONOMY[domain]:
            print(f"[bridge] 意图越界 {domain}.{intent} → fallback", flush=True)
            domain, intent = "fallback", "unknown"
        result["domain"] = domain
        result["intent"] = intent
        # 路由由框架的意图路由表决定（INTENT_TAXONOMY），模型的 route 建议只记录不采用——
        # 路由是框架的确定性知识，不能让模型每次漂移。
        result["route"] = INTENT_TAXONOMY[domain][intent]["route"]
        result["dialog_expected"] = str(result.get("dialog_expected") or "end")
        return result
    except Exception as e:
        print(f"[bridge] 意图识别失败({type(e).__name__}): {text[:40]} → fallback", flush=True)
        return {"domain": "fallback", "intent": "unknown",
                "entities": {}, "route": "flash", "dialog_expected": "end"}

# ---------- 对话控制（输出侧意图识别：判断播报后是否保持对话） ----------

def strip_bbcode(text: str) -> str:
    """清洗模型偶尔输出的 BBCode/标签杂质（如 [size=2]...[/size]），语音播报不能有。"""
    text = re.sub(r"\[/?[a-zA-Z][^\]]*\]", "", text)
    return text

def intent_check_dialogue(answer_text: str) -> str:
    """意图识别：判断这段播报说完后对话该继续还是结束。
    独立调用 Flash（与生成分离，判断更准）；失败默认 end（安全：绝不让音箱一直听）。
    返回 "keep_open" 或 "end"。"""
    prompt = (
        "你是家庭语音管家的对话状态判断器。管家刚刚对用户播报了下面这段话。\n"
        "判断播报结束后对话状态，只回复一个词：\n"
        "· keep_open：播报是在向用户提问、让用户选择、问用户意见、邀请用户继续说话"
        "（如「想聊点什么？」「要 A 还是 B？」「您说」），用户很可能要接话；\n"
        "· end：播报是收尾、陈述、执行结果，或者用户明显要结束对话，说完对话就结束。\n"
        "宁可选 end，也不要让音箱一直听。\n\n播报内容：\n" + answer_text[:600]
    )
    try:
        data = call_llm([{"role": "user", "content": prompt}], None)
        reply = (data.get("content") or "").strip()
        if "keep_open" in reply:
            return "keep_open"
        if "end" in reply:
            return "end"
        # 模型没按格式回：看结尾是不是问句（最后兜底，宁缺毋滥）
        if re.search(r"[?？]|[吗呢么]$", reply[-4:]) or answer_text.rstrip()[-1:] in "?？":
            return "keep_open"
    except Exception as e:
        print(f"[bridge] 对话意图识别失败({type(e).__name__})，默认 end", flush=True)
    return "end"

# ---------- 原生执行通道工具（把音箱原生小爱的 NLP 执行能力暴露给 AI） ----------

MIGPT_NATIVE_URL = "http://127.0.0.1:4398/native"

def native_device_command(command: str) -> str:
    """把设备指令文本交给音箱原生小爱执行（本地直连米家设备，1-2 秒完成）。"""
    payload = json.dumps({"text": command, "silent": True}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MIGPT_NATIVE_URL, payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {BRIDGE_SECRET}"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "原生小爱已执行" if data.get("ok") else "原生执行失败"

NATIVE_DEVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "native_device_command",
        "description": (
            "把设备控制指令交给音箱的原生小爱执行（走它的本地 NLP 和设备直连通道，"
            "通常 1-2 秒完成，比 HA 云端链路更快）。"
            "command=自然语言指令文本（如「把空调调到25度」「打开卧室的灯」）。"
            "适合单句设备控制；涉及多设备或需要精确验证的用 HA 工具（entity_action）。"
            "【铁律】用户说出的设备指令，原生小爱会先自己执行——你要先查状态验证"
            "它是否已执行到位（见 get_entity），已到位就只确认、绝不重复执行。"
            "只有确认原生没执行时，才用本工具或 entity_action 补执行。"
        ),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "设备指令文本"},
        }, "required": ["command"]},
    },
}

# ---------- Mac 本机只读文件工具（快通道直接查电脑文件，无需升级深通道） ----------

COMPUTER_ROOT = os.path.expanduser("~")
# 涉及隐私/密钥的目录一律不许进（只读工具也不许看）
COMPUTER_BLOCKED = [
    ".ssh", ".dsh", ".gnupg", ".aws", ".credentials.yaml", ".zsh_history",
    ".bash_history", ".git", ".config", ".netrc", ".npmrc",
    ".gitconfig", ".git-credentials", ".zshrc", ".bashrc", ".zprofile",
    "Library/Keychains", "Library/Cookies", "Library/Logs",
    "Library/Mail", "Library/Safari", "Library/Application Support/Google",
    "Library/Application Support/Firefox", "Library/Application Support/Arc",
    "Library/Containers/com.apple.mail",
]

def _resolve_computer_path(raw: str) -> str:
    """把用户/模型给的路径规整成绝对路径，限制在 Mac 主目录内。

    防 symlink 逃逸（TOCTOU）：沿**原始路径**在根目录之下的每个分量
    lstat，任何分量是符号链接都拒绝——realpath 会把链接解析掉，必须先
    于 realpath 检查。根目录自身之上的系统符号链接（如 macOS /var →
    /private/var）属可信系统路径，不检查。
    """
    raw = (raw or "").strip()
    if not raw:
        raw = COMPUTER_ROOT
    raw = os.path.expanduser(raw)
    if not raw.startswith("/"):
        raw = os.path.join(COMPUTER_ROOT, raw)
    norm = os.path.normpath(raw)  # 不解析 symlink，只规整 ../ 等
    root_raw = os.path.normpath(COMPUTER_ROOT)
    root_real = os.path.realpath(COMPUTER_ROOT)
    real = os.path.realpath(norm)
    if real != root_real and not real.startswith(root_real + "/"):
        raise ValueError("路径越界")
    # 逐分量 symlink 检查（原始路径，root_raw 之下）
    if norm.startswith(root_raw + "/"):
        rel_raw = norm[len(root_raw):].strip("/")
        if rel_raw:
            cur = root_raw
            for part in rel_raw.split("/"):
                cur = os.path.join(cur, part)
                if os.path.islink(cur):
                    raise ValueError("路径包含符号链接，拒绝访问")
    rel = real[len(root_real):].strip("/")
    for blocked in COMPUTER_BLOCKED:
        if rel == blocked or rel.startswith(blocked + "/"):
            raise ValueError("该目录不提供访问")
    return real

def list_computer_files(path: str = "") -> str:
    """列出 Mac 电脑上某目录的文件（名字、大小、修改时间），供语音回答。
    隐藏文件（点开头）默认不列出（避免把 .ssh/.git 等敏感名字暴露给模型）。"""
    try:
        real = _resolve_computer_path(path)
    except ValueError as e:
        return f"[拒绝访问: {e}]"
    if not os.path.isdir(real):
        return f"[不是目录: {path or COMPUTER_ROOT}]"
    try:
        entries = sorted(os.listdir(real), key=str.lower)
    except OSError as e:
        return f"[读取失败: {e}]"
    visible = [n for n in entries if not n.startswith(".")]
    hidden_n = len(entries) - len(visible)
    lines = [f"目录 {real} 共 {len(visible)} 项"
             + (f"（另有 {hidden_n} 个隐藏文件未列出）" if hidden_n else "") + "："]
    for name in visible[:60]:
        full = os.path.join(real, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        kind = "文件夹" if os.path.isdir(full) else "文件"
        size = f"{st.st_size // 1024}KB" if st.st_size >= 1024 else f"{st.st_size}B"
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d")
        lines.append(f"- {name}（{kind}，{size}，{mtime}）")
    if len(visible) > 60:
        lines.append(f"…其余 {len(visible) - 60} 项未列出")
    return "\n".join(lines)

def read_computer_file(path: str) -> str:
    """读取 Mac 电脑上某个文本文件的内容（最多 8KB），供语音回答。"""
    try:
        real = _resolve_computer_path(path)
    except ValueError as e:
        return f"[拒绝访问: {e}]"
    if not os.path.isfile(real):
        return f"[不是文件: {path}]"
    try:
        size = os.path.getsize(real)
        if size > 8 * 1024 * 1024:
            return f"[文件太大（{size // 1024 // 1024}MB），不读]"
        # O_NOFOLLOW：校验后到打开之间若被换成符号链接，打开即失败（防 TOCTOU）
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(real, flags)
        try:
            with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
                content = f.read(8 * 1024)
        except Exception:
            os.close(fd)
            raise
    except OSError as e:
        return f"[读取失败: {e}]"
    head = content if len(content) < 8 * 1024 else content + "\n…（内容截断）"
    return f"文件 {real}：\n{head}"

def search_computer_files(keyword: str, under: str = "") -> str:
    """按文件名关键词在 Mac 电脑上搜索文件/文件夹（默认搜常用目录，最多 20 条）。"""
    kw = (keyword or "").strip()
    if not kw:
        return "[请提供搜索关键词]"
    # 默认只搜常用目录（全盘 os.walk 太慢，且 Library 里都是系统文件）
    bases = [os.path.join(COMPUTER_ROOT, d) for d in
             ("Desktop", "Downloads", "Documents", "文稿", "deepseek")]
    if under:
        try:
            bases = [_resolve_computer_path(under)]
        except ValueError as e:
            return f"[拒绝访问: {e}]"
    skip_dirs = {"node_modules", ".git", ".venv", "venv", "__pycache__",
                 ".cache", "Library"}
    hits = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for name in files + dirs:
                if kw.lower() in name.lower():
                    hits.append(os.path.join(root, name))
                    if len(hits) >= 20:
                        return "找到（前 20 条）：\n" + "\n".join(hits)
    return "找到：\n" + "\n".join(hits) if hits else "[没有找到匹配的文件]"

COMPUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_computer_files",
            "description": (
                "列出用户 Mac 电脑上某个目录里的文件和文件夹（名字、大小、修改日期）。"
                "问「电脑桌面/下载/文稿里有什么」「某个文件夹里有哪些文件」之类的问题用这个工具。"
                "常用位置直接写英文路径：桌面=" + COMPUTER_ROOT + "/Desktop、"
                "下载=" + COMPUTER_ROOT + "/Downloads、文稿=" + COMPUTER_ROOT + "/Documents、"
                "主目录=" + COMPUTER_ROOT + "。"
            ),
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_computer_file",
            "description": (
                "读取用户 Mac 电脑上某个文本文件的内容。"
                "问「某个文件里写了什么」「帮我看看某文档/某笔记/某配置文件」用这个工具。"
            ),
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_computer_files",
            "description": (
                "按文件名关键词在用户 Mac 电脑上搜索文件/文件夹。"
                "问「找一下某个文件在哪」「有没有叫某某的文件」用这个工具。"
            ),
            "parameters": {"type": "object",
                           "properties": {"keyword": {"type": "string"},
                                          "under": {"type": "string"}},
                           "required": ["keyword"]},
        },
    },
]

COMPUTER_TOOL_HANDLERS = {
    "list_computer_files": lambda a: list_computer_files(str(a.get("path", ""))),
    "read_computer_file": lambda a: read_computer_file(str(a.get("path", ""))),
    "search_computer_files": lambda a: search_computer_files(
        str(a.get("keyword", "")), str(a.get("under", ""))),
}

def _load_env(key: str) -> str:
    """HA 相关配置从 config/local.json 读取（不再用 .env 文件）。"""
    if key == "HA_TOKEN":
        return cfg_ha("token") or ""
    if key == "HA_URL":
        return cfg_ha("url") or "http://127.0.0.1:8123"
    return os.environ.get(key, "")

HA_URL = _load_env("HA_URL")

def weather_forecast() -> str:
    """调用 HA weather.get_forecasts 服务（daily），返回紧凑的逐日预报文本。"""
    token = _load_env("HA_TOKEN")
    req = urllib.request.Request(
        f"{HA_URL}/api/services/weather/get_forecasts?return_response",
        json.dumps({"entity_id": WEATHER_ENTITY, "type": "daily"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    forecasts = data.get("service_response", {}).get(WEATHER_ENTITY, {}).get("forecast", [])
    if not forecasts:
        return "[天气数据暂不可用]"
    lines = []
    for item in forecasts[:5]:
        day = datetime.datetime.fromisoformat(item["datetime"])
        cond = CONDITIONS.get(item.get("condition", ""), item.get("condition", ""))
        t_high = round(item.get("temperature", 0))
        t_low = round(item.get("templow", item.get("temperature", 0)))
        rain = item.get("precipitation", 0)
        hum = item.get("humidity")
        part = f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）：{cond}，{t_low}-{t_high}度"
        if rain:
            part += f"，降水 {round(rain, 1)} 毫米"
        if hum is not None:
            part += f"，湿度 {round(hum)}%"
        lines.append(part)
    return "\n".join(lines)

# ---------- 本地媒体播放工具（官方小爱已全禁，点歌/白噪音走 web-audio-play 链路） ----------

WEB_AUDIO_PLAY = os.path.join(_BRIDGE_DIR, "web-audio-play.py")
MIGPT_PLAY_URL_BASE = "http://127.0.0.1:4398"
_last_music_url = ""  # 最近一次播放的音频 URL（继续播放用）
_pending_play = None  # (url, title)：工具找到的音频，等 AI 回答播报完后再播放

def _queue_play(url: str, title: str) -> str | None:
    """登记待播放音频：不立即播放，等 Flash 确认语播报结束后由 _flush_pending_play 播放。
    返回 None（成功）或错误说明（URL 校验失败时拒绝登记）。"""
    if security.validate_audio_url(url) is None:
        return "[播放失败: URL 不合法或指向内网/本机地址]"
    global _pending_play
    _pending_play = (url, title)
    return None

def _flush_pending_play() -> None:
    """把待播放音频推给音箱（在 AI 回答播报完成之后调用，避免音乐被 TTS 打断）。"""
    global _pending_play, _last_music_url
    if not _pending_play:
        return
    url, title = _pending_play
    _pending_play = None
    # 推送前二次校验（登记后到推送间可能被并发改动）
    if security.validate_audio_url(url) is None:
        print(f"[bridge] 播放推送被拒绝(URL 校验失败): {title[:40]}", flush=True)
        return
    _last_music_url = url
    try:
        payload = json.dumps({"url": url}).encode("utf-8")
        req = urllib.request.Request(
            MIGPT_PLAY_URL_BASE + "/play_url", payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {BRIDGE_SECRET}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"[bridge] 播放已推送: {title[:40]}", flush=True)
    except Exception as e:
        print(f"[bridge] 播放推送失败({type(e).__name__}): {title[:40]}", flush=True)

def web_audio_play(query: str) -> str:
    """搜索音频资源：web-audio-play.py --no-play 负责搜索→直链探测，只拿 URL 不播放
    （播放时机交给 Flash 回答播报完成后）。支持点歌、白噪音、电台、故事等。"""
    try:
        proc = subprocess.run(
            [sys.executable, WEB_AUDIO_PLAY, "--no-play", query],
            capture_output=True, text=True, timeout=90,
            cwd=os.path.dirname(WEB_AUDIO_PLAY),
        )
    except subprocess.TimeoutExpired:
        return "[播放失败] 搜索音频超时，请换一个说法或稍后再试"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = re.search(r"\[web-audio\] URL: (\S+)", out)
    title_m = re.search(r"\[web-audio\] (?:bilibili|generic|auto): (.+)", out)
    if proc.returncode != 0 or not m:
        detail = out.strip().splitlines()[-1:] or ["无输出"]
        return f"[播放失败] {detail[0][:120]}"
    title = title_m.group(1).strip() if title_m else ""
    err = _queue_play(m.group(1), title)
    if err:
        return err  # URL 校验失败（内网/本机地址等）：如实告知，不登记播放
    return f"已找到：{title}" if title else "已找到音频资源"

# ---------- 网易云音乐工具（netease-music CLI，网易云账号自备，正版音源） ----------
# 反封号：CLI 内置 ≥5s 节流；桥侧加缓存（搜索结果 10 分钟、播放 URL 15 分钟）防重复调用。

NETEASE_MUSIC = cfg_paths("netease_music_cli") or "/usr/local/bin/netease-music"
_netease_search_cache = {}   # query -> (ts, songs)
_netease_url_cache = {}      # song_id -> (ts, url, level)
_netease_playlists_cache = [0, None]  # (ts, [{"id","name","trackCount"}...])

def _netease_run(args: list, timeout: int = 40) -> str:
    """跑 netease-music CLI，返回 stdout 文本；失败抛 RuntimeError。"""
    try:
        proc = subprocess.run(
            [NETEASE_MUSIC] + args,
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ,
                 "PATH": os.path.dirname(NETEASE_MUSIC) + ":" + os.path.dirname(NODE) + ":" + os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("网易云接口超时")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError((proc.stderr or out or "无输出").strip()[-150:])
    return out

def _netease_search_songs(query: str) -> list:
    """搜索歌曲（带 10 分钟缓存）。返回 [{id,name,artists,album,duration}]。"""
    now = time.time()
    hit = _netease_search_cache.get(query)
    if hit and now - hit[0] < 600:
        return hit[1]
    out = _netease_run(["search", query, "--limit", "3"])
    data = json.loads(out)
    songs = data.get("songs") or []
    _netease_search_cache[query] = (now, songs)
    return songs

def _netease_get_url(song_id: int) -> str:
    """拿播放直链（带 15 分钟缓存，URL 官方时效约 20 分钟）。"""
    now = time.time()
    hit = _netease_url_cache.get(song_id)
    if hit and now - hit[0] < 900:
        return hit[1]
    out = _netease_run(["url", str(song_id), "--level", "lossless"])
    data = json.loads(out)
    url = (data.get("url") or "").strip()
    if not url:
        raise RuntimeError("这首歌拿不到播放链接（可能无版权或仅试听）")
    _netease_url_cache[song_id] = (now, url)
    return url

def _netease_pick_song(songs: list, query: str) -> dict | None:
    """从搜索结果挑可播的版本：排除试听（fee>=8）；query 带歌手名时严格匹配歌手
    （翻唱者匹配不到 → 返回 None 降级其他平台找原唱），只有歌名时取第一个可播版。"""
    candidates = [s for s in songs if (s.get("fee") or 0) < 8]
    if not candidates:
        return None
    tokens = [t for t in re.split(r"[\s\-/]+", query) if len(t) >= 2]
    # 歌名词 = 与候选歌名主体精确/前缀匹配的 token（去掉括号注释，防「[原唱:周杰伦]」误判）
    song_name_tokens = set()
    for t in tokens:
        for s in candidates:
            base = re.sub(r"[(\[（].*$", "", str(s.get("name") or "")).strip()
            if base and (t == base or base.startswith(t)):
                song_name_tokens.add(t)
                break
    singer_tokens = [t for t in tokens if t not in song_name_tokens]
    for token in singer_tokens:
        hit = [s for s in candidates if token in str(s.get("artists") or "")]
        if hit:
            return hit[0]
    if singer_tokens:
        # 用户点名了歌手但网易云没有该歌手的可播版本 → 降级其他平台找原唱
        return None
    # query 只含歌名（无歌手词）→ 取第一个可播版本
    return candidates[0]

def netease_music_play(query: str) -> str:
    """搜索网易云歌曲并播放第一首可播版本：search → url → 登记待播放。
    网易云无版权/搜不到时自动降级 web_audio_play（B 站/浏览器）找音源——
    一次工具调用完成点歌，省掉模型第二轮往返（点歌更快）。"""
    try:
        songs = _netease_search_songs(query)
        if songs:
            song = _netease_pick_song(songs, query)
            if song:
                url = _netease_get_url(song["id"])
                err = _queue_play(url, f"{song.get('name', '')} - {song.get('artists', '')}")
                if not err:
                    name = song.get("name", "")
                    artists = str(song.get("artists") or "").replace("/", "、")
                    return f"已找到：{name}" + (f" - {artists}" if artists else "")
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError) as e:
        print(f"[bridge] 网易云失败({str(e)[:80]})，降级B站: {query[:30]}", flush=True)
    # 无版权/搜不到/接口失败/URL 校验失败 → 自动降级 B 站搜索
    return web_audio_play(query)

def _netease_get_playlists() -> list:
    """拿歌单列表（缓存 30 分钟）。"""
    now = time.time()
    if _netease_playlists_cache[1] is not None and now - _netease_playlists_cache[0] < 1800:
        return _netease_playlists_cache[1]
    out = _netease_run(["playlists"])
    data = json.loads(out)
    pls = data.get("playlists") or data.get("list") or []
    norm = [{"id": p.get("id"), "name": p.get("name", ""),
             "count": p.get("count", p.get("trackCount", 0))} for p in pls if p.get("id")]
    _netease_playlists_cache[0], _netease_playlists_cache[1] = now, norm
    return norm

def netease_music_personal(which: str) -> str:
    """每日推荐/红心/歌单：which=daily（每日推荐第一首）/liked（红心随机一首）/playlists（列歌单）。"""
    try:
        if which == "playlists":
            pls = _netease_get_playlists()
            if not pls:
                return "[网易云] 没有找到歌单"
            lines = [f"· {p['name']}（{p['count']}首）" for p in pls[:10]]
            return "您的网易云歌单：\n" + "\n".join(lines)
        if which == "daily":
            out = _netease_run(["daily", "--limit", "5"])
            songs = json.loads(out).get("songs") or []
            if not songs:
                return "[网易云] 每日推荐拿不到"
            song = _netease_pick_song(songs, "") or songs[0]
            url = _netease_get_url(song["id"])
            err = _queue_play(url, f"{song.get('name', '')} - {song.get('artists', '')}")
            if err:
                return err
            return f"已找到每日推荐：{song.get('name', '')}"
        if which == "liked":
            out = _netease_run(["liked", "--limit", "10"])
            songs = json.loads(out).get("songs") or []
            if not songs:
                return "[网易云] 红心列表拿不到"
            song = _netease_pick_song(songs, "") or random.choice(songs)
            url = _netease_get_url(song["id"])
            err = _queue_play(url, f"{song.get('name', '')} - {song.get('artists', '')}")
            if err:
                return err
            return f"已找到您红心的歌：{song.get('name', '')}"
        return "[参数错误] which 只支持 daily/liked/playlists"
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError) as e:
        return f"[网易云失败] {str(e)[:120]}"

def netease_music_playlist(name_or_id: str) -> str:
    """点播歌单：按歌单名关键词匹配（或直接 id），播放歌单第一首。"""
    try:
        pls = _netease_get_playlists()
        target = None
        if name_or_id.isdigit():
            target = next((p for p in pls if str(p["id"]) == name_or_id), None)
        else:
            matches = [p for p in pls if name_or_id in p["name"]]
            target = matches[0] if matches else None
        if not target:
            return f"[网易云] 没有找到歌单「{name_or_id}」"
        out = _netease_run(["playlist", str(target["id"]), "--limit", "20"])
        songs = json.loads(out).get("songs") or []
        if not songs:
            return f"[网易云] 歌单「{target['name']}」是空的"
        song = _netease_pick_song(songs, "") or songs[0]
        url = _netease_get_url(song["id"])
        err = _queue_play(url, f"{song.get('name', '')} - {song.get('artists', '')}")
        if err:
            return err
        return f"已找到歌单「{target['name']}」的第一首：{song.get('name', '')}"
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError) as e:
        return f"[网易云失败] {str(e)[:120]}"

def netease_music_lyric(query: str) -> str:
    """查歌词：搜索第一首的歌词，返回前 16 行。"""
    try:
        songs = _netease_search_songs(query)
        if not songs:
            return "[网易云搜不到] 没有找到相关歌曲"
        song = songs[0]
        out = _netease_run(["lyric", str(song["id"])])
        data = json.loads(out)
        lrc = data.get("lyric") or (data.get("lrc") or {}).get("lyric") or ""
        # LRC 每行形如 [00:01.00]歌词文本：去掉时间戳与空行
        lines = []
        for ln in lrc.splitlines():
            body = re.sub(r"^\[[0-9:.]+\]\s*", "", ln.strip())
            if body:
                lines.append(body)
        if not lines:
            return f"[网易云] 「{song.get('name','')}」没有歌词（可能纯音乐）"
        return f"「{song.get('name','')}」歌词前几句：\n" + "\n".join(lines[:16])
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError) as e:
        return f"[网易云失败] {str(e)[:120]}"

NETEASE_PLAY_TOOL = {
    "type": "function",
    "function": {
        "name": "netease_music_play",
        "description": (
            "点歌播放（网易云音乐正版音源，首选工具）。用户说「放XX的歌」「播放XX」"
            "「来首XX」等具体点歌需求时用：query=歌名或歌手名（如「周杰伦 七里香」）。"
            "找到可播放的正版版本后自动排队播放。"
            "返回以 [网易云搜不到]/[网易云无版权]/[网易云失败] 开头时，"
            "改用 web_audio_play 工具在视频平台找（如周杰伦的歌网易云多无版权）。"
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "歌名或歌手关键词"},
        }, "required": ["query"]},
    },
}

NETEASE_PERSONAL_TOOL = {
    "type": "function",
    "function": {
        "name": "netease_music_personal",
        "description": (
            "网易云账号个性化音乐。用户说「放每日推荐/今天推荐什么歌」→which=daily；"
            "「放我喜欢的歌/放红心歌单」→which=liked；「我有哪些歌单」→which=playlists。"
        ),
        "parameters": {"type": "object", "properties": {
            "which": {"type": "string", "enum": ["daily", "liked", "playlists"]},
        }, "required": ["which"]},
    },
}

NETEASE_PLAYLIST_TOOL = {
    "type": "function",
    "function": {
        "name": "netease_music_playlist",
        "description": (
            "点播网易云歌单。用户说「放我的XX歌单」（如健身/纯音乐/开车）时用："
            "name_or_id=歌单名关键词。播歌单第一首。"
        ),
        "parameters": {"type": "object", "properties": {
            "name_or_id": {"type": "string", "description": "歌单名关键词或歌单 id"},
        }, "required": ["name_or_id"]},
    },
}

NETEASE_LYRIC_TOOL = {
    "type": "function",
    "function": {
        "name": "netease_music_lyric",
        "description": (
            "查歌词。用户问「XX的歌词是什么」时用：query=歌名。返回歌词前几句。"
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "歌名关键词"},
        }, "required": ["query"]},
    },
}

def speaker_music_control(action: str) -> str:
    """暂停/继续音箱正在播放的音频（miplayer）。"""
    global _last_music_url
    if action == "pause":
        cmd = ("ubus call mediaplayer player_play_operation '{\"action\":\"stop\"}' 2>/dev/null; "
               "mphelper pause 2>/dev/null; "
               "for i in 1 2 3 4 5; do kill -9 $(/bin/pidof miplayer) 2>/dev/null; "
               "/bin/pidof miplayer >/dev/null 2>&1 || break; sleep 0.3; done; true")
    elif action == "resume" and _last_music_url:
        # 继续 = 重新拉上次的流（miplayer 无 resume，直接重播）。
        # URL 登记/推送时已过 SSRF 校验；这里二次校验并做单引号转义
        # （URL 必须 http(s) 且不含 ' 等 shell 元字符），杜绝注入。
        url = _last_music_url
        if (security.validate_audio_url(url) is None
                or "'" in url or '"' in url or "$" in url or "\\" in url):
            print(f"[bridge] resume 拒绝不安全 URL: {security.safe_url(url)}", flush=True)
            return "[操作失败] 上次的播放地址不安全，无法继续"
        cmd = f"( miplayer -f '{url}' >/dev/null 2>&1 & )"
    elif action == "resume":
        return "[暂停中无曲目] 现在没有正在播放的内容"
    else:
        return "[参数错误] action 只支持 pause/resume"
    try:
        payload = json.dumps({"cmd": cmd}).encode("utf-8")
        req = urllib.request.Request(
            MIGPT_PLAY_URL_BASE + "/exec", payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {BRIDGE_SECRET}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
        ok = d.get("ok", True)
        return "已暂停播放" if action == "pause" else "已继续播放" if ok else "操作失败"
    except Exception as e:
        return f"[操作失败] {type(e).__name__}"

WEB_AUDIO_PLAY_TOOL = {
    "type": "function",
    "function": {
        "name": "web_audio_play",
        "description": (
            "搜索音频资源（点歌/来点音乐/白噪音/电台/某首歌/某类音乐），找到后自动排队播放。"
            "官方小爱已停用，所有播放需求都用这个工具：传用户想听的内容关键词。"
            "网易云工具失败或报无版权/搜不到时，用这个工具在视频平台找音频（可能找到原唱/翻唱）。"
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "想听的歌/音乐/声音的关键词"},
        }, "required": ["query"]},
    },
}

SPEAKER_MUSIC_TOOL = {
    "type": "function",
    "function": {
        "name": "speaker_music_control",
        "description": (
            "控制音箱正在播放的音频：pause=暂停播放，resume=继续播放。"
            "用户说「暂停/停一下/别放了」→pause；「继续/接着放」→resume。"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["pause", "resume"]},
        }, "required": ["action"]},
    },
}

# ---------- 本地提醒工具（官方小爱已全禁，闹钟/提醒走桥侧队列 + 到点播报） ----------

REMINDER_FILE = os.path.join(RUNTIME_DIR, "speaker-reminders.json")
_reminder_lock = threading.Lock()

def _load_reminders() -> list:
    try:
        with open(REMINDER_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []

def _save_reminders(items: list) -> None:
    with _reminder_lock:
        try:
            atomic_write_json(REMINDER_FILE, items)
        except OSError:
            pass

def reminder_set(time_iso: str, text: str) -> str:
    """设本地提醒：time_iso 形如 2026-08-23T09:00:00（Flash 已换算好本地时间）。"""
    try:
        when = datetime.datetime.fromisoformat(time_iso)
    except ValueError:
        return "[时间格式错误] 请给出明确的日期时间"
    if when <= datetime.datetime.now():
        return "[时间已过] 提醒时间必须是未来时间"
    items = _load_reminders()
    items.append({"id": uuid.uuid4().hex[:8], "time": when.isoformat(),
                  "text": text, "origin": text})
    _save_reminders(items)
    return f"已设提醒：{when.month}月{when.day}日 {when.hour}点{when.minute:02d}分，{text}"

def reminder_list() -> str:
    items = _load_reminders()
    if not items:
        return "现在没有待触发的提醒"
    lines = []
    for it in sorted(items, key=lambda x: x["time"]):
        when = datetime.datetime.fromisoformat(it["time"])
        lines.append(f"· {when.month}月{when.day}日 {when.hour}点{when.minute:02d}分：{it['text']}")
    return "\n".join(lines)

def reminder_cancel(keyword: str = "") -> str:
    """取消提醒：keyword 为空=全部取消，否则按文本包含关键词匹配。"""
    items = _load_reminders()
    if keyword:
        keep = [it for it in items if keyword not in it["text"]]
        removed = len(items) - len(keep)
        _save_reminders(keep)
        return f"已取消 {removed} 条提醒" if removed else "没有找到匹配的提醒"
    _save_reminders([])
    return f"已取消全部 {len(items)} 条提醒"

def _reminder_loop() -> None:
    """后台线程：每 10 秒检查提醒队列，到点推送音箱播报。
    先推送、成功才从文件删除（推送失败保留原条目，下次轮询重试——
    避免「已删但没播出来」的提醒丢失）。"""
    while True:
        try:
            items = _load_reminders()
            now = datetime.datetime.now()
            due = [it for it in items
                   if _reminder_past(it.get("time"), now)]
            if due:
                pushed = []
                for it in due:
                    try:
                        push_to_migpt(f"先生，提醒您：{it.get('text', '')}")
                        pushed.append(it["id"])
                    except Exception:
                        print(f"[bridge] 提醒推送失败，保留待重试: {it.get('text', '')[:30]}",
                              flush=True)
                if pushed:
                    _save_reminders([it for it in items if it["id"] not in pushed])
        except Exception:
            pass
        time.sleep(10)

def _reminder_past(time_iso: str, now: datetime.datetime) -> bool:
    """提醒时间是否已到（解析失败视为未到，避免误触发/误删）。"""
    try:
        return datetime.datetime.fromisoformat(time_iso) <= now
    except (TypeError, ValueError):
        return False

REMINDER_SET_TOOL = {
    "type": "function",
    "function": {
        "name": "reminder_set",
        "description": (
            "设置本地提醒/闹钟（官方小爱已停用）。用户说「X点提醒我做什么」「明天早上叫我」"
            "「设个闹钟」「倒计时」时用。time_iso 用本地时间换算成 ISO 格式"
            "（如 2026-08-23T09:00:00，今天、明天、上午下午都要换算对）。"
        ),
        "parameters": {"type": "object", "properties": {
            "time_iso": {"type": "string", "description": "提醒时间，ISO 格式 YYYY-MM-DDTHH:MM:SS"},
            "text": {"type": "string", "description": "提醒内容"},
        }, "required": ["time_iso", "text"]},
    },
}

REMINDER_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "reminder_list",
        "description": "查询待触发的本地提醒列表。用户问「我设了哪些提醒/闹钟」时用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

REMINDER_CANCEL_TOOL = {
    "type": "function",
    "function": {
        "name": "reminder_cancel",
        "description": "取消提醒。用户说「取消提醒/取消闹钟」时用；keyword 传提醒内容关键词，取消全部则留空。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "提醒内容关键词，留空=全部取消"},
        }, "required": []},
    },
}

def mcp_call_tool(name: str, arguments: dict) -> str:
    if name == "get_weather":
        return weather_forecast()
    if name == "get_now_time":
        return get_now_time()
    if name == "native_device_command":
        return native_device_command(str(arguments.get("command", "")))
    if name == "set_speaker_volume":
        return set_speaker_volume(int(arguments.get("percent", 50)),
                                 str(arguments.get("which", "self")))
    if name == "get_speaker_volume":
        return get_speaker_volume()
    if name == "web_audio_play":
        return web_audio_play(str(arguments.get("query", "")))
    if name == "netease_music_play":
        return netease_music_play(str(arguments.get("query", "")))
    if name == "netease_music_personal":
        return netease_music_personal(str(arguments.get("which", "")))
    if name == "netease_music_playlist":
        return netease_music_playlist(str(arguments.get("name_or_id", "")))
    if name == "netease_music_lyric":
        return netease_music_lyric(str(arguments.get("query", "")))
    if name == "speaker_music_control":
        return speaker_music_control(str(arguments.get("action", "pause")))
    if name == "reminder_set":
        return reminder_set(str(arguments.get("time_iso", "")),
                            str(arguments.get("text", "")))
    if name == "reminder_list":
        return reminder_list()
    if name == "reminder_cancel":
        return reminder_cancel(str(arguments.get("keyword", "")))
    if name in COMPUTER_TOOL_HANDLERS:
        return COMPUTER_TOOL_HANDLERS[name](arguments)
    if name in _evolved_tools:
        return run_evolved_tool(name)
    result = mcp_rpc("tools/call", {"name": name, "arguments": arguments}, 99)
    parts = []
    for item in result.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    text = "\n".join(parts).strip()
    if result.get("isError"):
        return f"[工具错误] {text}"
    return text or "[工具无输出]"

# ---------- 大模型快模型直连 ----------

def call_llm(messages: list, tools: list | None) -> dict:
    """调 deepseek-v4-flash，返回 assistant message dict。"""
    payload = {"model": LLM_MODEL, "messages": messages, "thinking": {"type": "disabled"}}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {load_llm_key()}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"大模型错误: {data['error']}")
    return data["choices"][0]["message"]

def stream_llm(messages: list, tools: list | None):
    """流式调大模型：逐 token yield ('delta', text)；结束时 yield ('tool_calls', [...])。"""
    payload = {"model": LLM_MODEL, "messages": messages, "stream": True,
               "thinking": {"type": "disabled"}}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {load_llm_key()}"},
        method="POST",
    )
    tool_calls: dict = {}
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if "error" in obj:
                raise RuntimeError(f"大模型错误: {obj['error']}")
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield ("delta", content)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                entry = tool_calls.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    entry["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    entry["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]
    if tool_calls:
        yield ("tool_calls", [tool_calls[i] for i in sorted(tool_calls)])

def run_tools_parallel(tool_calls: list) -> list:
    """并行执行 MCP 工具调用，返回 [(tool_call, result_text), ...]。"""
    names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
    print(f"[bridge] 工具调用: {names}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(6, max(1, len(tool_calls)))) as ex:
        futures = {}
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            futures[ex.submit(mcp_call_tool, fn.get("name", ""), args)] = tc
        out = []
        for fut in concurrent.futures.as_completed(futures):
            tc = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = f"[工具调用异常] {type(e).__name__}"
            out.append((tc, res))
        return out

def ask_fast_direct(question: str, pending_ctx: str = "") -> str:
    """快速通道：直连 Flash + MCP 工具循环。"""
    tools = get_openai_tools()
    prompt = build_fast_prompt(question, pending_ctx)
    messages = [{"role": "user", "content": prompt}]
    for _ in range(MAX_TOOL_TURNS):
        msg = call_llm(messages, tools)
        messages.append(msg)  # 原样回传（含 reasoning_content/tool_calls）
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return (msg.get("content") or "").strip()
        for tc, result_text in run_tools_parallel(tool_calls):
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result_text[:4000],
            })
    return "抱歉，我查得有点绕，请换个说法再问一次。"

# ---------- dsh（深度通道 + 快速兜底） ----------

def _log_error(which: str, proc: subprocess.CompletedProcess) -> None:
    try:
        with open(f"{LOG_DIR}/bridge-dsh-error.log", "a", encoding="utf-8") as f:
            f.write(f"\n==== {datetime.datetime.now()} [{which}] rc={proc.returncode} ====\n")
            f.write("STDOUT:\n" + (proc.stdout or "(空)") + "\n")
            f.write("STDERR:\n" + (proc.stderr or "(空)") + "\n")
    except OSError:
        pass

PLAYBACK_INSTRUCTION = (
    "你有在音箱上播放音频的能力：如果用户要「播放」某个音乐/音频片段，"
    "用 web_search 尽力找到该音频的可公开直链资源（http(s):// 开头、"
    "以 .mp3/.m4a/.aac/.flac/.wav 结尾的 URL，能从浏览器直接下载的才算）。\n"
    "· 找到直链 → 在你的最终回答末尾单独附一行：【播放】<完整URL>\n"
    "  桥会自动把该 URL 交给音箱播放，你同时在回答里告诉用户「找到了，这就放」。\n"
    "· 找不到直链（如 B 站视频只有页面链接、或资源需要登录/会员）→ 不要附【播放】，"
    "诚实告诉用户资源在哪能找到、为什么音箱放不了，并给出可行的替代方案。\n"
    "· 绝不编造 URL。"
)

EVOLUTION_INSTRUCTION = (
    "API 调用、数据查询套路等），在最终回答的最后附上一个【技能】段落，"
    "方便以后同类任务直接按流程走；纯一次性问答或纯知识回答不要附。格式：\n"
    "【技能】\n"
    "名称: <kebab-case 英文名>\n"
    "何时使用: <一句话说明什么场景用它>\n"
    "步骤:\n"
    "1. <具体命令/工具/参数>\n"
    "2. ...\n"
    "【技能结束】\n"
    "如果这个流程还能固化成无状态、只读的快速工具，在【技能结束】后再附一个【工具】段落：\n"
    "【工具】\n"
    '{"name": "<英文名>", "description": "<一句话>", "steps": [{"http": {"method": "GET", "path": "/api/states/xxx"}}]}\n'
    "【工具结束】\n"
    "steps 里的元素二选一：{\"http\": {...}} 只允许请求本机 Home Assistant "
    "（" + HA_URL + "，path 以 /api/ 开头，鉴权自动附加）；"
    "{\"shell\": {\"command\": \"...\"}} 只允许只读查询命令，禁止修改、删除、重启类操作。"
)

MEMORY_INSTRUCTION = (
    "你有长期记忆能力（memory 工具，Memory Evolve）。\n"
    "· 写入：交互中了解到值得长期记住的事（用户的偏好、家庭成员、家里设备情况、"
    "约定等），用 memory 工具写入自己的记忆——用户相关事实用 target=user，"
    "需要在以后自动注入的关键事实用 target=key（可附 summary 摘要）。"
    "你已经配置为自主模式，写入直接生效。\n"
    "· 读取：回答需要背景信息时，先查自己的记忆（memory 工具 list 各轨）；"
    "自己记忆里没有的，可以读取用户的 DSH 主记忆（只读，绝不能修改）："
    "目录 " + USER_MAIN_MEMORY_DIR + "/ 下 USER.md 是用户档案、"
    "daily/*.md 是按日期的日志、projects/*/ 下是各项目的 MEMORY.md/KEY.md。\n"
    "· 优先级：先用自己的记忆，查不到再读用户的主记忆，找到后若值得长期记住"
    "就顺手写入自己的记忆（以后就不用再翻主记忆了）。\n"
    "· 回答顺序铁律：先把该写的记忆、该沉淀的技能都写完（工具调用），"
    "然后你的最后一条消息 = 给用户的完整答复本身（包含全部查到的结果和结论，"
    "必须把用户问题的实质答案说全：查到的信息、版本、资源、结论一样都不能少）。"
    "绝对禁止把「已记下/我记住了」当作答复——记记忆是后台动作，"
    "用户要听的是他问题的答案。如果某轮只有记忆写入而没有实质回答，"
    "那轮就是失败的：记忆写了多少，回答就要讲多少。\n"    "例如问邮箱就写出邮箱地址、问分析就写出分析结论）；"
    "最后一条消息绝不能只是「已查看完毕/记下了/好的」之类的确认话术。"
)

def ask_dsh(question: str, fast: bool, context: str = "") -> str:
    """dsh headless 通道（fast=flash patch，否则深度默认配置）。

    会话存到隔离的 DSH_HOME_SPEAKER（音箱发起的会话不进用户的 DSH 列表）。
    深通道附带音箱专用技能库与长期记忆指令（只归音箱用，不污染用户 DSH）。
    context = 话题历史（续聊时注入，让深通道接着之前的话题答）。
    深通道 cwd = 音箱项目文件夹（project 记忆落地于此）。
    """
    parts = [p for p in (load_persona(),) if p]
    if fast:
        parts.append(router_instruction())
    else:
        parts.append(MEMORY_INSTRUCTION)
        parts.append(EVOLUTION_INSTRUCTION)
        parts.append(PLAYBACK_INSTRUCTION)
        skills = load_skills_text()
        if skills:
            parts.append("音箱专用技能库（当前任务若与某个技能匹配，按其步骤执行）：\n" + skills)
    if context:
        parts.append(context)
    parts.append(f"用户问题：{question}")
    cmd = [NODE, "--import", "tsx/esm", CLI, "--profile", "headless"]
    if fast:
        cmd += ["--patch", FAST_PATCH]
    cmd.append("\n\n".join(parts))
    env = dict(os.environ)
    env["DSH_HOME"] = DSH_HOME_SPEAKER
    # cwd = 音箱项目文件夹（project 记忆落地于此），但 tsx 需要 checkout 的 tsconfig
    # paths（@deepseek-ai/* 映射到 vendor 源码）与 node_modules（SPEAKER_HOME 下有符号链接）
    env["TSX_TSCONFIG_PATH"] = os.path.join(CHECKOUT, "tsconfig.json")
    proc = subprocess.run(
        cmd, cwd=SPEAKER_HOME, capture_output=True, text=True,
        timeout=90 if fast else TIMEOUT_DEEP, env=env,
    )
    answer = proc.stdout.strip()
    if proc.returncode != 0 or not answer:
        _log_error("fast" if fast else "deep", proc)
        set_brain_state("degraded")  # DSH 挂了 → 降级认知
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"dsh headless 失败: {detail[-3:] if detail else '无输出'}")
    set_brain_state("full")  # DSH 通道正常 → 全功能认知
    return answer

def ask_llm_plain(question: str, pending_ctx: str = "") -> str:
    """快模型纯问答兜底（无工具）：DSH 挂、深通道全挂时保底能答。
    简单问题直答；需要工具/深度的问题诚实告知暂不可用。"""
    messages = [
        {"role": "system", "content": LLM_FALLBACK_SYSTEM},
    ]
    if pending_ctx:
        messages.append({"role": "assistant", "content": pending_ctx})
    messages.append({"role": "user", "content": question})
    msg = call_llm(messages, None)
    return (msg.get("content") or "").strip() or "抱歉，我暂时回答不了。"

LLM_FALLBACK_SYSTEM = (
    (load_persona() or "你是家庭语音管家。") + "\n"
    "当前你的设备控制工具和深度思考通道暂时不可用（后台大脑离线），"
    "只能做基础问答。回答口语化、简短，不超过三句话，中文。"
    "遇到需要查设备、开灯关灯、查询实时数据这类做不了的事，"
    "就说「后台大脑暂时离线了，这类事情请稍后再试」，不要编造。"
    "简单的常识问答、聊天、算术直接回答。"
)

# ---------- 豆包快速搜索通道 ----------

DOUBAO_ASK = cfg_paths("doubao_cli") or ""

def ask_doubao(question: str) -> str:
    """豆包快速搜索（国内实时资讯/热点/行情）：调 doubao-ask CLI 返回 answer 文本。
    豆包网页版是非官方渠道，CLI 内置 6~14s 节流与风控保护（全局状态文件，
    主 DSH 会话与音箱桥共享节流额度）；失败（冷却/风控/超时/非零退出）抛异常，
    由调用方降级深通道。"""
    env = dict(os.environ)
    # launchd 进程 PATH 不含 node（doubao-ask 是 node 脚本，shebang env node）
    env["PATH"] = os.path.dirname(NODE) + ":" + env.get("PATH", "/usr/bin:/bin")
    proc = subprocess.run(
        [DOUBAO_ASK, question],
        capture_output=True, text=True, timeout=55, env=env,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        print(f"[bridge] 豆包退出 {proc.returncode}: {detail[-1:] if detail else '无输出'}",
              flush=True)
        raise RuntimeError(f"doubao exit {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[bridge] 豆包输出非 JSON: {proc.stdout[:120]}", flush=True)
        raise RuntimeError("doubao bad json") from e
    risk_level = int(data.get("risk_level") or 0)
    if data.get("risk") or risk_level >= 2:
        print(f"[bridge] 豆包风控信号(risk_level={risk_level})，当日停用", flush=True)
        raise RuntimeError("doubao risk")
    answer = (data.get("answer") or "").strip()
    if not answer:
        raise RuntimeError("doubao empty answer")
    print(f"[bridge] 豆包返回 {len(answer)} 字 (risk={risk_level})", flush=True)
    return answer

# ---------- 路由 ----------

def answer_question(question: str) -> str:
    start = time.time()
    pending_ctx = topic_state.consume_pending(question)  # 上一轮反问的上下文
    # 意图识别驱动路由（与 answer_stream 一致）
    intent = classify_intent(question, pending_ctx)
    route = intent.get("route", "flash")
    print(f"[bridge] 意图 {intent.get('domain')}.{intent.get('intent')} → {route}: {question[:30]}",
          flush=True)
    # 空调确定性短路（温度/模式/风速/开关机）
    ac_short = _ac_shortcut(question)
    if ac_short:
        return ac_short
    # 扫地机器人确定性短路
    vac_short = _vacuum_shortcut(question)
    if vac_short:
        return vac_short
    # 塔扇/摄像头确定性短路
    fan_short = _fan_shortcut(question)
    if fan_short:
        return fan_short
    cam_short = _camera_shortcut(question)
    if cam_short:
        return cam_short
    if route == "native":
        return "（已交给官方小爱处理）"
    if route == "deep":
        print(f"[bridge] 意图深路由: {question[:40]}", flush=True)
        try:
            deep_ans = ask_dsh(question, fast=False, context=pending_ctx)
            return extract_and_play(deep_ans)
        except Exception as e:
            print(f"[bridge] 深路由失败({type(e).__name__})，大模型兜底: {question[:40]}", flush=True)
            return ask_llm_plain(question, pending_ctx)
    if route == "doubao":
        print(f"[bridge] 豆包路由: {question[:40]}", flush=True)
        try:
            answer = ask_doubao(question)
            polished = polish_for_speech(answer) or answer
            return strip_bbcode(polished)
        except Exception as e:
            print(f"[bridge] 豆包失败({type(e).__name__})，降级深通道: {question[:40]}", flush=True)
            try:
                deep_ans = ask_dsh(question, fast=False, context=pending_ctx)
                return extract_and_play(deep_ans)
            except Exception as e2:
                print(f"[bridge] 深通道也失败({type(e2).__name__})，大模型兜底: {question[:40]}", flush=True)
                return ask_llm_plain(question, pending_ctx)
    try:
        ans = ask_fast_direct(question, pending_ctx)
        print(f"[bridge] 直连 Flash {time.time()-start:.0f}s: {question[:40]}", flush=True)
    except Exception as e:
        print(f"[bridge] 直连失败({type(e).__name__})，退回 dsh 快速通道: {question[:40]}", flush=True)
        try:
            ans = ask_dsh(question, fast=True, context=pending_ctx)
        except Exception as e2:
            print(f"[bridge] dsh 快速通道也失败({type(e2).__name__})，大模型纯问答兜底: {question[:40]}", flush=True)
            ans = ask_llm_plain(question, pending_ctx)
    # 深路由判断与流式路径统一：整段是「深」，或最后一行单独「深」，
    # 或短文本以「深」结尾，或长句末尾带「：深 / ，深 / 。深」的转接声明
    marker = ans.strip().rstrip("。！？!? ，,～~").strip()
    last_line = marker.splitlines()[-1].strip() if marker else ""
    deep_asked = (marker == "深" or last_line == "深"
                  or (marker.endswith("深") and len(marker) <= 6
                      and not re.search(r"[A-Za-z]", marker[:4]))
                  or re.search(r"[：:，,。；;]\s*深$", marker))
    if deep_asked:
        print(f"[bridge] 升级深度通道: {question[:40]}", flush=True)
        try:
            deep_ans = ask_dsh(question, fast=False, context=pending_ctx)
            return extract_and_play(deep_ans)
        except Exception as e:
            print(f"[bridge] 深通道失败({type(e).__name__})，大模型纯问答兜底: {question[:40]}", flush=True)
            return ask_llm_plain(question, pending_ctx)
    # 对话状态与流式路径统一：输出侧意图识别（intent_check_dialogue）判定，
    # 不再用 is_question 启发式；keep_open 才写 pending，end 清理。
    dlg_action = intent_check_dialogue(ans)
    topic_state.record_pending(question, ans, dlg_action)
    _flush_pending_play()  # 回答播报完成后放音乐（工具已找到的音频）
    return ans

# ---------- 流式 + 后台深化（v4 多线架构） ----------

MIGPT_PLAY_URL = "http://127.0.0.1:4398/play"
FILLERS = ["好的。", "嗯。", "好嘞。", "明白。", "好。", "在的，您说。"]
_filler_idx = 0

def next_filler() -> str:
    """垫场词轮换，避免每次都是同一句「好的。」。"""
    global _filler_idx
    filler = FILLERS[_filler_idx % len(FILLERS)]
    _filler_idx += 1
    return filler

def extract_and_play(text: str) -> str:
    """从深通道答案中提取【播放】URL 并交给音箱播放，返回去除该行的文本。
    找不到就原样返回。URL 必须通过 SSRF 校验，否则拒绝播放并保留文本。"""
    m = re.search(r"【播放】\s*(https?://[^\s】]+)", text)
    if not m:
        return text
    url = m.group(1)
    cleaned = re.sub(r"\n?【播放】\s*https?://[^\s】]+\n?", "\n", text).strip()
    if security.validate_audio_url(url) is None:
        print(f"[bridge] 拒绝播放内网/非法 URL: {url[:80]}", flush=True)
        return cleaned + " （播放链接没有通过安全检查，我把文字留给你了。）" if cleaned else cleaned
    try:
        req = urllib.request.Request(
            MIGPT_PLAY_URL.replace("/play", "/play_url"),
            json.dumps({"url": url}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {BRIDGE_SECRET}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            print(f"[bridge] 音箱播放中: {security.safe_url(url)}", flush=True)
        else:
            print(f"[bridge] 播放失败: {security.safe_url(url)} -> {data.get('error', '')}", flush=True)
            cleaned = cleaned + " （播放没成功，链接留给你了。）" if cleaned else cleaned
    except Exception as e:
        print(f"[bridge] play_url 异常({type(e).__name__}): {security.safe_url(url)}", flush=True)
    return cleaned

def push_to_migpt(text: str, turn: int | None = None) -> None:
    """把后台深通道的结果推送到 migpt 服务端补说。
    turn 非空时先做相关性检查：后台任务启动后若用户已开启新对话
    （进程内 turn 代际前进），就把过期结果丢弃（避免音箱突然冒出过时播报
    与当前对话叠声）。判定用进程内代际，绝不读历史文件猜当前轮次。"""
    if turn is not None and turn != current_turn():
        print(f"[bridge] 深任务完成但已开启新对话，丢弃过期推送: turn {turn} < {current_turn()}",
              flush=True)
        return
    # 语音场景限制长度，避免一次念太久
    if len(text) > 200:
        text = text[:200] + "。"
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MIGPT_PLAY_URL, payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {BRIDGE_SECRET}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        print("[bridge] 后台深化结果已推送", flush=True)
    except Exception as e:
        print(f"[bridge] 推送失败: {type(e).__name__}", flush=True)

def topic_choose(question: str, topics: list) -> str:
    """用 Flash 判断新问题与哪个话题相关：返回话题 id（稳定标识，不随排序变化）；
    都不相关返回 ""。返回 id 而非下标——tasks 完成后话题列表可能已重新排序，
    用旧下标 update_topic 会写错话题。"""
    if not topics:
        return ""
    lines = "\n".join(
        f"{i + 1}. {t.get('summary', '')[:60]}" for i, t in enumerate(topics[:TOPIC_MAX_ACTIVE]))
    prompt = (
        "以下是用户之前和家庭语音管家聊过的话题列表（编号+一句话摘要）：\n"
        f"{lines}\n\n"
        f"用户现在的新问题：{question}\n\n"
        "判断新问题是否与上面某个话题相关（同一件事的后续、对之前结果的追问、"
        "接着之前的话题继续聊都算相关）：相关就只回复话题编号（单个数字）；"
        "都不相关只回复 0。不要任何解释。"
    )
    try:
        data = call_llm([{"role": "user", "content": prompt}], None)
        text = (data.get("content") or "").strip()
        m = re.search(r"[0-9]+", text)
        if not m:
            return ""
        idx = int(m.group(0)) - 1
        if 0 <= idx < len(topics):
            return str(topics[idx].get("id", "") or "")
        return ""
    except Exception as e:
        print(f"[bridge] 话题判定失败({type(e).__name__})，按新话题处理", flush=True)
        return ""

def topic_summarize(history: list) -> str:
    """用 Flash 把话题的问答记录压缩成一句话摘要。"""
    text = "\n".join(history[-4:])[:2000]
    prompt = (
        "下面是用户和家庭语音管家之间一个话题的几轮对话记录，"
        "请用一句话（不超过 30 字）概括这个话题在聊什么：\n\n" + text
    )
    try:
        data = call_llm([{"role": "user", "content": prompt}], None)
        s = (data.get("content") or "").strip()
        return s[:60] if s else "未命名话题"
    except Exception:
        return "未命名话题"

# ---------- 自我进化：深通道解决后沉淀可复用流程（技能 + 工具） ----------

def _collect_skills(dirs: list) -> list:
    """收集若干技能目录下的 SKILL.md 内容（目录不存在/不可读跳过）。"""
    parts: list = []
    for base in dirs:
        try:
            for entry in sorted(os.listdir(base)):
                path = os.path.join(base, entry, "SKILL.md")
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as f:
                        parts.append(f.read().strip())
        except OSError:
            continue
    return parts

def load_skills_text() -> str:
    """读取音箱专用技能库内容（注入深通道提示词，让同类任务按既有流程走）。

    合并两个来源：
    - 仓库 skills/speaker-skills/（人工维护的只读精选，不随运行改动）；
    - 运行时 RUNTIME_SKILLS_DIR/（模型沉淀，每次重启保留，不写回公开仓库）。
    运行时同名技能覆盖仓库同名技能（同名 = 模型对该技能的更新优先）。"""
    repo_skills = {os.path.basename(os.path.dirname(p)): p for p in _skill_paths(SKILLS_DIR)}
    runtime_skills = {os.path.basename(os.path.dirname(p)): p for p in _skill_paths(RUNTIME_SKILLS_DIR)}
    runtime_skills.update(repo_skills)  # 运行时优先
    parts: list = []
    for _name, path in sorted(runtime_skills.items()):
        try:
            with open(path, encoding="utf-8") as f:
                parts.append(f.read().strip())
        except OSError:
            continue
    if not parts:
        return ""
    # 控制注入长度：最多 5 个技能、每个截 800 字
    return "\n\n---\n\n".join(p[-800:] for p in parts[-5:])

def _skill_paths(base: str) -> list:
    """列出技能目录下所有 SKILL.md 路径。"""
    try:
        return [os.path.join(base, entry, "SKILL.md")
                for entry in sorted(os.listdir(base))
                if os.path.isdir(os.path.join(base, entry))]
    except OSError:
        return []

def write_skill(block: str) -> None:
    """把【技能】块写成 RUNTIME_SKILLS_DIR/<name>/SKILL.md（标准 frontmatter 格式）。

    写运行时数据目录（SPEAKER_HOME/runtime/speaker-skills），绝不写公开仓库
    的 skills/speaker-skills（防 AI 生成内容污染 git 工作区与供应链）。"""
    name = desc = ""
    for ln in block.splitlines():
        key, sep, val = ln.partition(":")
        if not sep:
            continue
        if key.strip() in ("名称", "name") and not name:
            name = val.strip()
        elif key.strip() in ("何时使用", "何时使用/触发场景", "description") and not desc:
            desc = val.strip()
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not name or len(name) > 40:
        print(f"[bridge] 技能沉淀跳过（名称无效）", flush=True)
        return
    path = os.path.join(RUNTIME_SKILLS_DIR, name, "SKILL.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = f"---\nname: {name}\ndescription: {desc or name}\n---\n\n{block.strip()}\n"
    atomic_write_text(path, content)
    print(f"[bridge] 已沉淀技能: {name}", flush=True)

# 演化工具注册表（进程内 + evolved-tools.json 持久化，模块加载时初始化）

def load_evolved_tools() -> dict:
    try:
        with open(EVOLVED_TOOLS_FILE, encoding="utf-8") as f:
            tools = json.load(f)
        if isinstance(tools, list):
            return {t["name"]: t for t in tools if isinstance(t, dict) and t.get("name")}
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return {}

_evolved_tools: dict = load_evolved_tools()

def register_evolved_tool(spec_json: str) -> None:
    """把【工具】块注册成快速通道可调用的工具（声明式 HTTP 步骤，安全校验）。

    安全边界（模型输出不可信，默认全拒）：
    - 只接受 http 步骤，且仅 GET 方法（只读）；
    - path 必须 /api/ 开头且总长 ≤200；
    - 步骤数 ≤4、单步字段有限、data 字段一律拒绝（GET 无 body）；
    - 沉淀文件写运行时目录（RUNTIME_DIR），绝不写公开仓库。
    """
    global _tools_cache
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError:
        print("[bridge] 工具沉淀跳过（JSON 无效）", flush=True)
        return
    name = str(spec.get("name", "")).strip()
    desc = str(spec.get("description", "")).strip()[:500]
    steps = spec.get("steps")
    if (not name or not re.fullmatch(r"[a-z_][a-z0-9_]*", name)
            or not desc or not isinstance(steps, list) or not steps):
        print("[bridge] 工具沉淀跳过（字段无效）", flush=True)
        return
    if len(steps) > 4:
        print("[bridge] 工具沉淀跳过（步骤过多）", flush=True)
        return
    for s in steps:
        if not isinstance(s, dict) or len(s) > 2:
            print("[bridge] 工具沉淀跳过（步骤格式非法）", flush=True)
            return
        h = s.get("http")
        if not isinstance(h, dict):
            print("[bridge] 工具沉淀跳过（仅支持 http 步骤）", flush=True)
            return
        path = str(h.get("path", ""))
        method = str(h.get("method", "GET")).upper()
        if (not path.startswith("/api/") or len(path) > 200
                or method != "GET" or h.get("data") is not None):
            print("[bridge] 工具沉淀跳过（http 步骤越界：仅允许 GET /api/ 只读）", flush=True)
            return
    tools = load_evolved_tools()
    tools[name] = {"name": name, "description": desc, "steps": steps}
    atomic_write_json(EVOLVED_TOOLS_FILE, list(tools.values()))
    _evolved_tools.clear()
    _evolved_tools.update(tools)
    _tools_cache = None  # 让工具列表缓存重建
    print(f"[bridge] 已沉淀工具: {name}", flush=True)

def run_evolved_tool(name: str) -> str:
    """执行演化工具（声明式 HTTP 步骤：仅 GET /api/，只读，SSRF 由 path 白名单兜底）。"""
    spec = _evolved_tools.get(name) or {}
    parts = []
    for s in spec.get("steps", []):
        h = s.get("http")
        if not isinstance(h, dict):
            continue
        path = str(h.get("path", ""))
        if not path.startswith("/api/"):
            continue  # 已注册时校验过；防御重复检查
        url = HA_URL + path
        req = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {_load_env('HA_TOKEN')}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            parts.append(resp.read().decode("utf-8", "replace")[:4000])
    return "\n".join(parts) if parts else "[工具无输出]"

def process_evolution(answer: str) -> str:
    """拆分深通道回答：剥离【技能】【工具】块并沉淀，返回面向用户的部分。"""
    if "【技能】" not in answer and "【工具】" not in answer:
        return answer.strip()
    for block in re.findall(r"【技能】(.*?)【技能结束】", answer, re.DOTALL):
        write_skill(block)
    for block in re.findall(r"【工具】\s*(\{.*?\})\s*【工具结束】", answer, re.DOTALL):
        register_evolved_tool(block)
    user_part = re.sub(r"【技能】.*?【技能结束】", "", answer, flags=re.DOTALL)
    user_part = re.sub(r"【工具】.*?【工具结束】", "", user_part, flags=re.DOTALL)
    return user_part.strip() or "这个问题我想好了，但还没来得及整理成语音。"

def polish_for_speech(text: str, with_dialogue: bool = False):
    """前台 Flash 把深通道的结论润色成适合语音播报的短句（用户听到的是汇报版）。
    with_dialogue=True 时返回 (润色文本, 对话状态)，状态由 Flash 意图识别：
    润色后的播报若在反问/邀请用户继续 → keep_open，否则 end。"""
    if not text or len(text) <= 60:
        return (text, "end") if with_dialogue else text
    prompt = (
        "你是家庭语音管家。下面是一段后台助手完成用户任务后得到的结论，"
        "请把它改写成适合语音播报的口头汇报：口语化、亲切、保留全部关键信息"
        "（数字、结论、名单都要在），去掉技术细节和废话，控制在 100 字以内，"
        "称呼用户为「先生」（禁止「主人」「亲」等称呼），"
        "直接输出改写后的文本，不要任何解释或前缀：\n\n" + text[:3000]
    )
    if with_dialogue:
        prompt += (
            "\n\n同时判断播报完音箱的对话状态，只回复一个词："
            "keep_open（播报是在向用户提问、让用户选择或邀请继续）"
            "或 end（播报是收尾/陈述，说完对话结束）。"
        )
    try:
        data = call_llm([{"role": "user", "content": prompt}], None)
        polished = (data.get("content") or "").strip()
        if polished:
            if with_dialogue:
                action = "end"
                if polished.endswith("keep_open"):
                    action = "keep_open"
                    polished = polished[: -len("keep_open")].strip()
                elif polished.endswith("end"):
                    polished = polished[: -len("end")].strip()
                return (polished or text, action)
            return polished
    except Exception as e:
        print(f"[bridge] 润色失败({type(e).__name__})，用原文", flush=True)
    return (text, "end") if with_dialogue else text

def _deep_push(question: str, pending_ctx: str = "", turn: int | None = None) -> None:
    """后台线程：话题判定 → 深通道处理（带话题上下文）→ 前台 Flash 润色 → 推送播报。

    代际语义（turn）：任务启动时捕获自己的 turn；完成时只有 turn 仍等于
    当前代际才执行「影响当前对话」的动作（推送播报 / 写 pending / 更新话题
    的 last_active 排序）。旧任务（用户已说新话）：
      - 仍写 history（事实记录，append-only）；
      - 不推送、不写 pending（pending 只反映当前对话）；
      - 不更新话题（避免旧结果把新话题顶到列表顶部、干扰后续 topic_choose）。
    并发上限：深任务同时最多 3 个（防深通道资源耗尽），超出时排队等待。
    """
    start = time.time()
    topic_state.daily_cleanup()
    # 进度播报：深任务可能跑 1-2 分钟，定时让音箱说一声「还在做」，用户不必干等
    progress_said = {"n": 0}
    PROGRESS_PHRASES = ["还在处理，稍等一下。", "这个有点复杂，我再做一会儿。",
                        "快了，马上就好。"]

    def progress_timer() -> None:
        while progress_said["n"] < len(PROGRESS_PHRASES) and not progress_done.is_set():
            if progress_done.wait(timeout=45):
                return
            phrase = PROGRESS_PHRASES[progress_said["n"]]
            progress_said["n"] += 1
            try:
                push_to_migpt(phrase, turn=turn)  # 进度播报同样受代际保护
            except Exception:
                pass

    progress_done = threading.Event()
    timer = threading.Thread(target=progress_timer, daemon=True)
    timer.start()
    try:
        # 1. 判定话题归属：相关则续聊（注入历史上下文），无关则新开。
        #    topic_choose 返回稳定 topic id（不随列表排序变化）。
        with topic_state.topics_lock:
            topics = topic_state.load_topics()
        topic_id = topic_choose(question, topics)
        context = topic_state.topics_context_text(topics, topic_id) if topic_id else ""
        if pending_ctx:
            context = pending_ctx + "\n\n" + context  # 上一轮反问的上下文优先
        if topic_id:
            t = next((x for x in topics if str(x.get("id", "")) == topic_id), None)
            print(f"[bridge] 续聊话题: {t['summary'][:30] if t else topic_id}", flush=True)
        # 2. 深通道执行
        answer = ask_dsh(question, fast=False, context=context)
        print(f"[bridge] 后台深化 {time.time()-start:.0f}s: {question[:40]}", flush=True)
        user_text = process_evolution(answer)
        # 2.5 播放协议：答案里若有【播放】URL，交给音箱播放并从文本中摘除
        user_text = extract_and_play(user_text)
        # 代际判定（在写 history 之前）：当前是否仍是本任务的轮次
        still_current = (turn is None or turn == current_turn())
        # 3. 历史记录：append-only 事实记录（旧任务也写，但用 deep 标记）
        topic_state.record_history(question, user_text, "deep")
        if still_current:
            # 4. 更新话题档案（新话题建档 / 旧话题按 id 追加+重新摘要）
            with topic_state.topics_lock:
                topic_state.save_topics(topic_state.update_topic(topic_state.load_topics(), topic_id, question, user_text, summarize_fn=topic_summarize))
        else:
            print(f"[bridge] 深任务完成但已开启新对话，仅记历史不推送: {question[:30]}",
                  flush=True)
        polished, dlg_action = polish_for_speech(user_text, with_dialogue=True)
        # 5. 待答复状态：与流式路径同一判定来源（keep_open 才写，end 清理）；
        #    旧任务不写 pending（pending 只反映当前对话）。
        if still_current:
            topic_state.record_pending(question, polished, dlg_action)
        DEEP_PREFIXES = ["我查清楚了，", "先生，我这边办好了，", "结果出来了，"]
        if still_current:
            push_to_migpt(DEEP_PREFIXES[progress_said["n"] % len(DEEP_PREFIXES)]
                          + polished + f"<<dialogue:{dlg_action}>>", turn=turn)
    except subprocess.TimeoutExpired:
        print(f"[bridge] 后台深化超时({time.time()-start:.0f}s): {question[:40]}", flush=True)
        push_to_migpt("这个任务比较复杂，一次没做完。你再说一遍，我继续处理。", turn=turn)
    except Exception as e:
        print(f"[bridge] 后台深化失败({type(e).__name__}): {question[:40]}", flush=True)
        # 媒体类问题（点歌/白噪音等）降级到本地播放工具（网易云/web_audio_play），
        # 不依赖深通道；其他问题走大模型纯问答兜底。
        media_fallback = _media_local_fallback(question)
        if media_fallback:
            push_to_migpt(media_fallback, turn=turn)
        else:
            try:
                fallback = ask_llm_plain(question, "")
                push_to_migpt(fallback, turn=turn)
            except Exception:
                push_to_migpt("本地大脑刚刚没接上这个任务，你再说一遍试试。", turn=turn)
    finally:
        progress_done.set()
        _deep_slots.release()

# 深任务并发上限（信号量：同时最多 3 个后台深任务，超出排队）
_deep_slots = threading.BoundedSemaphore(3)

def _spawn_deep(question: str, pending_ctx: str = "", turn: int | None = None) -> None:
    """带并发上限地启动后台深任务（超过 3 个时阻塞等待空位，绝不无限堆积）。"""
    _deep_slots.acquire()
    threading.Thread(target=_deep_push, args=(question, pending_ctx, turn),
                     daemon=True).start()

def _media_local_fallback(question: str) -> str:
    """深通道失败时，媒体类问题降级到本地播放工具（不依赖 DSH 深通道）。
    返回播报文本；非媒体问题或降级失败返回空串。"""
    media_keywords = ("播放", "放", "点歌", "来首", "来点音乐", "听", "白噪音", "助眠", "电台", "歌")
    if not any(k in question for k in media_keywords):
        return ""
    # 先试网易云（正版快链）
    try:
        query = question
        for pref in ("播放", "放一首", "放个", "来一首", "来首", "点歌"):
            query = query.replace(pref, " ", 1)
        query = " ".join(query.split()).strip()
        if not query:
            return ""
        result = netease_music_play(query)
        if not result.startswith("[网易云"):
            _flush_pending_play()
            return "好的先生，给您换成本地播放。" + result.replace("已找到：", "马上为您播放")
    except Exception:
        pass
    # 网易云不行 → web_audio_play（B 站/通用浏览器链路）
    try:
        result = web_audio_play(question)
        if not result.startswith("[播放失败]"):
            _flush_pending_play()
            return "好的先生，给您换成本地播放。" + result.replace("已找到：", "马上为您播放")
    except Exception:
        pass
    return ""

def load_speaker_memory() -> str:
    """读取音箱自己的长期记忆（Memory Evolve 存储），注入快速通道提示词。

    只读音箱 home（~/.dsh-speaker/memories）里的记忆文件，不碰用户主记忆。
    """
    files: list = []
    try:
        for root, _dirs, names in os.walk(SPEAKER_MEMORY_DIR):
            for n in names:
                if n.endswith(".md"):
                    files.append(os.path.join(root, n))
    except OSError:
        pass
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    chunks: list = []
    total = 0
    for path in files[:6]:
        try:
            with open(path, encoding="utf-8") as f:
                text = "\n".join(f.read().strip().splitlines()[-15:])
        except OSError:
            continue
        if not text:
            continue
        chunks.append(text)
        total += len(text)
        if total > 2000:
            break
    return "\n\n".join(chunks) if chunks else ""

# ---------- 自我认知（大脑状态） ----------

# 桥启动时默认全功能；每次 dsh 通道成功/失败后更新。
# "full" = DSH 核心在线（工具 + 深度思考齐全）；
# "degraded" = DSH 核心离线（当前直连大模型，无工具无深度）。
_brain_state: str = "full"
_brain_ts: float = time.time()

def set_brain_state(state: str) -> None:
    global _brain_state, _brain_ts
    if state in ("full", "degraded") and state != _brain_state:
        _brain_state = state
        _brain_ts = time.time()
        print(f"[bridge] 大脑状态切换: {state}", flush=True)

def get_brain_state() -> str:
    # 状态长期没刷新（>10 分钟没人碰过 dsh）不主动降级——保守保持上次结论
    return _brain_state

def SELF_AWARENESS_TEXT() -> str:
    """自我认知注入：模型知道自己是直连大模型还是 DSH 核心，回答时如实体现。"""
    if _brain_state == "full":
        return (
            "【自我认知】你运行在本地管家系统上，当前是全功能模式："
            "本地 DSH 大脑核心在线，工具齐全（设备控制、天气、时间、文件等）。"
            "用户若问起你的状态/能力，如实说明「本地大脑核心在线，功能完整」。"
        )
    return (
        "【自我认知】你运行在降级模式：本地 DSH 大脑核心暂时离线，"
        "你当前直连云端大模型回答，没有设备控制工具、没有深度思考，只能基础问答。"
        "用户若问起你的状态/能力，如实说明「本地大脑核心离线，现在是直连云端模式，"
        "功能受限」，不要编造能力。"
    )

def build_fast_prompt(question: str, pending_ctx: str = "") -> str:
    """组装快速通道提示词：人设 + 路由指令 + 自我认知 + 音箱自己的长期记忆 + 最近话题 + 用户问题。"""
    sections = [p for p in (load_persona(), router_instruction()) if p]
    sections.append(SELF_AWARENESS_TEXT())
    if pending_ctx:
        sections.append(pending_ctx)  # 上一轮反问的上下文（用户这轮的回答）
    memory_text = load_speaker_memory()
    if memory_text:
        sections.append("你自己长期记住的事（音箱专属记忆，回答时优先参考）：\n" + memory_text)
    # 最近聊过的话题摘要：新问题若接着之前的话题（如「刚才那个文件呢」），可参考
    try:
        topics = topic_state.load_topics()[:2]
        recent = [t for t in topics if t.get("summary")]
        if recent:
            lines = "\n".join(f"· {t['summary'][:60]}（{t.get('last_active', '')[:10]}）"
                              for t in recent)
            sections.append(
                "最近聊过的话题（用户新问题若接着其中某个话题，先看能否直接回答；"
                "答不了就回「深」）：\n" + lines)
    except OSError:
        pass
    sections.append(f"用户问题：{question}")
    return "\n\n".join(sections)

# ---------- ASR 容错（截断/同音误识别） ----------

ASR_HOMOPHONES = {
    # 常见同音误识别 → 修正（音箱常见，从真实日志收集）
    "冰箱": "小爱", "家电": "小爱", "相爱": "小爱", "宵夜": "小爱",
}

# 典型「说了一半」的残缺句式（ASR 提前结束/用户被打断）
ASR_TRUNCATED_ENDINGS = ["打开", "关上", "关掉", "帮我", "帮我把", "把", "调到", "设置", "查一下", "看一下"]

def asr_repair(question: str) -> str:
    """修正明显误识别；截断句加提示让模型结合上下文猜测最可能的意图。"""
    q = question.strip()
    for wrong, right in ASR_HOMOPHONES.items():
        if q.startswith(wrong):
            q = right + q[len(wrong):]
    # 句子以「打开/帮我把/把」等结尾且没有明确宾语 = 说了一半被截断
    if q.endswith("打开") or q.endswith("关上") or q.endswith("关掉") \
            or q.endswith("帮我把") or q.endswith("调到") or q.endswith("设置"):
        q += "（这句话可能被语音识别截断了，请结合最近的对话上下文猜测用户最可能想做什么，直接执行最合理的那个，不要反问。）"
    elif q in ("帮我", "把", "查一下", "看一下"):
        q += "（这句话可能被语音识别截断了，请结合最近的对话上下文猜测用户最可能想做什么，直接执行最合理的那个，不要反问。）"
    return q

# ---------- 重复问题去重（实测同一问题最多连问 5 次，给用户即时反馈） ----------

_recent_answers: dict = {}  # 规范化问题 → (答案, 时间戳)
_recent_lock = threading.Lock()
DUP_WINDOW_SECONDS = 300  # 5 分钟内同一问题算重复

def _normalize_question(q: str) -> str:
    return re.sub(r"[，。！？,.!? ～~]", "", q).strip()

def check_duplicate(question: str):
    """5 分钟内同一问题再问：yield 上次的答案（带「刚才说过」提示），否则 None。"""
    key = _normalize_question(question)
    with _recent_lock:
        hit = _recent_answers.get(key)
        if hit:
            ans, ts = hit
            if time.time() - ts < DUP_WINDOW_SECONDS:
                return ans
            _recent_answers.pop(key, None)
    return None

def remember_answer(question: str, answer: str) -> None:
    """记录最近一次回答，供重复问题去重。"""
    if not answer or len(answer) > 300:
        return
    key = _normalize_question(question)
    with _recent_lock:
        _recent_answers[key] = (answer, time.time())
        # 防内存膨胀
        if len(_recent_answers) > 100:
            cutoff = time.time() - DUP_WINDOW_SECONDS
            for k in [k for k, (_, t) in _recent_answers.items() if t < cutoff]:
                _recent_answers.pop(k, None)

# 设备指令特征：用于识别设备控制类问题（现在设备指令由 AI 走 HA 通道执行，
# 官方云端执行链路已被音箱端 hook 锁定；识别仅用于统计/日志，不再静默短路）
DEVICE_COMMAND_RE = re.compile(
    r"(开|关|打开|关闭|调|调到|设|设置|亮度|色温|风速|模式|温度|制冷|制热|"
    r"灯|插座|风扇|窗帘|热水器|空调|净化|加湿|除湿|电源|扫地|拖地)")
# 查询句特征：以「查/多少/吗/状态/了没/是什么」结尾或含「查一下/多少度/开着没」——
# 这些是状态查询不是控制指令，不能静默短路
DEVICE_QUERY_RE = re.compile(
    r"(查一下|查查|查询|多少|吗|呢|状态|怎么样|了没|开着没|关着没|是不是|"
    r"几度|是什么|好不好|有没有)")

def is_device_command(question: str) -> bool:
    """判断是否为设备控制指令（原生小爱会并行执行）。查询句不算。
    仅用于日志统计；路由已由意图识别驱动。"""
    if DEVICE_QUERY_RE.search(question):
        return False
    return bool(DEVICE_COMMAND_RE.search(question))

def answer_stream(question: str, turn: int | None = None):
    """流式回答生成器：意图识别驱动路由 → 真流式输出，遇「深」标记转后台深通道。"""
    start = time.time()
    question = asr_repair(question)
    # ---- 意图识别：分类到意图体系，路由/工具/对话状态全部由意图驱动 ----
    pending_ctx = topic_state.consume_pending(question)  # 上一轮反问的上下文（一次性消费）
    intent = classify_intent(question, pending_ctx)
    route = intent.get("route", "flash")
    print(f"[bridge] 意图 {intent.get('domain')}.{intent.get('intent')} → {route}: {question[:30]}",
          flush=True)
    # 空调确定性短路（温度/模式/风速/开关机）
    ac_short = _ac_shortcut(question)
    if ac_short:
        yield ac_short
        yield "<<dialogue:end>>"
        return
    # 扫地机器人确定性短路
    vac_short = _vacuum_shortcut(question)
    if vac_short:
        yield vac_short
        yield "<<dialogue:end>>"
        return
    # 塔扇/摄像头确定性短路
    fan_short = _fan_shortcut(question)
    if fan_short:
        yield fan_short
        yield "<<dialogue:end>>"
        return
    cam_short = _camera_shortcut(question)
    if cam_short:
        yield cam_short
        yield "<<dialogue:end>>"
        return
    if route == "native":
        # 放行官方小爱：migpt 收到标记后不播报，原生自己应答（音乐/闹钟/媒体等）
        yield "<<native_passthrough>>"
        return
    if route == "native_instant":
        # 立即打断类（闭嘴/停下）：migpt 本地处理；桥侧直接结束（不应到达这里，兜底）
        yield "<<native_passthrough>>"
        return
    if route == "deep":
        # 复杂任务/时效资讯：直接进深通道（后台处理 + 垫场语）
        print(f"[bridge] 意图深路由: {question[:40]}", flush=True)
        _spawn_deep(question, pending_ctx, turn)
        yield "这个我得联网查一查，稍等。"
        return
    if route == "doubao":
        # 国内实时资讯：豆包快速搜索（约 10~15s，比深通道快 4 倍以上）。
        # 同步阻塞（filler 垫场已出声）；失败/风控自动降级深通道（后台 push，不再垫第二句）。
        print(f"[bridge] 豆包路由: {question[:40]}", flush=True)
        try:
            yield next_filler()
            answer = ask_doubao(question)
            polished = polish_for_speech(answer) or answer
            polished = strip_bbcode(polished)
            yield polished
            # 对话控制标记：与主路径一致——输出侧意图识别判断是否反问/邀请
            if intent.get("domain") == "chitchat" and intent.get("intent") == "farewell":
                dlg_action = "end"
            else:
                dlg_action = intent_check_dialogue(polished)
            yield f"<<dialogue:{dlg_action}>>"
            topic_state.record_pending(question, polished, dlg_action)  # keep_open 才写，end 清理
            remember_answer(question, polished)
            print(f"[bridge] 豆包 {time.time()-start:.0f}s: {question[:40]}", flush=True)
            return
        except Exception as e:
            print(f"[bridge] 豆包失败({type(e).__name__})，降级深通道: {question[:40]}", flush=True)
            _spawn_deep(question, pending_ctx, turn)
            yield "这个我得联网查一查，稍等。"
            return
    dup = None
    # 状态查询句（结果会变）不走去重缓存；静态问题才复答
    if not DEVICE_QUERY_RE.search(question):
        dup = check_duplicate(question)
    if dup:
        print(f"[bridge] 重复问题直接复答: {question[:40]}", flush=True)
        yield dup
        return
    tools = get_openai_tools()
    prompt = build_fast_prompt(question, pending_ctx)
    messages = [{"role": "user", "content": prompt}]
    yield next_filler()  # 垫场：意图分类后立即出声（native/深路由上面已返回，不会到这里）
    progress_pushed = False  # 工具找到音乐后只垫一句进度语
    for round_idx in range(MAX_TOOL_TURNS):
        full_text = ""          # 本轮完整文本
        tool_calls = []
        # 每轮都先缓冲到轮末再决定放行内容：
        # 「深」路由/过程叙述 = 吞掉；最终回答轮 = 整段放行。
        # 垫场词「好的。」已给用户即时反馈，整轮缓冲的 1-3 秒延迟可接受。
        try:
            for kind, val in stream_llm(messages, tools):
                if kind == "delta":
                    full_text += val
                else:
                    tool_calls = val
        except Exception as e:
            print(f"[bridge] 流式异常({type(e).__name__}): {question[:40]}", flush=True)
            # 大模型工具流挂了 → 大模型纯问答兜底（同样的模型，无工具）
            try:
                yield ask_llm_plain(question, "")
            except Exception:
                yield "抱歉，我的大脑卡了一下，请再说一次。"
            return
        if not tool_calls:
            # 只要模型回「深」——单字，或前面有叙述最后一行单独回「深」——
            # 都升级深通道，绝不把「深」或过程叙述念给用户
            marker = full_text.strip().rstrip("。！？!? ，,～~").strip()
            last_line = marker.splitlines()[-1].strip() if marker else ""
            deep_asked = (marker == "深" or last_line == "深"
                          or (marker.endswith("深") and len(marker) <= 6
                              and not re.search(r"[A-Za-z]", marker[:4]))
                          or re.search(r"[：:，,。；;]\s*深$", marker))
            if deep_asked:
                print(f"[bridge] 后台深化启动: {question[:40]}", flush=True)
                _spawn_deep(question, pending_ctx, turn)
                yield "这个问题让我好好想想，想好了再告诉我。"
                return
            if full_text:
                # 工具轮后的回答容易混入「自问自答/思考」叙述（模型偶尔抽风），
                # 检测到元叙述特征就交给 Flash 润成纯结论；无特征直接放行
                if round_idx > 0 and re.search(r"[，。]?让我确认|按照指令|我应该|不过|家中确|\n", full_text):
                    try:
                        full_text = polish_for_speech(full_text)
                    except Exception:
                        pass
                full_text = strip_bbcode(full_text)
                yield full_text  # 最终回答：整段放行
                # 对话控制标记：意图驱动——
                # ① 用户 farewell/stop 类意图 → 强制 end（告别了就绝不再保持麦克风）；
                # ② 其余场景由输出侧意图识别（intent_check_dialogue）判断播报是否反问/邀请。
                if intent.get("domain") == "chitchat" and intent.get("intent") == "farewell":
                    dlg_action = "end"
                elif intent.get("domain") == "dialogue_mgmt" and intent.get("intent") in ("stop_interrupt", "cancel", "deny"):
                    dlg_action = "end"
                else:
                    dlg_action = intent_check_dialogue(full_text)
                yield f"<<dialogue:{dlg_action}>>"
                topic_state.record_pending(question, full_text, dlg_action)  # keep_open 才写，end 清理
                remember_answer(question, full_text)  # 重复问题去重
                _flush_pending_play()  # 回答播报完成后放音乐（工具已找到的音频）
            print(f"[bridge] 流式 {time.time()-start:.0f}s: {question[:40]}", flush=True)
            return
        # 工具轮：并行执行后继续下一轮
        messages.append({
            "role": "assistant",
            "content": full_text or None,
            "tool_calls": tool_calls,
        })
        for tc, result_text in run_tools_parallel(tool_calls):
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result_text[:4000],
            })
        # 音乐已找到：立即垫一句进度语，覆盖下一轮模型生成回答的静音空档
        # （点歌体验：垫场 → 找到了马上放 → 回答 → 音乐，全程有反馈）
        if _pending_play and not progress_pushed:
            progress_pushed = True
            yield "找到了，马上放。"
    yield "抱歉，我查得有点绕，请换个说法再问一次。"

class Handler(BaseHTTPRequestHandler):
    # 只收本机（migpt）请求；请求体上限 1MB（OpenAI 兼容 JSON，足够用）
    MAX_BODY = 1024 * 1024

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        """读取请求体，校验 Content-Length（负数/缺失/超限拒绝）。返回 None 表示已回错误。"""
        cl = self.headers.get("Content-Length")
        if cl is None or not cl.strip():
            self._json({"error": {"message": "missing content-length"}}, 411)
            return None
        try:
            length = int(cl)
        except ValueError:
            self._json({"error": {"message": "bad content-length"}}, 400)
            return None
        if length < 0:
            self._json({"error": {"message": "bad content-length"}}, 400)
            return None
        if length > self.MAX_BODY:
            self._json({"error": {"message": "payload too large"}}, 413)
            return None
        return self.rfile.read(length)

    def _check_auth(self) -> bool:
        """本机鉴权：/v1/chat/completions 需要 Authorization: Bearer <BRIDGE_SECRET>
        （constant-time 比较）。/v1/models 保持无鉴权——migpt 健康探测依赖它，
        且只暴露模型 id，无敏感信息。"""
        if self.path in ("/v1/models", "/v1/models/"):
            return True
        if not BRIDGE_SECRET:
            # 未配置 secret：拒绝一切写路径（配置缺失是部署错误，不该降级开放）
            self._json({"error": {"message": "bridge auth not configured"}}, 503)
            return False
        auth = self.headers.get("Authorization", "")
        expect = "Bearer " + BRIDGE_SECRET
        if not hmac.compare_digest(auth, expect):
            self._json({"error": {"message": "unauthorized"}}, 401)
            return False
        return True

    def do_GET(self) -> None:
        if self.path in ("/v1/models", "/v1/models/"):
            self._json({"object": "list", "data": [
                {"id": "dsh-local", "object": "model", "owned_by": "dsh"}]})
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self) -> None:
        if self.path not in ("/v1/chat/completions", "/v1/chat/completions/"):
            self._json({"error": {"message": "not found"}}, 404)
            return
        if not self._check_auth():
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            want_stream = bool(payload.get("stream"))
            messages = payload.get("messages", [])
            question = next(
                (m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            ).strip()
            if not question:
                self._json({"error": {"message": "empty user message"}}, 400)
                return
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": {"message": "bad json"}}, 400)
            return

        # 排队而非拒绝：用户连问两个问题很常见（比如打断/追问），
        # 等上一个请求最多 45 秒；等不到才报忙
        if not lock.acquire(timeout=45):
            self._json({"error": {"message": "busy: 上一个问题还在思考中"}}, 503)
            return

        # 每轮用户请求分配进程内代际（turn）：深任务捕获自己的代际，
        # 完成时与当前代际比对，过期结果不推送/不写 pending/不更新话题
        turn = next_turn()

        if want_stream:
            # 真流式：垫场词 → Flash 逐块转发 → 结束
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"

                def emit(delta_text: str | None, finish: str | None = None) -> None:
                    chunk = {
                        "id": cid, "object": "chat.completion.chunk", "created": 0,
                        "model": "dsh-local",
                        "choices": [{"index": 0,
                                     "delta": {"content": delta_text} if delta_text is not None else {},
                                     "finish_reason": finish}],
                    }
                    self.wfile.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()

                # 垫场词由 answer_stream 内部按路由决定（native/深路由不发垫场词）
                answer_parts: list = []
                try:
                    for piece in answer_stream(question, turn):
                        if piece:
                            emit(piece)
                            answer_parts.append(piece)
                except subprocess.TimeoutExpired:
                    emit("抱歉，这个问题我思考太久了，请换个方式再问一次。")
                except Exception as e:
                    emit(f"抱歉，本地大脑出了点问题：{type(e).__name__}")
                answer_text = "".join(answer_parts)
                topic_state.record_history(question, answer_text, "fast")
                emit(None, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                lock.release()
            return

        try:
            answer = answer_question(question)
        except subprocess.TimeoutExpired:
            answer = "抱歉，这个问题我思考太久了，请换个方式再问一次。"
        except Exception as e:
            answer = f"抱歉，本地大脑出了点问题：{type(e).__name__}"
        finally:
            lock.release()
        topic_state.record_history(question, answer, "fast")

        self._json({
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": 0,
            "model": "dsh-local",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def setup(self) -> None:
        super().setup()
        # 慢连接防护：单请求整体 180s 上限（正常回答流远小于此）
        try:
            self.connection.settimeout(180)
        except OSError:
            pass

    def log_message(self, *args) -> None:
        pass  # 安静模式

def main() -> int:
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    # 启动前配置检查（fail fast）：示例配置 / 缺桥鉴权 secret 都拒绝启动，
    # 绝不带着「看起来正常其实没配置」的状态服务
    if is_example():
        print("[bridge] 使用示例配置，拒绝启动：请先复制 config/config.example.json 为"
              " config/local.json 并在配置后台（http://127.0.0.1:8390）填写真实配置后保存。",
              flush=True)
        return 1
    if not BRIDGE_SECRET:
        print("[bridge] 未配置桥鉴权 secret，拒绝启动：请在配置后台保存一次配置"
              "（自动生成 bridge.secret），或手动放置 config/generated/bridge-secret"
              "（内容为 32 位以上随机 hex）。", flush=True)
        return 1
    # 注入话题/待答复/历史存储的运行时路径（topic_state 不读配置）
    topic_state.configure(
        history_file=os.path.join(SPEAKER_HOME, "speaker-history.jsonl"),
        topics_file=os.path.join(SPEAKER_HOME, "speaker-topics.json"),
        pending_file=os.path.join(SPEAKER_HOME, "speaker-pending.json"),
        session_root=os.path.join(DSH_HOME_SPEAKER, "sessions"),
    )
    topic_state.daily_cleanup()  # 启动时清理过期话题档案与 headless 会话文件
    _apply_discovery()  # config 留空的设备实体自动从 HA 发现（日志见 [bridge] 设备自动发现）
    # 保障音箱项目文件夹有 checkout 的 node_modules（tsx 与依赖解析依赖它）；
    # checkout 还没配置/不存在时跳过（深通道不可用，其余通道照常）
    nm_link = os.path.join(SPEAKER_HOME, "node_modules")
    if os.path.isdir(CHECKOUT) and os.path.isdir(SPEAKER_HOME) and not os.path.islink(nm_link):
        try:
            os.symlink(os.path.join(CHECKOUT, "node_modules"), nm_link)
        except OSError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    # 本地提醒后台线程（官方小爱已全禁，闹钟/提醒由桥侧队列 + 到点推送播报）
    threading.Thread(target=_reminder_loop, daemon=True).start()
    print(f"小爱桥 v5: http://127.0.0.1:{port}/v1（垫场+真流式+并行工具+自我进化 → 后台深通道推送）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
