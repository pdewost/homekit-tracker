"""Parser for HomeKit exports from third-party apps (Controller, HomeCare, HomekitControl, Catalyst)."""

from __future__ import annotations
import csv
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ..models.schema import (
    Accessory,
    Action,
    Automation,
    Characteristic,
    Household,
    Room,
    Scene,
    Service,
    Zone,
)


def detect_export_type(file_path: Union[str, Path]) -> str:
    """Detect the format of an exported file: 'controller_json', 'catalyst_json', 'csv_inventory', 'generic_json'."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {path}")

    if path.suffix.lower() == ".csv":
        return "csv_inventory"

    if path.suffix.lower() in (".json", ".txt"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse JSON file {path}: {e}")

        if isinstance(data, dict):
            # Controller for HomeKit structure
            if "version" in data and ("homes" in data or "rooms" in data or "accessories" in data):
                return "controller_json"
            if "accessories" in data and "rooms" in data:
                return "catalyst_json"
            if "home" in data or "name" in data:
                return "generic_json"
        elif isinstance(data, list):
            return "list_json"

    return "unknown"


def parse_app_export(
    file_path: Union[str, Path],
    household_name: Optional[str] = None
) -> Household:
    """Auto-detect format and parse any third-party app export into a normalized Household."""
    path = Path(file_path)
    fmt = detect_export_type(path)

    if fmt == "controller_json":
        return _parse_controller_json(path, household_name)
    elif fmt == "catalyst_json":
        return _parse_catalyst_json(path, household_name)
    elif fmt == "csv_inventory":
        return _parse_csv_inventory(path, household_name)
    elif fmt in ("generic_json", "list_json"):
        return _parse_generic_json(path, household_name)
    else:
        raise ValueError(f"Unsupported or unrecognized export format in {path}")


def _select_home(homes: List[Dict[str, Any]], target_home: Optional[str]) -> Dict[str, Any]:
    """Same policy as the ZMKF reader: an unknown name never falls back; several homes need --home."""
    names = [h.get("name") for h in homes]
    if target_home:
        matches = [h for h in homes if str(h.get("name") or "").lower() == target_home.lower()]
        if not matches:
            raise ValueError(f"No home named {target_home!r}; homes in the export: {names}")
        if len(matches) > 1:
            raise ValueError(f"The home name {target_home!r} is ambiguous in the export: several homes match")
        return matches[0]
    if len(homes) > 1:
        raise ValueError(f"The export has {len(homes)} homes; pass --home with one of: {names}")
    return homes[0]


def _parse_catalyst_json(path: Path, target_home: Optional[str] = None) -> Household:
    """Parse output from HomeKit Catalyst apps (e.g. homekit-extractor)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Sometimes the export contains multiple homes
    home_data = data
    if "homes" in data and isinstance(data["homes"], list):
        home_data = _select_home(data["homes"], target_home)
    elif target_home and str(data.get("name") or "").lower() != target_home.lower():
        raise ValueError(f"No home named {target_home!r}; the export holds one home: {data.get('name')!r}")

    home_name = home_data.get("name", target_home or "Primary Residence")
    home_id = str(home_data.get("id") or home_data.get("uniqueIdentifier") or uuid.uuid4())

    household = Household(
        id=home_id,
        name=home_name,
        is_primary=home_data.get("isPrimary", True),
    )

    # 1. Parse Rooms
    room_map: Dict[str, str] = {}  # id -> name
    for r in home_data.get("rooms", []):
        rid = str(r.get("uniqueIdentifier") or r.get("id") or uuid.uuid4())
        rname = r.get("name", "Unknown Room")
        room_map[rid] = rname
        household.rooms[rid] = Room(id=rid, name=rname)

    # 2. Parse Zones
    for z in home_data.get("zones", []):
        zid = str(z.get("uniqueIdentifier") or z.get("id") or uuid.uuid4())
        zname = z.get("name", "Unknown Zone")
        zrooms = [str(r.get("uniqueIdentifier") or r.get("id") or r) for r in z.get("rooms", [])]
        household.zones[zid] = Zone(id=zid, name=zname, room_ids=zrooms)
        for r_id in zrooms:
            if r_id in household.rooms:
                household.rooms[r_id].zone_ids.append(zid)

    # 3. Parse Accessories
    for a in home_data.get("accessories", []):
        aid = str(a.get("uniqueIdentifier") or a.get("id") or uuid.uuid4())
        aname = a.get("name", "Unknown Accessory")
        rid = str(a.get("roomIdentifier") or a.get("room_id") or a.get("roomId") or "")
        rname = a.get("roomName") or room_map.get(rid, "Unassigned")

        # Services & Characteristics
        services: List[Service] = []
        for s in a.get("services", []):
            sid = str(s.get("uniqueIdentifier") or s.get("id") or uuid.uuid4())
            sname = s.get("name", aname)
            stype = s.get("serviceType", s.get("type", "UnknownService"))

            chars: List[Characteristic] = []
            for c in s.get("characteristics", []):
                cid = str(c.get("uniqueIdentifier") or c.get("id") or uuid.uuid4())
                ctype = c.get("characteristicType", c.get("type", "UnknownChar"))
                chars.append(Characteristic(
                    id=cid,
                    type=ctype,
                    description=c.get("description", ctype),
                    value=c.get("value"),
                    format=c.get("format", ""),
                    unit=c.get("unit"),
                ))

            services.append(Service(
                id=sid,
                name=sname,
                service_type=stype,
                characteristics=chars,
            ))

        acc = Accessory(
            id=aid,
            name=aname,
            room_id=rid or None,
            room_name=rname,
            category=a.get("category", "Other"),
            manufacturer=a.get("manufacturer", "Unknown"),
            model=a.get("model", "Unknown"),
            firmware_version=a.get("firmwareVersion", a.get("firmware", "")),
            serial_number=a.get("serialNumber", ""),
            is_bridge=a.get("isBridge", False),
            bridge_id=a.get("bridgeIdentifier"),
            reachable=a.get("isReachable", a.get("reachable", True)),
            services=services,
        )
        household.accessories[aid] = acc
        if rid and rid in household.rooms:
            household.rooms[rid].accessory_ids.append(aid)

    # 4. Parse Scenes
    for s in home_data.get("scenes", home_data.get("actionSets", [])):
        sid = str(s.get("uniqueIdentifier") or s.get("id") or uuid.uuid4())
        sname = s.get("name", "Custom Scene")
        actions: List[Action] = []
        for act in s.get("actions", []):
            actions.append(Action(
                target_accessory_id=str(act.get("accessoryIdentifier") or act.get("accessory_id", "")),
                target_service_id=str(act.get("serviceIdentifier") or ""),
                target_characteristic_type=act.get("characteristicType", ""),
                target_value=act.get("targetValue", act.get("value")),
                accessory_name=act.get("accessoryName", ""),
                service_name=act.get("serviceName", ""),
            ))
        household.scenes[sid] = Scene(id=sid, name=sname, actions=actions)

    # 5. Parse Automations
    for auto in home_data.get("automations", home_data.get("triggers", [])):
        autoid = str(auto.get("uniqueIdentifier") or auto.get("id") or uuid.uuid4())
        autoname = auto.get("name", "Unnamed Automation")
        is_shortcut = auto.get("isShortcut", False) or "workflow" in auto

        actions: List[Action] = []
        for act in auto.get("actions", []):
            actions.append(Action(
                target_accessory_id=str(act.get("accessoryIdentifier") or ""),
                target_service_id=str(act.get("serviceIdentifier") or ""),
                target_characteristic_type=act.get("characteristicType", ""),
                target_value=act.get("targetValue"),
                accessory_name=act.get("accessoryName", ""),
            ))

        household.automations[autoid] = Automation(
            id=autoid,
            name=autoname,
            is_enabled=auto.get("isEnabled", True),
            trigger_type=auto.get("triggerType", "event"),
            triggers=auto.get("triggers", []),
            conditions=auto.get("conditions", []),
            actions=actions,
            is_shortcut=is_shortcut,
            shortcut_details=auto.get("workflow"),
        )

    return household


