"""Direct SQLite reader for Apple's HomeKit daemon database (file name in src/store_files.py)."""

from __future__ import annotations
import plistlib
import sqlite3
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
    ServiceGroup,
    Zone,
)
from .. import safe_io
from ..store_files import refuse_live_root
from . import hap_types


REQUIRED_TABLES = ("home", "room", "accessory", "action set", "trigger")  # the zone table is optional

_STABLE_NS = uuid.UUID("6f1c2d8e-4b7a-4c55-9a43-2e0d9b1f7a10")


def _uuid_from_bytes(b: Any) -> str:
    """Convert raw bytes or string into a formatted UUID string."""
    if isinstance(b, bytes):
        try:
            return str(uuid.UUID(bytes=b))
        except Exception:
            return b.hex()
    elif isinstance(b, str):
        return b
    return ""


def _row_id(zuuid: Any, table: str, pk: Any) -> str:
    """The row's ZUUID as a UUID string, else an id derived from table + Z_PK (stable across runs)."""
    if zuuid:
        return _uuid_from_bytes(zuuid)
    return str(uuid.uuid5(_STABLE_NS, f"{table}:{pk}"))


def _id_forms(value: Any) -> List[Any]:
    """Every form an identifier may take: raw bytes, hex, UUID string, bytes decoded as text; a Z_PK as text."""
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        forms: List[Any] = [b, b.hex()]
        if len(b) == 16:
            forms.append(str(uuid.UUID(bytes=b)))
        try:
            forms.append(b.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        return forms
    if isinstance(value, str):
        return [value, value.lower()]
    if isinstance(value, int):
        return [str(value)]
    return []


def _raw_text(value: Any) -> str:
    """An identifier as the plist carries it: a string as is, bytes as UTF-8 text when they decode, else hex."""
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    return "" if value is None else str(value)


def _safe_load_plist(data: Any) -> Any:
    """Safely decode binary or XML property lists."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    try:
        return plistlib.loads(data)
    except Exception:
        return None


def parse_homed_sqlite(
    db_path: Union[str, Path],
    household_name: Optional[str] = None
) -> Household:
    """Read Apple's homed CoreData SQLite database and reconstruct a Household."""
    path = Path(db_path).expanduser()
    refuse_live_root(path)  # read a scratch copy, never the live store
    if not path.exists():
        raise FileNotFoundError(f"HomeKit database not found at {path}")

    conn = safe_io.sqlite_ro(path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Discover tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0].upper(): row[0] for row in cursor.fetchall()}

    if "ZMKFHOME" in tables:  # the real store's schema
        try:
            return _parse_zmkf(cursor, tables, household_name)
        finally:
            conn.close()

    # Find the entity table names
    home_table = tables.get("ZHMHOMEMODEL") or tables.get("ZHOMEMODEL")
    room_table = tables.get("ZHMROOMMODEL") or tables.get("ZROOMMODEL")
    zone_table = tables.get("ZHMZONEMODEL") or tables.get("ZZONEMODEL")
    acc_table = tables.get("ZHMACCESSORYMODEL") or tables.get("ZACCESSORYMODEL")
    actionset_table = tables.get("ZHMACTIONSETMODEL") or tables.get("ZACTIONSETMODEL")
    trigger_table = tables.get("ZHMTRIGGERMODEL") or tables.get("ZTRIGGERMODEL")

    found = dict(zip(REQUIRED_TABLES, (home_table, room_table, acc_table, actionset_table, trigger_table)))
    missing = [name for name, table in found.items() if not table]
    if missing:
        raise ValueError(f"SQLite file is missing required HomeKit table(s) {missing} (tables present: {list(tables.keys())})")

    # 1. Select Target Home
    cursor.execute(f"SELECT * FROM {home_table}")
    home_rows = cursor.fetchall()
    if not home_rows:
        raise ValueError("No homes found in database.")

    # Same policy as the ZMKF reader: an unknown name never falls back; several homes need --home
    home_names = [dict(r).get("ZNAME") or dict(r).get("name") for r in home_rows]
    if household_name:
        matches = [r for r, n in zip(home_rows, home_names) if n and str(n).lower() == household_name.lower()]
        if not matches:
            raise ValueError(f"No home named {household_name!r}; homes in the store: {home_names}")
        if len(matches) > 1:
            raise ValueError(f"The home name {household_name!r} is ambiguous: several homes match")
        selected_home = matches[0]
    elif len(home_rows) > 1:
        raise ValueError(f"The store has {len(home_rows)} homes; pass --home with one of: {home_names}")
    else:
        selected_home = home_rows[0]

    home_dict = dict(selected_home)
    home_pk = home_dict.get("Z_PK")
    h_name = home_dict.get("ZNAME") or household_name or "Home"
    h_uuid = _row_id(home_dict.get("ZUUID") or home_dict.get("ZUNIQUEIDENTIFIER"), home_table, home_pk)
    # Rows with no home belong to the selected home only when the store has a single home (multi-household isolation)
    home_filter = "WHERE ZHOME = ? OR ZHOME IS NULL" if len(home_rows) == 1 else "WHERE ZHOME = ?"

    household = Household(id=h_uuid, name=h_name, is_primary=True)

    # 2. Extract Rooms
    room_pk_to_id: Dict[int, str] = {}
    if room_table:
        cursor.execute(f"SELECT * FROM {room_table} {home_filter}", (home_pk,))
        for r in cursor.fetchall():
            rd = dict(r)
            r_pk = rd.get("Z_PK")
            r_name = rd.get("ZNAME") or "Unknown Room"
            r_uuid = _row_id(rd.get("ZUUID"), room_table, r_pk)
            room_pk_to_id[r_pk] = r_uuid
            household.rooms[r_uuid] = Room(id=r_uuid, name=r_name)

    # 3. Extract Zones
    if zone_table:
        cursor.execute(f"SELECT * FROM {zone_table} {home_filter}", (home_pk,))
        for z in cursor.fetchall():
            zd = dict(z)
            z_pk = zd.get("Z_PK")
            z_name = zd.get("ZNAME") or "Unknown Zone"
            z_uuid = _row_id(zd.get("ZUUID"), zone_table, z_pk)
            household.zones[z_uuid] = Zone(id=z_uuid, name=z_name)

    # 4. Extract Accessories; map every identifier form of each accessory row to its id (accessory rows only)
    acc_ref_to_id: Dict[Any, str] = {}
    if acc_table:
        cursor.execute(f"SELECT * FROM {acc_table} {home_filter}", (home_pk,))
        for a in cursor.fetchall():
            ad = dict(a)
            a_pk = ad.get("Z_PK")
            a_name = ad.get("ZNAME") or ad.get("ZCONFIGUREDNAME") or "Accessory"
            a_uuid = _row_id(ad.get("ZUUID"), acc_table, a_pk)
            r_pk = ad.get("ZROOM")
            r_uuid = room_pk_to_id.get(r_pk)
            r_name = household.rooms[r_uuid].name if r_uuid and r_uuid in household.rooms else "Unassigned"

            category = "Other"
            cat_val = ad.get("ZACCESSORYCATEGORY")
            if cat_val is not None:
                category = str(cat_val)

            manufacturer = ad.get("ZMANUFACTURER") or "Unknown"
            model = ad.get("ZMODEL") or "Unknown"
            firmware = ad.get("ZFIRMWAREVERSION") or ""
            serial = ad.get("ZSERIALNUMBER") or ""

            acc = Accessory(
                id=a_uuid,
                name=a_name,
                room_id=r_uuid,
                room_name=r_name,
                category=category,
                manufacturer=manufacturer,
                model=model,
                firmware_version=firmware,
                serial_number=serial,
            )
            household.accessories[a_uuid] = acc
            for form in _id_forms(a_pk) + _id_forms(ad.get("ZUUID")):
                acc_ref_to_id.setdefault(form, a_uuid)
            if r_uuid and r_uuid in household.rooms:
                household.rooms[r_uuid].accessory_ids.append(a_uuid)

    # 5. Extract ActionSets (Scenes)
    actionset_pk_to_id: Dict[int, str] = {}
    if actionset_table:
        cursor.execute(f"SELECT * FROM {actionset_table} {home_filter}", (home_pk,))
        for s in cursor.fetchall():
            sd = dict(s)
            s_pk = sd.get("Z_PK")
            s_name = sd.get("ZNAME") or "Scene"
            s_uuid = _row_id(sd.get("ZUUID"), actionset_table, s_pk)
            actionset_pk_to_id[s_pk] = s_uuid

            # Inspect serialized actions if present
            actions: List[Action] = []
            for col in ("ZSERIALIZEDDATA", "ZACTIONS", "ZDATA"):
                if col in sd and sd[col]:
                    plist_data = _safe_load_plist(sd[col])
                    if isinstance(plist_data, dict):
                        # Decode action items
                        raw_acts = plist_data.get("HMActionSetSerializedActions") or plist_data.get("actions", [])
                        for ra in raw_acts:
                            if isinstance(ra, dict):
                                ref = ra.get("accessoryIdentifier")
                                target = next((acc_ref_to_id[f] for f in _id_forms(ref) if f in acc_ref_to_id), None)
                                actions.append(Action(
                                    target_accessory_id=target or _raw_text(ref),  # unresolved: kept raw, counted
                                    target_service_id=_raw_text(ra.get("serviceIdentifier")) or None,
                                    target_characteristic_type=ra.get("characteristicType", ""),
                                    target_value=ra.get("targetValue"),
                                ))

            household.scenes[s_uuid] = Scene(id=s_uuid, name=s_name, actions=actions)

    # 6. Extract Triggers & Automations (including Shortcut decoding)
    if trigger_table:
        cursor.execute(f"SELECT * FROM {trigger_table} {home_filter}", (home_pk,))
        for t in cursor.fetchall():
            td = dict(t)
            t_pk = td.get("Z_PK")
            t_name = td.get("ZNAME") or "Automation"
            t_uuid = _row_id(td.get("ZUUID"), trigger_table, t_pk)

            is_shortcut = False
            shortcut_workflow = None

            # Check if there is converted shortcut data in the blobs
            for col in ("ZSHORTCUTACTIONS", "ZWORKFLOW", "ZDATA", "ZSERIALIZEDDATA"):
                if col in td and td[col]:
                    plist_data = _safe_load_plist(td[col])
                    if plist_data and isinstance(plist_data, dict):
                        is_shortcut = True
                        shortcut_workflow = plist_data

            household.automations[t_uuid] = Automation(
                id=t_uuid,
                name=t_name,
                is_enabled=bool(td.get("ZENABLED", True)),
                trigger_type="homed_trigger",
                is_shortcut=is_shortcut,
                shortcut_details=shortcut_workflow,
            )

    conn.close()
    return household


