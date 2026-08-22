#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiaoai-dsh 统一配置加载器（桥侧共享）
======================================
所有可配置项来自仓库根 config/local.json（由 localhost 配置后台生成维护；
第一次使用可复制 config/config.example.json 为 config/local.json）。

任何组件（桥、web-audio-play、各工具）都用本模块读取配置，
不要在自己的代码里写死密钥、路径或设备实体。
"""

import json
import os

_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_BRIDGE_DIR)
_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "local.json")
_EXAMPLE_PATH = os.path.join(_REPO_ROOT, "config", "config.example.json")

_config_cache: dict | None = None


def _load() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    for p in (_CONFIG_PATH, _EXAMPLE_PATH):
        try:
            with open(p, encoding="utf-8") as f:
                _config_cache = json.load(f)
            return _config_cache
        except (OSError, json.JSONDecodeError):
            continue
    _config_cache = {}
    return _config_cache


def reload() -> dict:
    """强制重新读取配置（后台保存后可热生效）。"""
    global _config_cache
    _config_cache = None
    return _load()


def cfg_llm(key: str, default=None):
    return _load().get("llm", {}).get(key, default)


def cfg_ha(key: str, default=None):
    return _load().get("home_assistant", {}).get(key, default)


def cfg_mi(key: str, default=None):
    return _load().get("xiaomi_account", {}).get(key, default)


def cfg_speaker(key: str, default=None):
    return _load().get("speaker", {}).get(key, default)


def cfg_mac(key: str, default=None):
    return _load().get("mac", {}).get(key, default)


def cfg_devices(key: str, default=None):
    return _load().get("devices", {}).get(key, default)


def cfg_paths(key: str, default=None):
    return _load().get("paths", {}).get(key, default)


def repo_root() -> str:
    return _REPO_ROOT