def _parse_controller_json(path: Path, target_home: Optional[str] = None) -> Household:
    """Parse backup JSON created by Controller for HomeKit."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Controller for HomeKit wraps data in a top-level payload
    home_obj = None
    homes = data.get("homes", [])
    if homes:
        home_obj = _select_home(homes, target_home)
        if len(homes) > 1:  # the entity arrays carry no home reference: they cannot be assigned to one home
            raise ValueError("The export holds several homes and cannot be split by home; "
                             "export each home on its own")
    elif target_home:
        if str(data.get("name") or "").lower() != target_home.lower():
            raise ValueError(f"No home named {target_home!r}; the export carries no matching home name")
        home_obj = data

    home_name = home_obj.get("name", "Home") if home_obj else "Home"
    home_id = str(home_obj.get("uniqueIdentifier", uuid.uuid4())) if home_obj else str(uuid.uuid4())

    household = Household(id=home_id, name=home_name, is_primary=True)

    # Rooms
    rooms = data.get("rooms", [])
    for r in rooms:
        rid = str(r.get("uniqueIdentifier") or r.get("id") or uuid.uuid4())
        rname = r.get("name", "Room")
        household.rooms[rid] = Room(id=rid, name=rname)

    # Zones
    zones = data.get("zones", [])
    for z in zones:
        zid = str(z.get("uniqueIdentifier") or uuid.uuid4())
        zname = z.get("name", "Zone")
        room_ids = [str(item) for item in z.get("roomIdentifiers", [])]
        household.zones[zid] = Zone(id=zid, name=zname, room_ids=room_ids)
        for r_id in room_ids:
            if r_id in household.rooms:
                household.rooms[r_id].zone_ids.append(zid)

    # Accessories
    accessories = data.get("accessories", [])
    for a in accessories:
        aid = str(a.get("uniqueIdentifier") or uuid.uuid4())
        aname = a.get("name", "Accessory")
        rid = str(a.get("roomIdentifier") or "")
        rname = household.rooms[rid].name if rid in household.rooms else "Unassigned"

        services: List[Service] = []
        for s in a.get("services", []):
            sid = str(s.get("uniqueIdentifier") or uuid.uuid4())
            sname = s.get("name", aname)
            stype = s.get("serviceType", "Service")

            chars: List[Characteristic] = []
            for c in s.get("characteristics", []):
                cid = str(c.get("uniqueIdentifier") or uuid.uuid4())
                chars.append(Characteristic(
                    id=cid,
                    type=c.get("characteristicType", ""),
                    description=c.get("name", ""),
                    value=c.get("value"),
                    unit=c.get("unit"),
                ))

            services.append(Service(id=sid, name=sname, service_type=stype, characteristics=chars))

        acc = Accessory(
            id=aid,
            name=aname,
            room_id=rid or None,
            room_name=rname,
            category=a.get("category", "Other"),
            manufacturer=a.get("manufacturer", "Unknown"),
            model=a.get("model", "Unknown"),
            firmware_version=a.get("firmwareVersion", ""),
            services=services,
        )
        household.accessories[aid] = acc
        if rid and rid in household.rooms:
            household.rooms[rid].accessory_ids.append(aid)

    # Scenes
    scenes = data.get("scenes", data.get("actionSets", []))
    for s in scenes:
        sid = str(s.get("uniqueIdentifier") or uuid.uuid4())
        sname = s.get("name", "Scene")
        actions: List[Action] = []
        for act in s.get("actions", []):
            actions.append(Action(
                target_accessory_id=str(act.get("accessoryIdentifier") or ""),
                target_service_id=str(act.get("serviceIdentifier") or ""),
                target_characteristic_type=act.get("characteristicType", ""),
                target_value=act.get("targetValue"),
                accessory_name=act.get("accessoryName", ""),
            ))
        household.scenes[sid] = Scene(id=sid, name=sname, actions=actions)

    # Automations / Triggers
    automations = data.get("automations", data.get("triggers", []))
    for auto in automations:
        autoid = str(auto.get("uniqueIdentifier") or uuid.uuid4())
        autoname = auto.get("name", "Automation")
        household.automations[autoid] = Automation(
            id=autoid,
            name=autoname,
            is_enabled=auto.get("isEnabled", True),
            trigger_type=auto.get("triggerType", "time"),
            triggers=auto.get("events", auto.get("triggers", [])),
            conditions=auto.get("conditions", []),
            action_set_ids=[str(i) for i in auto.get("actionSetIdentifiers", [])],
        )

    return household


def _parse_csv_inventory(path: Path, household_name: Optional[str] = None) -> Household:
    """Parse a CSV inventory of accessories (e.g. from HomeCare)."""
    household = Household(
        id=str(uuid.uuid4()),
        name=household_name or "Imported Home (CSV)",
        is_primary=True
    )

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Case-insensitive column lookup
            cols = {k.strip().lower(): v.strip() for k, v in row.items() if k}

            aname = cols.get("name") or cols.get("accessory") or cols.get("device") or "Unknown"
            rname = cols.get("room") or cols.get("location") or "Unassigned"
            cat = cols.get("category") or cols.get("type") or "Other"
            manuf = cols.get("manufacturer") or cols.get("brand") or "Unknown"
            model = cols.get("model") or "Unknown"
            firmware = cols.get("firmware") or cols.get("firmware version") or ""
            protocol = cols.get("protocol") or cols.get("connectivity") or "Unknown"

            # Battery parsing
            battery = None
            if "battery" in cols and cols["battery"]:
                raw_batt = cols["battery"].replace("%", "").strip()
                try:
                    battery = int(float(raw_batt))
                except ValueError:
                    pass

            # Room tracking
            rid = None
            if rname and rname.lower() != "unassigned":
                # Find or create room
                matching_rooms = [r for r in household.rooms.values() if r.name.lower() == rname.lower()]
                if matching_rooms:
                    rid = matching_rooms[0].id
                else:
                    rid = str(uuid.uuid4())
                    household.rooms[rid] = Room(id=rid, name=rname)

            aid = str(uuid.uuid4())
            acc = Accessory(
                id=aid,
                name=aname,
                room_id=rid,
                room_name=rname,
                category=cat,
                manufacturer=manuf,
                model=model,
                firmware_version=firmware,
                battery_level=battery,
                protocol=protocol,
            )
            household.accessories[aid] = acc
            if rid and rid in household.rooms:
                household.rooms[rid].accessory_ids.append(aid)

    return household


def _parse_generic_json(path: Path, household_name: Optional[str] = None) -> Household:
    """Parse generic JSON matching schema or standard hierarchy."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "accessories" in data and "rooms" in data:
        # Matches our Household schema
        return Household.from_dict(data)

    raise ValueError(f"Unrecognized generic JSON layout in {path}")