# The real store's schema. Every query names its columns: the same tables hold Wi-Fi keys,
# passwords, serial numbers, MAC addresses and the home's location, and none of those columns is ever read.
ZMKF_REQUIRED = {
    "home": "ZMKFHOME",
    "room": "ZMKFROOM",
    "accessory": "ZMKFACCESSORY",
    "service": "ZMKFSERVICE",
    "characteristic": "ZMKFCHARACTERISTIC",
    "action set": "ZMKFACTIONSET",
    "action": "ZMKFACTION",
    "trigger": "ZMKFTRIGGER",
}
ZMKF_COLUMNS = {
    "ZMKFHOME": ("ZNAME", "ZMODELID"),
    "ZMKFROOM": ("ZHOME", "ZNAME", "ZMODELID"),
    "ZMKFZONE": ("ZHOME", "ZNAME", "ZMODELID"),
    "ZMKFACCESSORY": ("ZHOME", "ZROOM", "ZCONFIGUREDNAME", "ZPROVIDEDNAME", "ZMANUFACTURER", "ZMODEL",
                      "ZDISPLAYABLEFIRMWAREVERSION", "ZFIRMWAREVERSION", "ZACCESSORYCATEGORY", "ZMODELID",
                      "ZCOMMUNICATIONPROTOCOL", "ZSUPPORTEDLINKLAYERTYPES", "ZMATTERVENDORID", "ZMATTERPRODUCTID",
                      "ZSLEEPINTERVAL"),
    "ZMKFSERVICE": ("ZACCESSORY", "ZNAME", "ZSERVICETYPE", "ZMODELID"),
    "ZMKFCHARACTERISTIC": ("ZSERVICE", "ZINSTANCEID", "ZTYPE", "ZFORMAT", "ZUNITS", "ZMINIMUMVALUE", "ZMAXIMUMVALUE",
                           "ZSTEPVALUE"),
    "ZMKFACTIONSET": ("ZHOME", "ZNAME", "ZTYPE", "ZMODELID"),
    "ZMKFACTION": ("ZACTIONSET", "ZACCESSORY", "ZACCESSORY1", "ZACCESSORY2", "ZSERVICE", "ZCHARACTERISTICID",
                   "ZTARGETVALUE"),
    "ZMKFTRIGGER": ("ZHOME", "ZNAME", "ZCONFIGUREDNAME", "ZACTIVE", "ZMODELID", "ZEVALUATIONCONDITION"),
    "ZMKFEVENT": ("ZTRIGGER", "ZSERVICE", "ZCHARACTERISTICID", "ZMIN", "ZMAX", "ZEVENTVALUE", "ZDURATION", "ZENDEVENT"),
    "ZMKFSERVICEGROUP": ("ZHOME", "ZNAME", "ZMODELID"),
    "ZMKFSOFTWAREUPDATE": ("ZACCESSORY", "ZSTATE", "ZDISPLAYABLESOFTWAREVERSION"),
}
# Services that never name an accessory or say what it senses
_PLUMBING_SERVICES = {"AccessoryInformation", "Battery", "ProtocolInformation", "Pairing", "ThreadTransport",
                      "AccessoryRuntimeInformation", "FirmwareUpdate"}
