#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""device_discovery 模块回归测试：HA 设备自动发现分组。

运行：python3 -m unittest discover -s bridge -p 'test_*.py' -v
（原 test_bridge.py 的 TestDeviceDiscoveryGrouping 迁移至此。
 discover_devices 返回 {常量名: entity_id}，不再直接改调用方全局。）
"""
import os
import sys
import unittest
from unittest import mock

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

import device_discovery  # noqa: E402


def _configured():
    """模拟主桥的已配置实体（全空 = 全部待发现）。"""
    return {k: "" for k in (
        "AC_TEMP_ENTITY", "AC_MODE_ENTITY", "AC_TURN_ON_ENTITY",
        "AC_TURN_OFF_ENTITY", "AC_FAN_UP_ENTITY", "AC_FAN_DOWN_ENTITY",
        "FAN_ENTITY", "FAN_DELAY_ENTITY", "FAN_ANGLE_ENTITY",
        "CAM1_ON_ENTITY", "CAM2_ON_ENTITY",
        "VACUUM_ENTITY", "VACUUM_MODE_ENTITY",
        "MAIN_LIGHT_ENTITY", "AMBIENT_LIGHT_ENTITY", "SPEAKER_PLAYER",
    )}


class TestDeviceDiscoveryGrouping(unittest.TestCase):
    """#10：按 device_id 分组，防跨设备串台。"""

    def setUp(self):
        device_discovery._discovered.clear()
        device_discovery._discovery_done = False

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
        with mock.patch.object(device_discovery, "ha_states_all", return_value=states), \
             mock.patch.object(device_discovery, "ha_entity_registry", return_value=reg):
            found = device_discovery.discover_devices(_configured(), lambda k: "tok" if k == "HA_TOKEN" else "http://hass", force=True)
        # 多空调不自动绑（宁缺毋滥）
        self.assertNotIn("AC_TEMP_ENTITY", found)
        self.assertNotIn("AC_MODE_ENTITY", found)

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
        with mock.patch.object(device_discovery, "ha_states_all", return_value=states), \
             mock.patch.object(device_discovery, "ha_entity_registry", return_value=reg):
            found = device_discovery.discover_devices(_configured(), lambda k: "tok", force=True)
        self.assertEqual(found.get("AC_TEMP_ENTITY"), "number.ir_1_ir_temperature_p_2_2")
        self.assertEqual(found.get("AC_MODE_ENTITY"), "select.ir_1_ir_mode_p_2_1")
        self.assertEqual(found.get("AC_TURN_ON_ENTITY"), "button.ir_1_turn_on_a_2_6")
        self.assertEqual(found.get("AC_TURN_OFF_ENTITY"), "button.ir_1_turn_off_a_2_5")
        self.assertNotIn("AC_FAN_UP_ENTITY", found)  # 无该实体不绑

    def test_configured_never_overwritten(self):
        """已显式配置的实体绝不覆盖（发现结果里不出现）。"""
        states = [
            {"entity_id": "number.ir_1_ir_temperature_p_2_2", "state": "26"},
            {"entity_id": "select.ir_1_ir_mode_p_2_1", "state": "cool"},
            {"entity_id": "button.ir_1_turn_on_a_2_6", "state": "unavailable"},
        ]
        reg = {s["entity_id"]: "dev-ir" for s in states}
        cfg = _configured()
        cfg["AC_TEMP_ENTITY"] = "number.user_configured_temp"
        with mock.patch.object(device_discovery, "ha_states_all", return_value=states), \
             mock.patch.object(device_discovery, "ha_entity_registry", return_value=reg):
            found = device_discovery.discover_devices(cfg, lambda k: "tok", force=True)
        self.assertNotIn("AC_TEMP_ENTITY", found)
        self.assertIn("AC_MODE_ENTITY", found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
