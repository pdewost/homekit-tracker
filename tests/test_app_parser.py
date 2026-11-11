"""Test parsing of third-party HomeKit exports (JSON / CSV)."""

import json
import tempfile
import unittest
from pathlib import Path
from src.extractor.app_parser import detect_export_type, parse_app_export


class TestAppParser(unittest.TestCase):
    def test_detect_export_type(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            csv_file = tmp_path / "devices.csv"
            csv_file.write_text("Name,Room,Category\nLamp,Lounge,Lighting\n", encoding="utf-8")
            self.assertEqual(detect_export_type(csv_file), "csv_inventory")

            controller_json = tmp_path / "backup.json"
            controller_json.write_text(json.dumps({"version": 2, "homes": [{"name": "Home"}], "accessories": []}), encoding="utf-8")
            self.assertEqual(detect_export_type(controller_json), "controller_json")

            catalyst_json = tmp_path / "catalyst.json"
            catalyst_json.write_text(json.dumps({"accessories": [], "rooms": []}), encoding="utf-8")
            self.assertEqual(detect_export_type(catalyst_json), "catalyst_json")

    def test_parse_csv_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            csv_file = tmp_path / "homecare_export.csv"
            csv_content = """Name,Room,Category,Manufacturer,Model,Firmware,Battery,Connectivity
Ceiling Light,Lounge,Lighting,Acme,Hue 1.0,1.2.3,,Zigbee
Door Sensor,Hallway,Sensor,Initech,DW-01,2.0.1,85%,Zigbee
Portable Plug,Unassigned,Outlet,Hooli,Energy 2,,100%,Thread
"""
            csv_file.write_text(csv_content, encoding="utf-8")

            household = parse_app_export(csv_file, household_name="Test Apartment")
            self.assertEqual(household.name, "Test Apartment")
            self.assertEqual(len(household.rooms), 2)  # Lounge, Hallway
            self.assertEqual(len(household.accessories), 3)

            # Check battery parsing
            door = [a for a in household.accessories.values() if a.name == "Door Sensor"][0]
            self.assertEqual(door.battery_level, 85)
            self.assertEqual(door.protocol, "Zigbee")

            # Check unassigned accessory
            orphans = household.get_orphan_accessories()
            self.assertEqual(len(orphans), 1)
            self.assertEqual(orphans[0].name, "Portable Plug")

    def test_parse_controller_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            data = {
                "version": 1,
                "homes": [{"name": "Lake Cabin", "uniqueIdentifier": "h-99"}],
                "rooms": [
                    {"name": "Kitchen", "uniqueIdentifier": "r-10"},
                    {"name": "Living Room", "uniqueIdentifier": "r-20"},
                ],
                "accessories": [
                    {
                        "name": "Kitchen Light",
                        "uniqueIdentifier": "a-101",
                        "roomIdentifier": "r-10",
                        "category": "Lighting",
                        "manufacturer": "Umbrella",
                        "model": "Matter Bulb",
                        "services": [
                            {
                                "name": "Light Service",
                                "uniqueIdentifier": "s-101",
                                "serviceType": "Lightbulb",
                                "characteristics": [
                                    {"name": "On", "characteristicType": "PowerState", "value": True}
                                ]
                            }
                        ]
                    }
                ],
                "scenes": [
                    {
                        "name": "Cooking Time",
                        "uniqueIdentifier": "sc-10",
                        "actions": [
                            {"accessoryIdentifier": "a-101", "characteristicType": "PowerState", "targetValue": True}
                        ]
                    }
                ],
                "automations": [
                    {
                        "name": "Sunset Cooking",
                        "uniqueIdentifier": "at-50",
                        "triggerType": "time",
                        "actionSetIdentifiers": ["sc-10"]
                    }
                ]
            }
            json_file = tmp_path / "controller_backup.json"
            json_file.write_text(json.dumps(data), encoding="utf-8")

            household = parse_app_export(json_file)
            self.assertEqual(household.name, "Lake Cabin")
            self.assertEqual(len(household.rooms), 2)
            self.assertEqual(len(household.accessories), 1)
            self.assertEqual(len(household.scenes), 1)
            self.assertEqual(len(household.automations), 1)

            acc = household.accessories["a-101"]
            self.assertEqual(acc.room_name, "Kitchen")
            self.assertEqual(acc.manufacturer, "Umbrella")
            self.assertTrue(acc.services[0].characteristics[0].value)
            self.assertEqual(household.unresolved_action_refs(), 0)

    def test_controller_json_unknown_accessory_is_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            data = {
                "version": 1,
                "homes": [{"name": "Lake Cabin", "uniqueIdentifier": "h-99"}],
                "rooms": [],
                "accessories": [{"name": "Lamp", "uniqueIdentifier": "a-101"}],
                "scenes": [{
                    "name": "Ghost Scene",
                    "uniqueIdentifier": "sc-11",
                    "actions": [{"accessoryIdentifier": "a-999", "characteristicType": "PowerState", "targetValue": True}],
                }],
            }
            json_file = Path(td) / "controller_backup.json"
            json_file.write_text(json.dumps(data), encoding="utf-8")

            household = parse_app_export(json_file)
            self.assertEqual(household.unresolved_action_refs(), 1)


if __name__ == "__main__":
    unittest.main()
