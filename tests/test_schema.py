"""Test domain models and serialization."""

import unittest
from src.models.schema import (
    Accessory,
    Action,
    Automation,
    Characteristic,
    Floor,
    Household,
    Room,
    Scene,
    Service,
    ServiceGroup,
    Zone,
)


class TestSchema(unittest.TestCase):
    def test_household_serialization_roundtrip(self):
        household = Household(id="h-01", name="Main Villa", is_primary=True)

        # Add Room
        household.rooms["r-01"] = Room(id="r-01", name="Living Room")
        household.rooms["r-02"] = Room(id="r-02", name="Kitchen")

        # Add Accessories
        char = Characteristic(id="c-01", type="PowerState", value=True)
        svc = Service(id="s-01", name="Ceiling Lamp", service_type="Lightbulb", characteristics=[char])
        acc1 = Accessory(
            id="a-01",
            name="Living Room Lamp",
            room_id="r-01",
            room_name="Living Room",
            category="Lighting",
            manufacturer="Acme",
            model="Hue A19",
            battery_level=None,
            reachable=True,
            protocol="Zigbee",
            services=[svc],
        )
        acc2 = Accessory(
            id="a-02",
            name="Orphan Sensor",
            room_id=None,
            room_name="Unassigned",
            category="Sensor",
            battery_level=15,
            reachable=False,
        )
        household.accessories["a-01"] = acc1
        household.accessories["a-02"] = acc2
        household.rooms["r-01"].accessory_ids.append("a-01")

        # Add Scene
        action = Action(target_accessory_id="a-01", target_characteristic_type="PowerState", target_value=False)
        household.scenes["sc-01"] = Scene(id="sc-01", name="Goodnight", actions=[action])

        # Add Automation
        auto = Automation(
            id="at-01",
            name="Night Lockdown",
            trigger_type="time",
            triggers=[{"time": "22:40"}],
            action_set_ids=["sc-01"],
            is_shortcut=True,
            shortcut_details={"actions": ["CheckIfAnyoneHome", "RunScene"]},
        )
        household.automations["at-01"] = auto

        # Test query helpers
        self.assertEqual(len(household.get_accessories_by_room("r-01")), 1)
        self.assertEqual(household.get_accessories_by_room("r-01")[0].name, "Living Room Lamp")
        orphans = household.get_orphan_accessories()
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].name, "Orphan Sensor")

        # Serialize & deserialize
        serialized = household.to_dict()
        restored = Household.from_dict(serialized)

        self.assertEqual(restored.id, "h-01")
        self.assertEqual(restored.name, "Main Villa")
        self.assertEqual(len(restored.rooms), 2)
        self.assertEqual(len(restored.accessories), 2)
        self.assertTrue(restored.accessories["a-01"].services[0].characteristics[0].value)
        self.assertTrue(restored.automations["at-01"].is_shortcut)

    def test_unresolved_action_refs(self):
        household = Household(id="h-01", name="Main Villa")
        household.accessories["a-01"] = Accessory(id="a-01", name="Lamp")
        household.scenes["sc-01"] = Scene(id="sc-01", name="Night", actions=[
            Action(target_accessory_id="a-01"),                          # resolves
            Action(target_accessory_id="ghost"),                         # unresolved
            Action(target_accessory_id="ghost", accessory_name="Fan"),   # named by the export
        ])
        household.automations["at-01"] = Automation(id="at-01", name="Dusk", actions=[
            Action(target_accessory_id="ghost-2"),                       # unresolved
        ])
        self.assertEqual(household.unresolved_action_refs(), 2)


    def test_profile_fields_survive_the_round_trip(self):
        household = Household(id="h-02", name="ZX9")
        acc = Accessory(id="a-1", name="Sensor", display_name="Capteur", unnamed=True, radio="Thread",
                        category_name="Sensor", matter_vendor_id=65522, matter_product_id=32769, sleep_interval_ms=2000,
                        has_battery=True, senses=["LightLevel", "Occupancy"], pending_update={"state": 1, "version": "2"})
        acc.services.append(Service(id="s-1", name="Lux", service_type="x", type_name="LightSensor",
                                    characteristics=[Characteristic(id="c-1", type="y", type_name="CurrentAmbientLightLevel",
                                                                    min=0.0001, max=100000.0, step=None)]))
        household.accessories["a-1"] = acc
        household.service_groups["g-1"] = ServiceGroup(id="g-1", name="Galerie", service_ids=["s-1"], accessory_ids=["a-1"])
        household.automations["t-1"] = Automation(id="t-1", name="Rule", auto_off_s=120.0, hidden_in_app=True,
                                                  conditions=[{"text": "now() <= sunset"}])
        again = Household.from_dict(household.to_dict())
        self.assertEqual(again.to_dict(), household.to_dict())
        self.assertEqual(again.accessories["a-1"].label, "Capteur")


if __name__ == "__main__":
    unittest.main()
