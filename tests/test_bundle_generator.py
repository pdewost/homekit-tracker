"""Test generation of LLM audit bundles."""

import tempfile
import unittest
from pathlib import Path
import json

from src.audit.bundle_generator import BUNDLE_FILES, STRIPPED_KEYS, generate_llm_audit_bundle
from src.models.schema import Accessory, Automation, Floor, Household, Room, Scene

CONTENTS_SENTENCE = ("Rooms, accessories, scenes, automations; service and characteristic states only where the source "
                     "provides them.")
SHORTCUTS_SENTENCE = ("Shortcuts logic is not in this bundle (by design), so dimension 3 covers only the automations' "
                      "names and types.")


def _keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


class TestBundleGenerator(unittest.TestCase):
    def test_generate_llm_audit_bundle(self):
        household = Household(id="h-test", name="Villa Test")
        household.rooms["r-1"] = Room(id="r-1", name="Lounge")
        household.accessories["a-1"] = Accessory(
            id="a-1",
            name="Lustre Lounge",
            room_id="r-1",
            room_name="Lounge",
            category="Lighting",
            reachable=True,
        )
        household.accessories["a-2"] = Accessory(
            id="a-2",
            name="Smoke Detector",
            room_id="r-1",
            room_name="Lounge",
            category="Sensor",
            battery_level=10,  # low battery flag
            reachable=True,
        )
        household.accessories["a-3"] = Accessory(
            id="a-3",
            name="Lost Sensor",
            room_id=None,
            room_name="Unassigned",
            category="Sensor",
            reachable=False,  # offline flag + orphan flag
        )

        household.scenes["s-1"] = Scene(id="s-1", name="Cinema Mode")
        household.automations["at-1"] = Automation(
            id="at-1",
            name="Motion Movie",
            is_shortcut=True,
            shortcut_details={"step": "if dark then cinema"},
        )

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "audit_output"
            bundle_path = generate_llm_audit_bundle(household, out_dir)

            self.assertTrue((bundle_path / "00_README.md").exists())
            self.assertTrue((bundle_path / "01_devices_and_rooms.md").exists())
            self.assertTrue((bundle_path / "02_scenes_matrix.md").exists())
            self.assertTrue((bundle_path / "03_automations_analysis.md").exists())
            self.assertTrue((bundle_path / "04_hygiene_and_conflicts.md").exists())
            self.assertTrue((bundle_path / "05_llm_audit_prompt.md").exists())
            self.assertTrue((bundle_path / "household_normalized.json").exists())

            # Verify hygiene flags detected the low battery, offline device, and orphan device
            hygiene_text = (bundle_path / "04_hygiene_and_conflicts.md").read_text(encoding="utf-8")
            self.assertIn("Lost Sensor", hygiene_text)
            self.assertIn("Smoke Detector", hygiene_text)

            # Verify LLM prompt contains instructions
            prompt_text = (bundle_path / "05_llm_audit_prompt.md").read_text(encoding="utf-8")
            self.assertIn("Villa Test", prompt_text)
            self.assertIn("NATIVE VS SHORTCUT", prompt_text)

    def _planted_household(self):
        household = Household(id="h-test", name="Villa Test", metadata={"wifi_ssid": "planted-ssid"})
        household.floors["f-1"] = Floor(id="f-1", name="Ground", plan_file="data/floor_plans/ground.pdf")
        household.rooms["r-1"] = Room(id="r-1", name="Lounge", area_m2=31.5, polygon_coords=[(0.1, 0.2), (0.3, 0.4)])
        household.accessories["a-1"] = Accessory(id="a-1", name="Lamp", room_id="r-1", coordinates_on_plan=(0.5, 0.5))
        household.automations["at-1"] = Automation(
            id="at-1", name="Motion Movie", is_shortcut=True, shortcut_details={"step": "if dark then cinema"},
        )
        return household

    def test_cloud_bound_json_carries_no_stripped_key(self):
        """HA-1, HA-6, HC-2: the bare keys never appear in household_normalized.json."""
        with tempfile.TemporaryDirectory() as td:
            bundle = generate_llm_audit_bundle(self._planted_household(), Path(td))
            data = json.loads((bundle / "household_normalized.json").read_text(encoding="utf-8"))
            self.assertEqual(_keys(data) & STRIPPED_KEYS, set())
            self.assertEqual(data["rooms"]["r-1"]["name"], "Lounge")

    def test_serial_numbers_and_bridge_ids_are_stripped(self):
        household = Household(id="h-t", name="Villa Test")
        household.accessories["a-1"] = Accessory(id="a-1", name="Lamp", serial_number="planted-serial",
                                                 is_bridge=True, bridge_id="planted-bridge")
        self.assertIn("planted-serial", json.dumps(household.to_dict()))
        with tempfile.TemporaryDirectory() as td:
            bundle = generate_llm_audit_bundle(household, Path(td) / "b")
            for name in BUNDLE_FILES:
                text = (bundle / name).read_text(encoding="utf-8")
                self.assertNotIn("planted-serial", text, name)
                self.assertNotIn("planted-bridge", text, name)
        self.assertLessEqual({"serial_number", "bridge_id"}, STRIPPED_KEYS)

    def test_shortcuts_payload_is_in_no_file(self):
        """HB-1: the planted value appears nowhere, and 03 has no Shortcut Logic Details section."""
        with tempfile.TemporaryDirectory() as td:
            bundle = generate_llm_audit_bundle(self._planted_household(), Path(td))
            for name in BUNDLE_FILES:
                self.assertNotIn("if dark then cinema", (bundle / name).read_text(encoding="utf-8"), name)
            self.assertNotIn("Shortcut Logic Details", (bundle / "03_automations_analysis.md").read_text(encoding="utf-8"))

    def test_stale_json_is_deleted_when_not_rewritten(self):
        """HC-3: the delete step, not a rewrite, removes a stale household_normalized.json."""
        with tempfile.TemporaryDirectory() as td:
            stale = Path(td) / "household_normalized.json"
            stale.write_text('{"shortcut_details": "stale pre-strip copy"}', encoding="utf-8")
            generate_llm_audit_bundle(self._planted_household(), Path(td), include_raw_json=False)
            self.assertFalse(stale.exists())

    def test_prompt_states_its_contents_honestly(self):
        """HA-13, HB-7, HB-8, HB-10, HC-5."""
        with tempfile.TemporaryDirectory() as td:
            bundle = generate_llm_audit_bundle(self._planted_household(), Path(td))
            prompt = (bundle / "05_llm_audit_prompt.md").read_text(encoding="utf-8")
            self.assertIn(CONTENTS_SENTENCE, prompt)
            self.assertIn(SHORTCUTS_SENTENCE, prompt)
            for word in ("complete", "entire", "full configuration"):
                self.assertNotIn(word, prompt.lower())

    def test_json_only_mode_leaves_only_the_stripped_json(self):
        """include_markdown=False deletes stale markdown and writes only the stripped JSON."""
        with tempfile.TemporaryDirectory() as td:
            stale = Path(td) / "01_devices_and_rooms.md"
            stale.write_text("stale markdown from an older run", encoding="utf-8")
            bundle = generate_llm_audit_bundle(self._planted_household(), Path(td), include_markdown=False)
            self.assertFalse(stale.exists())
            self.assertEqual([n for n in BUNDLE_FILES if (bundle / n).exists()], ["household_normalized.json"])
            data = json.loads((bundle / "household_normalized.json").read_text(encoding="utf-8"))
            self.assertEqual(_keys(data) & STRIPPED_KEYS, set())


if __name__ == "__main__":
    unittest.main()
