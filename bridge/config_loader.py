#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiaoai-dsh 统一配置加载器（桥侧共享）
======================================
所有可配置项来自仓库根 config/local.json（由 localhost 配置后台生成维护；
第一次使用可复制 config/config.example.json 为 config/local.json）。

任何组件（桥、web-audio-play、各工具）都用本模块读取配置，
不要在自己的代码里写死密钥、路径或设备实体。

失败策略（明确、不静默）：
- local.json 存在但损坏（JSON 无效 / 不是对象）=> 抛 ConfigError（fail fast），
  绝不静默回退示例配置假装正常；
- local.json 不存在 => 允许回退 config.example.json 作为「首次配置展示」，
  调用方通过 is_example 自行决定是否拒绝启动（服务类组件必须拒绝）。
"""

import json
import os


class ConfigError(Exception):
    """配置缺失/损坏。服务类组件应据此拒绝启动并给出明确指引。"""


_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_BRIDGE_DIR)
_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "local.json")
_EXAMPLE_PATH = os.path.join(_REPO_ROOT, "config", "config.example.json")
_GENERATED_DIR = os.path.join(_REPO_ROOT, "config", "generated")
_BRIDGE_SECRET_FILE = os.path.join(_GENERATED_DIR, "bridge-secret")

_config_cache: dict | None = None
_config_is_example: bool = False


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是 JSON 对象")
    return data


def _load() -> tuple[dict, bool]:
    """返回 (config, is_example)。local 存在且有效 => (local, False)；
    local 存在但损坏 => 抛 ConfigError；local 不存在 => (example, True)。"""
    global _config_cache, _config_is_example
    if _config_cache is not None:
        return _config_cache, _config_is_example
    if os.path.exists(_CONFIG_PATH):
        try:
            cfg = _read_json(_CONFIG_PATH)
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(
                f"config/local.json 存在但无法解析（{type(e).__name__}）。"
                f"请检查文件内容或删除后重新在配置后台保存。"
            ) from e
        _config_cache, _config_is_example = cfg, False
        return cfg, False
    # local 不存在：首次使用，允许用示例配置展示（服务组件须按 is_example 拒绝启动）
    try:
        cfg = _read_json(_EXAMPLE_PATH)
    except (OSError, json.JSONDecodeError):
        raise ConfigError(
            f"config/local.json 与 config/config.example.json 都不可用，无法加载配置"
        ) from None
    _config_cache, _config_is_example = cfg, True
    return cfg, True


def load_config() -> tuple[dict, bool]:
    """供需要原始配置的组件（admin 后台）使用。"""
    return _load()


def is_example() -> bool:
    """当前生效的是否示例配置（服务组件据此拒绝启动）。"""
    return _load()[1]


def reload() -> dict:
    """强制重新读取配置（后台保存后可热生效）。"""
    global _config_cache
    _config_cache = None
    return _load()[0]


def _read_secret_file() -> str:
    """从 config/generated/bridge-secret 读取桥鉴权 secret（admin 生成）。"""
    try:
        with open(_BRIDGE_SECRET_FILE, encoding="utf-8") as f:
            val = f.read().strip()
        return val
    except OSError:
        return ""


def cfg_bridge_secret() -> str:
    """桥与 migpt 之间的本机鉴权 secret。

    读取顺序：local.json 的 bridge.secret（admin 保存时自动生成）
    → config/generated/bridge-secret（兜底，管理员手动放置）。
    两者皆无时返回空串——桥必须以「未配置鉴权」状态拒绝服务 /v1/*（见
    xiaogpt-bridge.py 启动检查），migpt 侧同样要求配置。
    """
    cfg, _ = _load()
    val = str(cfg.get("bridge", {}).get("secret", "") or "").strip()
    if val:
        return val
    return _read_secret_file()


def cfg_llm(key: str, default=None):
    return _load()[0].get("llm", {}).get(key, default)


def cfg_ha(key: str, default=None):
    return _load()[0].get("home_assistant", {}).get(key, default)


def cfg_mi(key: str, default=None):
    return _load()[0].get("xiaomi_account", {}).get(key, default)


def cfg_speaker(key: str, default=None):
    return _load()[0].get("speaker", {}).get(key, default)


def cfg_mac(key: str, default=None):
    return _load()[0].get("mac", {}).get(key, default)


def cfg_devices(key: str, default=None):
    return _load()[0].get("devices", {}).get(key, default)


def cfg_paths(key: str, default=None):
    return _load()[0].get("paths", {}).get(key, default)


def cfg_playback(key: str, default=None):
    """播放/URL 策略段（如 allow_private_urls）。"""
    return _load()[0].get("playback", {}).get(key, default)


def repo_root() -> str:
    return _REPO_ROOT