# The accessory column each action subentity uses; any other subentity uses ZACCESSORY
ZMKF_ACTION_ACCESSORY = {"MKFCharacteristicWriteAction": "ZACCESSORY1", "MKFNaturalLightingAction": "ZACCESSORY2"}
TRIGGER_OWNED = "HMActionSetTypeTriggerOwned"


def _zmkf_rows(cursor: sqlite3.Cursor, table: str, where: str = "", params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    columns = ", ".join(("Z_PK", "Z_ENT") + ZMKF_COLUMNS[table])
    cursor.execute(f"SELECT {columns} FROM {table} {where}", params)
    return [dict(r) for r in cursor.fetchall()]


def _join_table(cursor: sqlite3.Cursor, tables: Dict[str, str], col_a: str, col_b: str) -> Optional[str]:
    """The table whose columns are exactly the two named ones (Core Data names it after two entity numbers)."""
    for table in tables.values():
        cursor.execute("SELECT name FROM pragma_table_info(?)", (table,))
        if {row[0] for row in cursor.fetchall()} == {col_a, col_b}:
            return table
    return None


_OPERATORS = {0: "<", 1: "<=", 2: ">", 3: ">=", 4: "==", 5: "!=", 10: "IN", 99: "CONTAINS", 100: "BETWEEN"}
_UNSET = 0x7FFFFFFFFFFFFFFF  # NSDateComponentUndefined


class _Undecodable(Exception):
    pass


def render_condition(blob: Any) -> str:
    """An archived NSPredicate as text: key paths, operators, numbers and date offsets; any other constant is <value>."""
    size = len(blob) if isinstance(blob, (bytes, bytearray)) else 0
    try:
        archive = plistlib.loads(bytes(blob))
        objects = archive["$objects"]
        root = archive["$top"]["root"]
    except Exception:
        return f"condition: undecoded ({size} bytes)"

    def obj(ref: Any) -> Any:
        return objects[ref.data] if isinstance(ref, plistlib.UID) else ref

    def cls(o: Any) -> str:
        return obj(o["$class"])["$classname"] if isinstance(o, dict) and "$class" in o else ""

    def items(ref: Any) -> List[Any]:
        o = obj(ref)
        return [obj(r) for r in o.get("NS.objects", [])] if isinstance(o, dict) else []

    def constant(value: Any) -> str:
        value = obj(value)
        if isinstance(value, bool) or not isinstance(value, (int, float, dict)):
            return "<value>"
        if isinstance(value, (int, float)):
            return str(value)
        if cls(value) == "NSDateComponents":
            hour, minute = value.get("NS.hour", 0), value.get("NS.minute", 0)
            hour, minute = (0 if hour == _UNSET else hour), (0 if minute == _UNSET else minute)
            total = hour * 60 + minute
            return f"{'-' if total < 0 else '+'}{abs(total) // 60}h{abs(total) % 60:02d}"
        return "<value>"

    def expr(ref: Any) -> str:
        o = obj(ref)
        name = cls(o)
        if name == "NSSelfExpression":
            return "SELF"
        if name == "NSConstantValueExpression":
            return constant(o.get("NSConstantValue"))
        if name == "NSKeyPathSpecifierExpression":
            path = obj(o.get("NSKeyPath"))
            return path if isinstance(path, str) else "<value>"
        if name == "NSKeyPathExpression":
            args = [expr(a) for a in items(o.get("NSArguments"))]
            operand = obj(o.get("NSOperand"))
            prefix = "" if cls(operand) in ("NSSelfExpression", "") else expr(operand) + "."
            return prefix + ".".join(args)
        if name == "NSFunctionExpression":
            selector = obj(o.get("NSSelectorName"))
            args = [expr(a) for a in items(o.get("NSArguments"))]
            operand = obj(o.get("NSOperand"))
            if operand is not None and cls(operand) not in ("", "NSSelfExpression") and "NSOperand" in o:
                args = [expr(operand)] + args
            return f"{str(selector).rstrip(':')}({', '.join(args)})"
        raise _Undecodable(name)

    def pred(ref: Any) -> str:
        o = obj(ref)
        name = cls(o)
        if name == "NSCompoundPredicate":
            parts = [pred(p) for p in items(o.get("NSSubpredicates"))]
            kind = o.get("NSCompoundPredicateType")
            if kind == 0:
                return f"NOT ({' AND '.join(parts)})"
            return "(" + (" AND " if kind == 1 else " OR ").join(parts) + ")"
        if name == "NSComparisonPredicate":
            operator = obj(o.get("NSPredicateOperator"))
            op = _OPERATORS.get(operator.get("NSOperatorType") if isinstance(operator, dict) else None, "?")
            return f"{expr(o.get('NSLeftExpression'))} {op} {expr(o.get('NSRightExpression'))}"
        raise _Undecodable(name)

    try:
        return pred(root)
    except Exception:
        return f"condition: undecoded ({size} bytes)"


def _kind(entity_name: str) -> str:
    """MKFSignificantTimeEvent -> SignificantTime."""
    name = entity_name[3:] if entity_name.startswith("MKF") else entity_name
    return name[:-5] if name.endswith("Event") else name


def _parse_zmkf(cursor: sqlite3.Cursor, tables: Dict[str, str], household_name: Optional[str]) -> Household:
    missing = [name for name, table in ZMKF_REQUIRED.items() if table not in tables]
    if "Z_PRIMARYKEY" not in tables:
        missing.append("Z_PRIMARYKEY")
    if missing:
        raise ValueError(f"SQLite file is missing required HomeKit table(s) {missing} (tables present: {list(tables.keys())})")

    cursor.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")
    entity_names: Dict[int, str] = {row[0]: row[1] for row in cursor.fetchall()}
    entity_numbers = {name: ent for ent, name in entity_names.items()}

    # 1. Select the home: with several homes the name is required, and an unknown name never falls back
    homes = _zmkf_rows(cursor, "ZMKFHOME")
    if not homes:
        raise ValueError("No homes found in database.")
    names = [h["ZNAME"] for h in homes]
    if household_name:
        matches = [h for h in homes if h["ZNAME"] and str(h["ZNAME"]).lower() == household_name.lower()]
        if not matches:
            raise ValueError(f"No home named {household_name!r}; homes in the store: {names}")
        if len(matches) > 1:
            raise ValueError(f"The home name {household_name!r} is ambiguous: several homes match")
        home = matches[0]
    elif len(homes) > 1:
        raise ValueError(f"The store has {len(homes)} homes; pass --home with one of: {names}")
    else:
        home = homes[0]

    home_pk = home["Z_PK"]
    fallback = 0  # rows whose id comes from Z_PK because ZMODELID is empty
    fallback += not home["ZMODELID"]
    home_filter = ("WHERE ZHOME = ? OR ZHOME IS NULL" if len(homes) == 1 else "WHERE ZHOME = ?", (home_pk,))
    household = Household(id=_row_id(home["ZMODELID"], "ZMKFHOME", home_pk), name=home["ZNAME"] or "Home", is_primary=True)

    # 2. Rooms and zones
    room_pk_to_id: Dict[int, str] = {}
    for r in _zmkf_rows(cursor, "ZMKFROOM", *home_filter):
        r_id = _row_id(r["ZMODELID"], "ZMKFROOM", r["Z_PK"])
        fallback += not r["ZMODELID"]
        room_pk_to_id[r["Z_PK"]] = r_id
        household.rooms[r_id] = Room(id=r_id, name=r["ZNAME"] or "Unknown Room")
    if "ZMKFZONE" not in tables:
        household.unavailable.append("zones")
    else:
        for z in _zmkf_rows(cursor, "ZMKFZONE", *home_filter):
            z_id = _row_id(z["ZMODELID"], "ZMKFZONE", z["Z_PK"])
            fallback += not z["ZMODELID"]
            household.zones[z_id] = Zone(id=z_id, name=z["ZNAME"] or "Unknown Zone")

    # 3. Accessories
    acc_pk_to_id: Dict[int, str] = {}
    not_configured: set = set()
    for a in _zmkf_rows(cursor, "ZMKFACCESSORY", *home_filter):
        a_id = _row_id(a["ZMODELID"], "ZMKFACCESSORY", a["Z_PK"])
        fallback += not a["ZMODELID"]
        r_id = room_pk_to_id.get(a["ZROOM"])
        acc_pk_to_id[a["Z_PK"]] = a_id
        if not a["ZCONFIGUREDNAME"]:
            not_configured.add(a_id)
        proto, link, cat = a["ZCOMMUNICATIONPROTOCOL"], a["ZSUPPORTEDLINKLAYERTYPES"], a["ZACCESSORYCATEGORY"]
        given = a["ZCONFIGUREDNAME"] or a["ZPROVIDEDNAME"]
        household.accessories[a_id] = Accessory(
            id=a_id,
            name=a["ZCONFIGUREDNAME"] or a["ZPROVIDEDNAME"] or "Accessory",
            room_id=r_id,
            room_name=household.rooms[r_id].name if r_id else "Unassigned",
            category=str(a["ZACCESSORYCATEGORY"]) if a["ZACCESSORYCATEGORY"] is not None else "Other",
            manufacturer=a["ZMANUFACTURER"] or "Unknown",
            model=a["ZMODEL"] or "Unknown",
            firmware_version=a["ZDISPLAYABLEFIRMWAREVERSION"] or a["ZFIRMWAREVERSION"] or "",
            protocol=hap_types.PROTOCOLS.get(proto, f"unknown:{proto}") if proto is not None else "Unknown",
            radio=hap_types.LINK_LAYERS.get(link, f"unknown:{link}") if link is not None else "Unknown",
            category_name=hap_types.CATEGORY_NAMES.get(cat, str(cat)) if cat is not None else "",
            matter_vendor_id=a["ZMATTERVENDORID"] or None,
            matter_product_id=a["ZMATTERPRODUCTID"] or None,
            sleep_interval_ms=a["ZSLEEPINTERVAL"],
            display_name=given or " ".join(p for p in (a["ZMANUFACTURER"], a["ZMODEL"]) if p) or "Accessory",
            unnamed=not given,
        )
        if r_id:
            household.rooms[r_id].accessory_ids.append(a_id)

    # 4. Services and their characteristics, attached to this home's accessories
    services: Dict[int, Service] = {}
    service_acc: Dict[int, str] = {}
    for s in _zmkf_rows(cursor, "ZMKFSERVICE"):
        a_id = acc_pk_to_id.get(s["ZACCESSORY"])
        if a_id is None:
            continue
        s_type = _raw_text(s["ZSERVICETYPE"])
        fallback += not s["ZMODELID"]
        svc = Service(id=_row_id(s["ZMODELID"], "ZMKFSERVICE", s["Z_PK"]), name=s["ZNAME"] or "",
                      service_type=s_type, type_name=hap_types.type_name(s_type, hap_types.SERVICE_NAMES))
        services[s["Z_PK"]] = svc
        service_acc[s["Z_PK"]] = a_id
        acc = household.accessories[a_id]
        acc.services.append(svc)
        if svc.type_name in hap_types.SENSES and hap_types.SENSES[svc.type_name] not in acc.senses:
            acc.senses = sorted(acc.senses + [hap_types.SENSES[svc.type_name]])
        acc.has_battery = acc.has_battery or svc.type_name == "Battery"
        if svc.type_name == "Television" and svc.name and a_id in not_configured:  # no other service names it
            acc.display_name, acc.unnamed = svc.name, False
        if svc.type_name == "ThreadTransport" and acc.radio in ("Unknown", "unknown:None"):
            acc.radio = "Thread"
    char_type: Dict[Tuple[int, Any], str] = {}
    char_name: Dict[Tuple[int, Any], str] = {}
    for c in _zmkf_rows(cursor, "ZMKFCHARACTERISTIC"):
        svc = services.get(c["ZSERVICE"])
        if svc is None:
            continue
        c_type = _raw_text(c["ZTYPE"])
        c_name = hap_types.type_name(c_type, hap_types.CHARACTERISTIC_NAMES)
        svc.characteristics.append(Characteristic(id=_row_id(None, "ZMKFCHARACTERISTIC", c["Z_PK"]), type=c_type,
                                                  format=c["ZFORMAT"] or "", unit=c["ZUNITS"], type_name=c_name,
                                                  min=c["ZMINIMUMVALUE"], max=c["ZMAXIMUMVALUE"],
                                                  step=c["ZSTEPVALUE"]))
        char_type[(c["ZSERVICE"], c["ZINSTANCEID"])] = c_type
        char_name[(c["ZSERVICE"], c["ZINSTANCEID"])] = c_name

    # 4b. Groups, through the join named after the service and group entity numbers; pending updates
    if "ZMKFSERVICEGROUP" not in tables:
        household.unavailable.append("service_groups")
    else:
        groups = {g["Z_PK"]: ServiceGroup(id=_row_id(g["ZMODELID"], "ZMKFSERVICEGROUP", g["Z_PK"]), name=g["ZNAME"] or "")
                  for g in _zmkf_rows(cursor, "ZMKFSERVICEGROUP", "WHERE ZHOME = ?", (home_pk,))}
        fallback += sum(1 for g in _zmkf_rows(cursor, "ZMKFSERVICEGROUP", "WHERE ZHOME = ?", (home_pk,))
                        if not g["ZMODELID"])
        if groups:
            svc_col = f"Z_{entity_numbers.get('MKFService')}SERVICES_"
            grp_col = f"Z_{entity_numbers.get('MKFServiceGroup')}SERVICEGROUPS_"
            group_join = _join_table(cursor, tables, svc_col, grp_col)
            if not group_join:
                raise ValueError(f"No service/group join table with columns {svc_col}, {grp_col}")
            cursor.execute(f"SELECT {svc_col}, {grp_col} FROM {group_join}")
            for svc_pk, grp_pk in cursor.fetchall():
                group = groups.get(grp_pk)
                if group is None or svc_pk not in services:
                    continue
                group.service_ids.append(services[svc_pk].id)
                if service_acc[svc_pk] not in group.accessory_ids:
                    group.accessory_ids.append(service_acc[svc_pk])
        for group in groups.values():
            household.service_groups[group.id] = group
    if "ZMKFSOFTWAREUPDATE" not in tables:
        household.unavailable.append("pending_updates")
    else:
        for u in _zmkf_rows(cursor, "ZMKFSOFTWAREUPDATE"):
            a_id = acc_pk_to_id.get(u["ZACCESSORY"])
            if a_id:
                household.accessories[a_id].pending_update = {"state": u["ZSTATE"],
                                                              "version": u["ZDISPLAYABLESOFTWAREVERSION"]}

    # 5. Action sets: built-in and user sets are scenes; a trigger-owned set's actions go to its automation
    set_pk_to_id: Dict[int, str] = {}
    owned_sets: set = set()
    for s in _zmkf_rows(cursor, "ZMKFACTIONSET", *home_filter):
        s_id = _row_id(s["ZMODELID"], "ZMKFACTIONSET", s["Z_PK"])
        fallback += not s["ZMODELID"]
        set_pk_to_id[s["Z_PK"]] = s_id
        if s["ZTYPE"] == TRIGGER_OWNED:
            owned_sets.add(s["Z_PK"])
        else:
            s_type = (s["ZTYPE"] or "UserDefined").replace("HMActionSetType", "", 1)
            household.scenes[s_id] = Scene(id=s_id, name=s["ZNAME"] or "Scene", scene_type=s_type)

    actions_by_set: Dict[int, List[Action]] = {}
    for a in _zmkf_rows(cursor, "ZMKFACTION"):
        if a["ZACTIONSET"] not in set_pk_to_id:
            continue
        acc_pk = a[ZMKF_ACTION_ACCESSORY.get(entity_names.get(a["Z_ENT"], ""), "ZACCESSORY")]
        svc = services.get(a["ZSERVICE"])
        value = a["ZTARGETVALUE"]
        action = Action(
            target_accessory_id=acc_pk_to_id.get(acc_pk) or _raw_text(acc_pk),  # unresolved: kept raw, counted
            target_service_id=svc.id if svc else None,
            target_characteristic_type=char_type.get((a["ZSERVICE"], a["ZCHARACTERISTICID"]), ""),
            target_value=hap_types.decode_opack(value),
            service_name=svc.name if svc else "",
        )
        actions_by_set.setdefault(a["ZACTIONSET"], []).append(action)
    for set_pk, acts in actions_by_set.items():
        scene = household.scenes.get(set_pk_to_id[set_pk])
        if scene:
            scene.actions.extend(acts)

    # 6. Trigger -> action set links, through the join table named after the two entity numbers (never hard-coded)
    links: Dict[int, List[int]] = {}
    a_ent, t_ent = entity_numbers.get("MKFActionSet"), entity_numbers.get("MKFTrigger")
    set_col, trig_col = f"Z_{a_ent}ACTIONSETS_", f"Z_{t_ent}TRIGGERS_"
    join_table = _join_table(cursor, tables, set_col, trig_col)
    if join_table:
        cursor.execute(f"SELECT {set_col}, {trig_col} FROM {join_table}")
        for set_pk, trig_pk in cursor.fetchall():
            links.setdefault(trig_pk, []).append(set_pk)
    elif owned_sets:
        raise ValueError(f"No trigger/action-set join table with columns {set_col}, {trig_col}")

    # 7. Automations; the trigger kind comes from the event subentity names; event details from named columns only
    event_kinds: Dict[int, set] = {}
    event_details: Dict[int, List[Dict[str, Any]]] = {}
    auto_off: Dict[int, float] = {}
    events_known = "ZMKFEVENT" in tables  # an older or changed store may lack it: then no trigger detail is known
    if not events_known:
        household.unavailable.append("events")
    if events_known:
        for e in _zmkf_rows(cursor, "ZMKFEVENT"):
            kind = _kind(entity_names.get(e["Z_ENT"], ""))
            event_kinds.setdefault(e["ZTRIGGER"], set()).add(kind)
            detail: Dict[str, Any] = {"kind": kind}
            if e["ZSERVICE"] is not None:
                a_id = service_acc.get(e["ZSERVICE"])
                detail["accessory"] = household.accessories[a_id].label if a_id else None
                detail["accessory_id"] = a_id
                detail["characteristic"] = char_name.get((e["ZSERVICE"], e["ZCHARACTERISTICID"]), "")
            if e["ZMIN"] is not None or e["ZMAX"] is not None:
                detail["min"] = hap_types.decode_opack(e["ZMIN"])
                detail["max"] = hap_types.decode_opack(e["ZMAX"])
            if e["ZEVENTVALUE"] is not None:
                detail["value"] = hap_types.decode_opack(e["ZEVENTVALUE"])
            if e["ZDURATION"] is not None:
                detail["duration_s"] = e["ZDURATION"]
            detail["is_end_event"] = bool(e["ZENDEVENT"])
            if detail["is_end_event"] and e["ZDURATION"] is not None:
                auto_off[e["ZTRIGGER"]] = e["ZDURATION"]
            event_details.setdefault(e["ZTRIGGER"], []).append(detail)
    for t in _zmkf_rows(cursor, "ZMKFTRIGGER", *home_filter):
        t_id = _row_id(t["ZMODELID"], "ZMKFTRIGGER", t["Z_PK"])
        fallback += not t["ZMODELID"]
        if entity_names.get(t["Z_ENT"]) == "MKFTimerTrigger":
            trigger_type = "timer"
        else:
            trigger_type = "+".join(sorted(k for k in event_kinds.get(t["Z_PK"], set()) if k)) or "event"
        linked = [p for p in links.get(t["Z_PK"], []) if p in set_pk_to_id]
        condition = t["ZEVALUATIONCONDITION"]
        household.automations[t_id] = Automation(
            id=t_id,
            name=t["ZCONFIGUREDNAME"] or t["ZNAME"] or "Automation",
            is_enabled=bool(t["ZACTIVE"]),
            trigger_type=trigger_type,
            action_set_ids=[set_pk_to_id[p] for p in linked if p not in owned_sets],
            actions=[act for p in linked if p in owned_sets for act in actions_by_set.get(p, [])],
            triggers=event_details.get(t["Z_PK"], [] if events_known or trigger_type == "timer"
                                       else [{"kind": "unknown", "note": "event table absent from this store"}]),
            conditions=[{"text": render_condition(condition)}] if condition is not None else [],
            auto_off_s=auto_off.get(t["Z_PK"]),
            hidden_in_app=events_known and entity_names.get(t["Z_ENT"]) == "MKFEventTrigger" and t["Z_PK"] not in event_kinds,
        )

    household.metadata["fallback_ids"] = int(fallback)
    return household
