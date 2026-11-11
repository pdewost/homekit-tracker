"""Unified domain schema for Multi-Household Home Automation."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple
import json


@dataclass
class Characteristic:
    """A single HomeKit characteristic (e.g. PowerState, CurrentTemperature)."""
    id: str
    type: str
    description: str = ""
    value: Any = None
    format: str = ""  # bool, int, float, string
    unit: Optional[str] = None  # celsius, percentage, arcdegrees, etc.
    can_read: bool = True
    can_write: bool = True
    is_event_enabled: bool = False
    type_name: str = ""  # HAP name of `type`, or vendor:<uuid>
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Service:
    """A HomeKit service containing multiple characteristics (e.g. Lightbulb)."""
    id: str
    name: str
    service_type: str
    is_primary: bool = False
    characteristics: List[Characteristic] = field(default_factory=list)
    type_name: str = ""  # HAP name of `service_type`, or vendor:<uuid>

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "service_type": self.service_type,
            "type_name": self.type_name,
            "is_primary": self.is_primary,
            "characteristics": [c.to_dict() for c in self.characteristics],
        }


@dataclass
class Accessory:
    """A physical or bridged device in the household."""
    id: str
    name: str
    room_id: Optional[str] = None
    room_name: Optional[str] = None
    category: str = "Other"  # Lighting, Switch, Outlet, Thermostat, Sensor, Lock, Camera, Bridge
    manufacturer: str = "Unknown"
    model: str = "Unknown"
    firmware_version: str = ""
    serial_number: str = ""
    is_bridge: bool = False
    bridge_id: Optional[str] = None
    reachable: bool = True
    battery_level: Optional[int] = None  # 0-100 if battery-powered
    protocol: str = "Unknown"  # Thread, Matter, Zigbee, Wi-Fi, BLE
    services: List[Service] = field(default_factory=list)
    coordinates_on_plan: Optional[Tuple[float, float]] = None  # (x, y) normalized 0.0-1.0 or pixel
    # Device profile
    display_name: str = ""  # the name to show; empty = use `name`
    unnamed: bool = False
    radio: str = "Unknown"  # Thread, Wi-Fi, unknown:<n>
    category_name: str = ""
    matter_vendor_id: Optional[int] = None
    matter_product_id: Optional[int] = None
    sleep_interval_ms: Optional[float] = None
    has_battery: bool = False
    senses: List[str] = field(default_factory=list)
    pending_update: Optional[Dict[str, Any]] = None

    @property
    def label(self) -> str:
        return self.display_name or self.name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "room_id": self.room_id,
            "room_name": self.room_name,
            "category": self.category,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "serial_number": self.serial_number,
            "is_bridge": self.is_bridge,
            "bridge_id": self.bridge_id,
            "reachable": self.reachable,
            "battery_level": self.battery_level,
            "protocol": self.protocol,
            "coordinates_on_plan": self.coordinates_on_plan,
            "services": [s.to_dict() for s in self.services],
            "display_name": self.display_name,
            "unnamed": self.unnamed,
            "radio": self.radio,
            "category_name": self.category_name,
            "matter_vendor_id": self.matter_vendor_id,
            "matter_product_id": self.matter_product_id,
            "sleep_interval_ms": self.sleep_interval_ms,
            "has_battery": self.has_battery,
            "senses": self.senses,
            "pending_update": self.pending_update,
        }


@dataclass
class Room:
    """A physical room in the household."""
    id: str
    name: str
    floor_id: Optional[str] = None
    zone_ids: List[str] = field(default_factory=list)
    area_m2: Optional[float] = None
    polygon_coords: Optional[List[Tuple[float, float]]] = None  # Spatial bounding on floor plan
    accessory_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Floor:
    """A level/floor of the household."""
    id: str
    name: str
    level: int = 0  # 0: Ground, 1: 1st floor, -1: Basement
    plan_file: Optional[str] = None  # path to PDF or image plan
    room_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Zone:
    """A logical grouping of rooms (e.g. Upstairs, Exterior, Day Zone)."""
    id: str
    name: str
    room_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ServiceGroup:
    """A HomeKit accessory group (e.g. two bulbs shown as one tile)."""
    id: str
    name: str
    service_ids: List[str] = field(default_factory=list)
    accessory_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Action:
    """A single state change action (e.g. Set Light Power to True)."""
    target_accessory_id: str
    target_service_id: Optional[str] = None
    target_characteristic_type: str = ""
    target_value: Any = None
    accessory_name: str = ""
    service_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Scene:
    """A HomeKit scene / ActionSet (e.g. 'Good Night', 'Arrive Home')."""
    id: str
    name: str
    scene_type: str = "user_defined"  # user_defined, trigger_owned
    actions: List[Action] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "scene_type": self.scene_type,
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class Automation:
    """An automated trigger rule or converted shortcut."""
    id: str
    name: str
    is_enabled: bool = True
    trigger_type: str = "unknown"  # time, presence, sensor, characteristic, accessory_event
    triggers: List[Dict[str, Any]] = field(default_factory=list)
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    action_set_ids: List[str] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    is_shortcut: bool = False
    shortcut_details: Optional[Dict[str, Any]] = None  # Decoded workflow AST if converted to shortcut
    auto_off_s: Optional[float] = None  # from a duration end event ("Désactiver après")
    hidden_in_app: bool = False  # an event trigger with no event: the Home app does not show it and it cannot fire

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "is_enabled": self.is_enabled,
            "trigger_type": self.trigger_type,
            "triggers": self.triggers,
            "conditions": self.conditions,
            "action_set_ids": self.action_set_ids,
            "actions": [a.to_dict() for a in self.actions],
            "is_shortcut": self.is_shortcut,
            "shortcut_details": self.shortcut_details,
            "auto_off_s": self.auto_off_s,
            "hidden_in_app": self.hidden_in_app,
        }


@dataclass
class Household:
    """Root entity representing a single home/property."""
    id: str
    name: str
    is_primary: bool = False
    timezone: str = "UTC"
    hubs: List[str] = field(default_factory=list)  # Hub names or device IDs
    floors: Dict[str, Floor] = field(default_factory=dict)
    zones: Dict[str, Zone] = field(default_factory=dict)
    rooms: Dict[str, Room] = field(default_factory=dict)
    accessories: Dict[str, Accessory] = field(default_factory=dict)
    scenes: Dict[str, Scene] = field(default_factory=dict)
    automations: Dict[str, Automation] = field(default_factory=dict)
    service_groups: Dict[str, ServiceGroup] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    unavailable: List[str] = field(default_factory=list)  # collections the source could not describe (missing optional table)

    def get_accessories_by_room(self, room_id: str) -> List[Accessory]:
        """Return all accessories assigned to a given room."""
        return [acc for acc in self.accessories.values() if acc.room_id == room_id]

    def get_orphan_accessories(self) -> List[Accessory]:
        """Return accessories not assigned to any room or assigned to a non-existent room."""
        return [acc for acc in self.accessories.values() if not acc.room_id or acc.room_id not in self.rooms]

    def get_accessories_by_category(self, category: str) -> List[Accessory]:
        """Filter accessories by category."""
        return [acc for acc in self.accessories.values() if acc.category.lower() == category.lower()]

    def unresolved_action_refs(self) -> int:
        """Count scene and automation actions whose accessory is neither in this household nor named."""
        actions = [a for s in self.scenes.values() for a in s.actions]
        actions += [a for auto in self.automations.values() for a in auto.actions]
        return sum(1 for a in actions if a.target_accessory_id not in self.accessories and not a.accessory_name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "is_primary": self.is_primary,
            "timezone": self.timezone,
            "hubs": self.hubs,
            "floors": {k: v.to_dict() for k, v in self.floors.items()},
            "zones": {k: v.to_dict() for k, v in self.zones.items()},
            "rooms": {k: v.to_dict() for k, v in self.rooms.items()},
            "accessories": {k: v.to_dict() for k, v in self.accessories.items()},
            "scenes": {k: v.to_dict() for k, v in self.scenes.items()},
            "automations": {k: v.to_dict() for k, v in self.automations.items()},
            "service_groups": {k: v.to_dict() for k, v in self.service_groups.items()},
            "metadata": self.metadata,
            "unavailable": sorted(self.unavailable),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Household:
        """Reconstruct a Household instance from serialized dictionary."""
        household = cls(
            id=data.get("id", "household-01"),
            name=data.get("name", "My Home"),
            is_primary=data.get("is_primary", False),
            timezone=data.get("timezone", "UTC"),
            hubs=data.get("hubs", []),
            metadata=data.get("metadata", {}),
            unavailable=list(data.get("unavailable", [])),
        )

        # Restore floors
        for fid, fval in data.get("floors", {}).items():
            household.floors[fid] = Floor(
                id=fval.get("id", fid),
                name=fval.get("name", fid),
                level=fval.get("level", 0),
                plan_file=fval.get("plan_file"),
                room_ids=fval.get("room_ids", []),
            )

        # Restore zones
        for zid, zval in data.get("zones", {}).items():
            household.zones[zid] = Zone(
                id=zval.get("id", zid),
                name=zval.get("name", zid),
                room_ids=zval.get("room_ids", []),
            )

        # Restore rooms
        for rid, rval in data.get("rooms", {}).items():
            household.rooms[rid] = Room(
                id=rval.get("id", rid),
                name=rval.get("name", rid),
                floor_id=rval.get("floor_id"),
                zone_ids=rval.get("zone_ids", []),
                area_m2=rval.get("area_m2"),
                polygon_coords=rval.get("polygon_coords"),
                accessory_ids=rval.get("accessory_ids", []),
            )

        # Restore accessories
        for aid, aval in data.get("accessories", {}).items():
            services = []
            for sval in aval.get("services", []):
                chars = []
                for cval in sval.get("characteristics", []):
                    chars.append(Characteristic(
                        id=cval.get("id", ""),
                        type=cval.get("type", ""),
                        description=cval.get("description", ""),
                        value=cval.get("value"),
                        format=cval.get("format", ""),
                        unit=cval.get("unit"),
                        can_read=cval.get("can_read", True),
                        can_write=cval.get("can_write", True),
                        is_event_enabled=cval.get("is_event_enabled", False),
                        type_name=cval.get("type_name", ""),
                        min=cval.get("min"),
                        max=cval.get("max"),
                        step=cval.get("step"),
                    ))
                services.append(Service(
                    id=sval.get("id", ""),
                    name=sval.get("name", ""),
                    service_type=sval.get("service_type", ""),
                    is_primary=sval.get("is_primary", False),
                    characteristics=chars,
                    type_name=sval.get("type_name", ""),
                ))

            household.accessories[aid] = Accessory(
                id=aval.get("id", aid),
                name=aval.get("name", "Unknown Accessory"),
                room_id=aval.get("room_id"),
                room_name=aval.get("room_name"),
                category=aval.get("category", "Other"),
                manufacturer=aval.get("manufacturer", "Unknown"),
                model=aval.get("model", "Unknown"),
                firmware_version=aval.get("firmware_version", ""),
                serial_number=aval.get("serial_number", ""),
                is_bridge=aval.get("is_bridge", False),
                bridge_id=aval.get("bridge_id"),
                reachable=aval.get("reachable", True),
                battery_level=aval.get("battery_level"),
                protocol=aval.get("protocol", "Unknown"),
                coordinates_on_plan=aval.get("coordinates_on_plan"),
                services=services,
                display_name=aval.get("display_name", ""),
                unnamed=aval.get("unnamed", False),
                radio=aval.get("radio", "Unknown"),
                category_name=aval.get("category_name", ""),
                matter_vendor_id=aval.get("matter_vendor_id"),
                matter_product_id=aval.get("matter_product_id"),
                sleep_interval_ms=aval.get("sleep_interval_ms"),
                has_battery=aval.get("has_battery", False),
                senses=aval.get("senses", []),
                pending_update=aval.get("pending_update"),
            )

        # Restore scenes
        for sid, sval in data.get("scenes", {}).items():
            actions = [
                Action(
                    target_accessory_id=act.get("target_accessory_id", ""),
                    target_service_id=act.get("target_service_id"),
                    target_characteristic_type=act.get("target_characteristic_type", ""),
                    target_value=act.get("target_value"),
                    accessory_name=act.get("accessory_name", ""),
                    service_name=act.get("service_name", ""),
                )
                for act in sval.get("actions", [])
            ]
            household.scenes[sid] = Scene(
                id=sval.get("id", sid),
                name=sval.get("name", sid),
                scene_type=sval.get("scene_type", "user_defined"),
                actions=actions,
            )

        # Restore automations
        for atid, atval in data.get("automations", {}).items():
            actions = [
                Action(
                    target_accessory_id=act.get("target_accessory_id", ""),
                    target_service_id=act.get("target_service_id"),
                    target_characteristic_type=act.get("target_characteristic_type", ""),
                    target_value=act.get("target_value"),
                    accessory_name=act.get("accessory_name", ""),
                    service_name=act.get("service_name", ""),
                )
                for act in atval.get("actions", [])
            ]
            household.automations[atid] = Automation(
                id=atval.get("id", atid),
                name=atval.get("name", atid),
                is_enabled=atval.get("is_enabled", True),
                trigger_type=atval.get("trigger_type", "unknown"),
                triggers=atval.get("triggers", []),
                conditions=atval.get("conditions", []),
                action_set_ids=atval.get("action_set_ids", []),
                actions=actions,
                is_shortcut=atval.get("is_shortcut", False),
                shortcut_details=atval.get("shortcut_details"),
                auto_off_s=atval.get("auto_off_s"),
                hidden_in_app=atval.get("hidden_in_app", False),
            )

        # Restore service groups
        for gid, gval in data.get("service_groups", {}).items():
            household.service_groups[gid] = ServiceGroup(
                id=gval.get("id", gid),
                name=gval.get("name", gid),
                service_ids=gval.get("service_ids", []),
                accessory_ids=gval.get("accessory_ids", []),
            )

        return household
