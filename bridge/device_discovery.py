#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HA 设备自动发现：config/devices 留空的实体按米家命名规律从 HA 自动匹配。

职责：
- ha_state / ha_states_all / ha_entity_registry：HA REST 读取（state /
  entity registry 设备分组）；
- discover_devices：按 device_id 分组 + 命名特征自动匹配，返回
  {常量名: entity_id} 新发现项（不修改调用方全局；由主桥应用）。

不负责：
- 不录入叫方配置：configured（已显式配置的实体）由主桥传入，本模块只填空缺；
- 不做设备指令执行/短路逻辑（_ac_shortcut 等留在主桥）；
- 不读业务配置：HA URL/token 经 load_env 参数注入（主桥传 _load_env）。

依赖：仅标准库（urllib / json）。
"""

_discovered: dict = {}
_discovery_done = False


def ha_state(entity_id: str, load_env) -> dict:
    token = load_env("HA_TOKEN")
    req = urllib.request.Request(
        f"{load_env('HA_URL')}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ha_states_all(load_env) -> list:
    """拉取 HA 全部实体状态（自动发现用；失败返回空列表，不致命）。"""
    try:
        req = urllib.request.Request(
            f"{load_env('HA_URL')}/api/states",
            headers={"Authorization": f"Bearer {load_env('HA_TOKEN')}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []


def ha_entity_registry(load_env) -> dict:
    """拉取 HA 实体注册表（entity_id → device_id 映射）。
    失败返回空 dict——发现逻辑退化为「按全局唯一性兜底」，不致命。"""
    try:
        req = urllib.request.Request(
            f"{load_env('HA_URL')}/api/config/entity_registry/list",
            headers={"Authorization": f"Bearer {load_env('HA_TOKEN')}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            entries = json.loads(resp.read().decode("utf-8"))
        out = {}
        for e in entries:
            if isinstance(e, dict) and e.get("entity_id"):
                out[e["entity_id"]] = e.get("device_id") or ""
        return out
    except Exception:
        return {}


def discover_devices(configured: dict, load_env, force: bool = False) -> dict:
    """把 config 留空的设备实体自动填上（幂等，启动时调用一次即可）。

    返回本次发现结果 {常量名: entity_id}——只含「主桥当前为空」的项，
    已配置的绝不覆盖。调用方负责应用（如写入主桥模块全局变量）。

    configured: {常量名: 当前已配置的 entity_id 或空串}，如
        {"AC_TEMP_ENTITY": "number.x", "FAN_ENTITY": "", ...}

    防串台（#10）：候选实体先按 HA entity_registry 的 device_id 分组，
    只有「同一个设备」同时满足该设备的完整特征（如红外空调 = 温度 number +
    模式 select + 开关 button 都在同一 device）才绑定；多设备都符合时宁缺毋滥，
    留空并输出诊断日志（提示用户到 config/devices 显式配置），绝不跨设备拼凑。
    """
    global _discovery_done
    if _discovery_done and not force:
        return _discovered
    _discovery_done = True
    found: dict = {}

    def fill(attr: str, value: str) -> None:
        if not configured.get(attr) and value:
            found[attr] = value

    states = ha_states_all(load_env)
    if not states:
        return {}
    registry = ha_entity_registry(load_env)

    by_id: dict = {}  # device_id -> {entity_id: state}
    orphan = []       # 无 device_id 的实体
    for s in states:
        eid = s.get("entity_id", "")
        if not eid:
            continue
        dev = registry.get(eid, "")
        if dev:
            by_id.setdefault(dev, {})[eid] = s
        else:
            orphan.append(s)

    def alive(s) -> bool:
        return s.get("state") not in ("unavailable", "unknown")

    def fname(s) -> str:
        return (s.get("attributes") or {}).get("friendly_name") or ""

    def pick_from(group: dict, pred) -> str:
        cands = [s for s in group.values() if pred(s)]
        alive_c = [s for s in cands if alive(s)]
        if alive_c:
            cands = alive_c
        return cands[0]["entity_id"] if cands else ""

    def log_ambiguous(kind: str, cands: list) -> None:
        print(f"[bridge] 设备自动发现: {kind} 有多个候选设备，为避免串台不自动绑定，"
              f"请到 config/devices 显式配置（候选: {', '.join(cands[:4])}）",
              flush=True)

    # —— 红外空调：特征 = number.ir_temperature + select.ir_mode + button.turn_on/off 同设备 ——
    ac_groups = []
    for dev, group in by_id.items():
        has_temp = any("ir_temperature" in e for e in group)
        has_mode = any("ir_mode" in e for e in group)
        has_on = any("turn_on" in e for e in group)
        has_off = any("turn_off" in e for e in group)
        if has_temp and has_mode and (has_on or has_off):
            ac_groups.append((dev, group))
    if ac_groups:
        if len(ac_groups) == 1:
            _, g = ac_groups[0]
            fill("AC_TEMP_ENTITY", pick_from(g, lambda s: "ir_temperature" in s["entity_id"]))
            fill("AC_MODE_ENTITY", pick_from(g, lambda s: "ir_mode" in s["entity_id"]))
            fill("AC_TURN_ON_ENTITY", pick_from(g, lambda s: "turn_on" in s["entity_id"]))
            fill("AC_TURN_OFF_ENTITY", pick_from(g, lambda s: "turn_off" in s["entity_id"]))
            fill("AC_FAN_UP_ENTITY", pick_from(g, lambda s: "fan_speed_up" in s["entity_id"]))
            fill("AC_FAN_DOWN_ENTITY", pick_from(g, lambda s: "fan_speed_down" in s["entity_id"]))
        else:
            log_ambiguous("红外空调", [f"{g[0][:24]}…" for g in ac_groups])
    # 兼容旧形态：实体没有 device_id（孤儿）时仍按全局唯一性兜底（单候选才绑）
    if not found.get("AC_TEMP_ENTITY") and orphan:
        ac_cands = [s for s in orphan
                    if "ir_temperature" in s["entity_id"]]
        if len(ac_cands) == 1:
            fill("AC_TEMP_ENTITY", ac_cands[0]["entity_id"])
            fill("AC_MODE_ENTITY", pick_from({s["entity_id"]: s for s in orphan},
                                             lambda s: "ir_mode" in s["entity_id"]))
            fill("AC_TURN_ON_ENTITY", pick_from({s["entity_id"]: s for s in orphan},
                                                lambda s: "turn_on" in s["entity_id"]))
            fill("AC_TURN_OFF_ENTITY", pick_from({s["entity_id"]: s for s in orphan},
                                                 lambda s: "turn_off" in s["entity_id"]))

    # —— 塔扇：fan 域 + 同设备带 preset_modes / off_delay_time / swing ——
    fan_groups = []
    for dev, group in by_id.items():
        fans = {e: s for e, s in group.items() if e.startswith("fan.")}
        if fans:
            fan_groups.append((dev, group))
    if fan_groups:
        if len(fan_groups) == 1:
            _, g = fan_groups[0]
            fill("FAN_ENTITY",
                 pick_from(g, lambda s: s["entity_id"].startswith("fan.")
                           and bool((s.get("attributes") or {}).get("preset_modes")))
                 or pick_from(g, lambda s: s["entity_id"].startswith("fan.")))
            fill("FAN_DELAY_ENTITY",
                 pick_from(g, lambda s: "off_delay_time" in s["entity_id"]))
            fill("FAN_ANGLE_ENTITY",
                 pick_from(g, lambda s: "swing" in s["entity_id"]))
        else:
            log_ambiguous("塔扇", [f"{g[0][:24]}…" for g in fan_groups])
    if not found.get("FAN_ENTITY") and orphan:
        fans = [s for s in orphan if s["entity_id"].startswith("fan.")]
        if len(fans) == 1:
            fill("FAN_ENTITY", fans[0]["entity_id"])

    # —— 摄像头：switch 域 + on_p_2_1 结尾 + 名字含摄像，按设备分组 ——
    cam_devs = []
    for dev, group in by_id.items():
        cams = sorted(e for e in group
                      if e.startswith("switch.") and e.endswith("on_p_2_1")
                      and ("摄像" in fname(group[e]) or "摄像" in e))
        if cams:
            cam_devs.append((dev, cams))
    if len(cam_devs) == 1:
        _, cams = cam_devs[0]
        if cams:
            fill("CAM1_ON_ENTITY", cams[0])
        if len(cams) >= 2:
            fill("CAM2_ON_ENTITY", cams[1])
    elif len(cam_devs) > 1:
        log_ambiguous("摄像头", [f"{d[:24]}…" for d, _ in cam_devs])

    # —— 扫地机器人：vacuum 域 + cleaning_mode 模式 select，同设备分组 ——
    vac_groups = []
    for dev, group in by_id.items():
        vacs = {e: s for e, s in group.items() if e.startswith("vacuum.")}
        if vacs:
            vac_groups.append((dev, group))
    if vac_groups:
        if len(vac_groups) == 1:
            _, g = vac_groups[0]
            fill("VACUUM_ENTITY", pick_from(g, lambda s: s["entity_id"].startswith("vacuum.")))
            fill("VACUUM_MODE_ENTITY",
                 pick_from(g, lambda s: "cleaning_mode" in s["entity_id"]))
        else:
            log_ambiguous("扫地机器人", [f"{g[0][:24]}…" for g in vac_groups])

    # —— 灯：只认名字明显的（吸顶灯/大灯/主灯、氛围灯），找不到就留空走通用规则 ——
    fill("MAIN_LIGHT_ENTITY",
         pick_from({s["entity_id"]: s for s in states},
                   lambda s: s["entity_id"].startswith(("switch.", "light."))
                   and ("吸顶灯" in fname(s) or "大灯" in fname(s) or "主灯" in fname(s))
                   and "氛围" not in fname(s) and "指示" not in fname(s)
                   and "indicator" not in s["entity_id"]))
    fill("AMBIENT_LIGHT_ENTITY",
         pick_from({s["entity_id"]: s for s in states}, lambda s: "氛围灯" in fname(s)))
    # 音箱音量：media_player 名字带「音箱/小爱」的第一台
    fill("SPEAKER_PLAYER",
         pick_from({s["entity_id"]: s for s in states},
                   lambda s: s["entity_id"].startswith("media_player.")
                   and ("音箱" in fname(s) or "小爱" in fname(s))))
    if found:
        print(f"[bridge] 设备自动发现 {len(found)} 项："
              + " ".join(sorted(found.values())), flush=True)
    _discovered.update(found)
    return found