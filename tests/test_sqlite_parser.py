"""Test reading Apple HomeKit CoreData SQLite database."""

import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
import hashlib
from src.store_files import DB
from src.audit.bundle_generator import BUNDLE_FILES, generate_llm_audit_bundle
from src.extractor.sqlite_parser import ZMKF_REQUIRED, _row_id, parse_homed_sqlite

TABLES = {
    "ZHMHOMEMODEL": "Z_PK INTEGER PRIMARY KEY, ZNAME VARCHAR, ZUUID BLOB",
    "ZHMROOMMODEL": "Z_PK INTEGER PRIMARY KEY, ZHOME INTEGER, ZNAME VARCHAR, ZUUID BLOB",
    "ZHMACCESSORYMODEL": (
        "Z_PK INTEGER PRIMARY KEY, ZHOME INTEGER, ZROOM INTEGER, ZNAME VARCHAR, ZMANUFACTURER VARCHAR, "
        "ZMODEL VARCHAR, ZFIRMWAREVERSION VARCHAR, ZACCESSORYCATEGORY INTEGER, ZUUID BLOB"
    ),
    "ZHMACTIONSETMODEL": "Z_PK INTEGER PRIMARY KEY, ZHOME INTEGER, ZNAME VARCHAR, ZSERIALIZEDDATA BLOB, ZUUID BLOB",
    "ZHMTRIGGERMODEL": (
        "Z_PK INTEGER PRIMARY KEY, ZHOME INTEGER, ZNAME VARCHAR, ZENABLED INTEGER, ZSHORTCUTACTIONS BLOB, ZUUID BLOB"
    ),
}


def _actions_blob(*identifiers):
    return plistlib.dumps({"HMActionSetSerializedActions": [
        {"accessoryIdentifier": i, "characteristicType": "PowerState", "targetValue": False} for i in identifiers
    ]}, fmt=plistlib.FMT_BINARY)


def _build_store(db_file, skip_table=None, with_uuids=True):
    """The base fixture: one home, one room (pk 10), one accessory (pk 100), one scene (pk 200), one trigger (pk 300)."""
    u = (lambda b: b) if with_uuids else (lambda b: None)
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    for name, cols in TABLES.items():
        if name != skip_table:
            cur.execute(f"CREATE TABLE {name} ({cols})")
    cur.execute("INSERT INTO ZHMHOMEMODEL (Z_PK, ZNAME, ZUUID) VALUES (1, 'Maple House', ?)", (u(b"0123456789abcdef"),))
    cur.execute("INSERT INTO ZHMROOMMODEL (Z_PK, ZHOME, ZNAME, ZUUID) VALUES (10, 1, 'Master Bedroom', ?)",
                (u(b"room000000000001"),))
    cur.execute("""
        INSERT INTO ZHMACCESSORYMODEL (Z_PK, ZHOME, ZROOM, ZNAME, ZMANUFACTURER, ZMODEL, ZFIRMWAREVERSION, ZACCESSORYCATEGORY, ZUUID)
        VALUES (100, 1, 10, 'Bedside Lamp', 'Hooli', 'Hooli Light', '1.0.0', 3, ?)
    """, (u(b"acc0000000000001"),))
    cur.execute("INSERT INTO ZHMACTIONSETMODEL (Z_PK, ZHOME, ZNAME, ZSERIALIZEDDATA, ZUUID) VALUES (200, 1, 'Sleep', ?, ?)",
                (_actions_blob("acc0000000000001"), u(b"scene00000000001")))
    if skip_table != "ZHMTRIGGERMODEL":
        shortcut_data = plistlib.dumps({"WFWorkflowActions": [{"WFWorkflowActionIdentifier": "is.workflow.actions.conditional"}]},
                                       fmt=plistlib.FMT_BINARY)
        cur.execute("INSERT INTO ZHMTRIGGERMODEL (Z_PK, ZHOME, ZNAME, ZENABLED, ZSHORTCUTACTIONS, ZUUID) "
                    "VALUES (300, 1, 'Night Shortcut', 1, ?, ?)", (shortcut_data, u(b"trig000000000001")))
    conn.commit()
    return conn


class TestSqliteParser(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_file = Path(self._td.name) / DB

    def tearDown(self):
        self._td.cleanup()

    def test_parse_homed_sqlite(self):
        _build_store(self.db_file).close()

        household = parse_homed_sqlite(self.db_file)
        self.assertEqual(household.name, "Maple House")
        self.assertEqual(len(household.rooms), 1)
        self.assertEqual(len(household.accessories), 1)
        self.assertEqual(len(household.scenes), 1)
        self.assertEqual(len(household.automations), 1)

        # Check parsed accessory
        acc = list(household.accessories.values())[0]
        self.assertEqual(acc.name, "Bedside Lamp")
        self.assertEqual(acc.room_name, "Master Bedroom")

        # Check shortcut detection
        auto = list(household.automations.values())[0]
        self.assertEqual(auto.name, "Night Shortcut")
        self.assertTrue(auto.is_shortcut)
        self.assertIsNotNone(auto.shortcut_details)

    def test_scene_action_resolves_to_its_accessory(self):
        """HA-3: the action's text identifier joins the accessory whose ZUUID is the same bytes."""
        _build_store(self.db_file).close()

        household = parse_homed_sqlite(self.db_file)
        action = list(household.scenes.values())[0].actions[0]
        self.assertEqual(household.accessories[action.target_accessory_id].name, "Bedside Lamp")
        self.assertIs(action.target_value, False)
        self.assertEqual(household.unresolved_action_refs(), 0)

    def test_null_home_rows_stay_out_of_the_selected_home_when_there_are_two(self):
        """HA-10: with two homes, a room whose ZHOME is NULL is not merged into the selected one."""
        conn = _build_store(self.db_file)
        conn.execute("INSERT INTO ZHMHOMEMODEL (Z_PK, ZNAME, ZUUID) VALUES (2, 'Lake Cabin', ?)", (b"home000000000002",))
        conn.execute("INSERT INTO ZHMROOMMODEL (Z_PK, ZHOME, ZNAME, ZUUID) VALUES (11, NULL, 'Stray Room', ?)", (b"room000000000002",))
        conn.commit()
        conn.close()

        household = parse_homed_sqlite(self.db_file, household_name="Maple House")
        self.assertEqual(sorted(r.name for r in household.rooms.values()), ["Master Bedroom"])

    def test_null_home_rows_belong_to_the_only_home(self):
        conn = _build_store(self.db_file)
        conn.execute("INSERT INTO ZHMROOMMODEL (Z_PK, ZHOME, ZNAME, ZUUID) VALUES (11, NULL, 'Hallway', ?)", (b"room000000000002",))
        conn.commit()
        conn.close()

        household = parse_homed_sqlite(self.db_file)
        self.assertEqual(sorted(r.name for r in household.rooms.values()), ["Hallway", "Master Bedroom"])

    def test_ids_without_zuuid_are_stable_across_runs(self):
        """HA-10, HB-4: no uuid4(); the id derives from table + Z_PK."""
        _build_store(self.db_file, with_uuids=False).close()

        first = parse_homed_sqlite(self.db_file)
        second = parse_homed_sqlite(self.db_file)
        self.assertEqual(first.id, second.id)
        self.assertEqual(sorted(first.rooms), sorted(second.rooms))
        self.assertEqual(sorted(first.accessories), sorted(second.accessories))
        self.assertEqual(sorted(first.scenes), sorted(second.scenes))
        self.assertEqual(sorted(first.automations), sorted(second.automations))

    def test_missing_required_table_raises(self):
        """HA-4, HB-3: a missing trigger table raises and names it."""
        _build_store(self.db_file, skip_table="ZHMTRIGGERMODEL").close()

        with self.assertRaises(ValueError) as ctx:
            parse_homed_sqlite(self.db_file)
        self.assertIn("trigger", str(ctx.exception))

    def test_missing_zone_table_does_not_raise(self):
        # The base fixture has no zone table.
        _build_store(self.db_file).close()
        self.assertEqual(len(parse_homed_sqlite(self.db_file).zones), 0)

    def test_unknown_identifier_is_kept_raw_and_counted(self):
        """HC-4, HD-1: an action that matches no accessory keeps its identifier and is counted."""
        conn = _build_store(self.db_file)
        conn.execute("UPDATE ZHMACTIONSETMODEL SET ZSERIALIZEDDATA = ? WHERE Z_PK = 200",
                     (_actions_blob("acc0000000000001", "ghost-device-01"),))
        conn.commit()
        conn.close()

        household = parse_homed_sqlite(self.db_file)
        ids = [a.target_accessory_id for a in list(household.scenes.values())[0].actions]
        self.assertIn("ghost-device-01", ids)
        self.assertEqual(household.unresolved_action_refs(), 1)

    def test_shared_z_pk_resolves_to_the_accessory(self):
        """HC-4, HD-3: room and accessory share Z_PK 100; an action naming 100 resolves to the accessory."""
        conn = _build_store(self.db_file)
        conn.execute("INSERT INTO ZHMROOMMODEL (Z_PK, ZHOME, ZNAME, ZUUID) VALUES (100, 1, 'Shared Pk Room', ?)",
                     (b"room000000000100",))
        conn.execute("UPDATE ZHMACTIONSETMODEL SET ZSERIALIZEDDATA = ? WHERE Z_PK = 200", (_actions_blob(100),))
        conn.commit()
        conn.close()

        household = parse_homed_sqlite(self.db_file)
        action = list(household.scenes.values())[0].actions[0]
        self.assertEqual(household.accessories[action.target_accessory_id].name, "Bedside Lamp")


# --- The real store's schema ---------------------------------------------------------------

ACC_SECRETS = ("ZPASSWORD", "ZWIFIUNIQUEPRESHAREDKEY", "ZWIFISSID", "ZSERIALNUMBER", "ZMACADDRESS",
               "ZMATTEREXTENDEDMACADDRESS", "ZPUBLICKEY", "ZBROADCASTKEY", "ZPAIRINGIDENTITY", "ZACCESSORYSETUPHASH")
HOME_SECRETS = ("ZLOCATION", "ZTIMEZONE", "ZPRIMARYWIFISSID", "ZOWNERPUBLICKEY", "ZTHREADOPERATIONALDATASET")
EVENT_SECRETS = ("ZREGION", "ZUSER")
ENTITIES = {7: "MKFActionSet", 8: "MKFTrigger", 9: "MKFEventTrigger", 10: "MKFTimerTrigger",
            20: "MKFCharacteristicWriteAction", 21: "MKFNaturalLightingAction", 22: "MKFAppleMediaAccessoryPowerAction",
            30: "MKFSignificantTimeEvent", 31: "MKFCharacteristicRangeEvent", 32: "MKFDurationEvent",
            33: "MKFCharacteristicValueEvent", 40: "MKFService", 41: "MKFServiceGroup"}
LIGHT_SERVICE = b"00000043-0000-1000-8000-0026BB765291"
ON_CHARACTERISTIC = b"00000025-0000-1000-8000-0026BB765291"
ZMKF_TABLES = {
    "ZMKFHOME": ("ZNAME", "ZMODELID") + HOME_SECRETS,
    "ZMKFROOM": ("ZHOME", "ZNAME", "ZMODELID"),
    "ZMKFACCESSORY": ("ZHOME", "ZROOM", "ZCONFIGUREDNAME", "ZPROVIDEDNAME", "ZMANUFACTURER", "ZMODEL",
                      "ZDISPLAYABLEFIRMWAREVERSION", "ZFIRMWAREVERSION", "ZACCESSORYCATEGORY", "ZMODELID",
                      "ZLOWBATTERY", "ZCOMMUNICATIONPROTOCOL", "ZSUPPORTEDLINKLAYERTYPES", "ZMATTERVENDORID",
                      "ZMATTERPRODUCTID", "ZSLEEPINTERVAL") + ACC_SECRETS,
    "ZMKFSERVICE": ("ZACCESSORY", "ZNAME", "ZSERVICETYPE", "ZMODELID"),
    "ZMKFCHARACTERISTIC": ("ZSERVICE", "ZINSTANCEID", "ZTYPE", "ZFORMAT", "ZUNITS", "ZMINIMUMVALUE", "ZMAXIMUMVALUE",
                           "ZSTEPVALUE"),
    "ZMKFACTIONSET": ("ZHOME", "ZNAME", "ZTYPE", "ZMODELID"),
    "ZMKFACTION": ("ZACTIONSET", "ZACCESSORY", "ZACCESSORY1", "ZACCESSORY2", "ZSERVICE", "ZCHARACTERISTICID",
                   "ZTARGETVALUE", "ZMODELID"),
    "ZMKFTRIGGER": ("ZHOME", "ZNAME", "ZCONFIGUREDNAME", "ZACTIVE", "ZMODELID", "ZEVALUATIONCONDITION"),
    "ZMKFEVENT": ("ZTRIGGER", "ZMODELID", "ZSERVICE", "ZCHARACTERISTICID", "ZMIN", "ZMAX", "ZEVENTVALUE", "ZDURATION",
                  "ZENDEVENT") + EVENT_SECRETS,
    "ZMKFSERVICEGROUP": ("ZHOME", "ZNAME", "ZMODELID"),
    "ZMKFSOFTWAREUPDATE": ("ZACCESSORY", "ZSTATE", "ZDISPLAYABLESOFTWAREVERSION", "ZMODELID"),
}


def _mid(table, pk):
    return hashlib.md5(f"{table}:{pk}".encode()).digest()  # a 16-byte ZMODELID


def _secret(col, pk):
    return f"SECRET-{col}-{pk}"


def _insert(cur, table, pk, ent, **values):
    cols = ZMKF_TABLES[table]
    row = {c: None for c in cols}
    row.update(values)
    if "ZMODELID" in cols:
        row["ZMODELID"] = _mid(table, pk)
    for col in cols:
        if col in ACC_SECRETS + HOME_SECRETS + EVENT_SECRETS:
            row[col] = _secret(col, pk)
    names = ["Z_PK", "Z_ENT"] + list(cols)
    cur.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [pk, ent] + [row[c] for c in cols])


def _build_zmkf(db_file, homes=3, skip_table=None, profile=False, group_join=True):
    """Three homes; Maple House (pk 1) holds the rooms, accessories, scenes and automations the tests read."""
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER)")
    cur.executemany("INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME) VALUES (?, ?)", ENTITIES.items())
    for table, cols in ZMKF_TABLES.items():
        if table != skip_table:
            cur.execute(f"CREATE TABLE {table} (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, {', '.join(cols)})")
    cur.execute("CREATE TABLE Z_7TRIGGERS_ (Z_7ACTIONSETS_ INTEGER, Z_8TRIGGERS_ INTEGER)")

    def ins(table, pk, ent=0, **values):
        if table != skip_table:
            _insert(cur, table, pk, ent, **values)

    for pk, name in [(1, "Maple House"), (2, "Lake Cabin"), (3, "Barge")][:homes]:
        ins("ZMKFHOME", pk, ZNAME=name)
    ins("ZMKFROOM", 10, ZHOME=1, ZNAME="Lounge")
    ins("ZMKFROOM", 11, ZHOME=2, ZNAME="Kitchen")
    ins("ZMKFACCESSORY", 100, ZHOME=1, ZROOM=10, ZCONFIGUREDNAME="Lamp Lounge", ZPROVIDEDNAME="Hooli Light",
        ZDISPLAYABLEFIRMWAREVERSION="2.1", ZFIRMWAREVERSION="2.0", ZACCESSORYCATEGORY=5, ZLOWBATTERY=1)
    ins("ZMKFACCESSORY", 101, ZHOME=1, ZROOM=10, ZPROVIDEDNAME="Hooli Motion", ZFIRMWAREVERSION="1.5", ZLOWBATTERY=1)
    ins("ZMKFACCESSORY", 102, ZHOME=1, ZCONFIGUREDNAME="HomePod")
    ins("ZMKFACCESSORY", 200, ZHOME=2, ZROOM=11, ZCONFIGUREDNAME="Kitchen Plug")
    ins("ZMKFSERVICE", 500, ZACCESSORY=100, ZNAME="Light", ZSERVICETYPE=LIGHT_SERVICE)
    ins("ZMKFSERVICE", 501, ZACCESSORY=200, ZNAME="Outlet", ZSERVICETYPE=b"00000047-0000-1000-8000-0026BB765291")
    ins("ZMKFCHARACTERISTIC", 600, ZSERVICE=500, ZINSTANCEID=9, ZTYPE=ON_CHARACTERISTIC, ZFORMAT="bool")
    for pk, kind in [(300, "HomeArrival"), (301, "HomeDeparture"), (302, "Sleep"), (303, "WakeUp")]:
        ins("ZMKFACTIONSET", pk, 7, ZHOME=1, ZNAME=kind, ZTYPE=f"HMActionSetType{kind}")
    ins("ZMKFACTIONSET", 304, 7, ZHOME=1, ZNAME="", ZTYPE="HMActionSetTypeTriggerOwned")
    ins("ZMKFACTIONSET", 310, 7, ZHOME=2, ZNAME="HomeArrival", ZTYPE="HMActionSetTypeHomeArrival")
    ins("ZMKFACTION", 700, 20, ZACTIONSET=300, ZACCESSORY1=100, ZSERVICE=500, ZCHARACTERISTICID=9, ZTARGETVALUE=b"\x01")
    ins("ZMKFACTION", 701, 21, ZACTIONSET=301, ZACCESSORY2=101)
    ins("ZMKFACTION", 702, 22, ZACTIONSET=302, ZACCESSORY=102)
    ins("ZMKFACTION", 703, 20, ZACTIONSET=304, ZACCESSORY1=100, ZSERVICE=500, ZCHARACTERISTICID=9, ZTARGETVALUE=b"\x00")
    ins("ZMKFACTION", 710, 20, ZACTIONSET=310, ZACCESSORY1=200)
    ins("ZMKFTRIGGER", 800, 9, ZHOME=1, ZNAME="Sunset", ZACTIVE=1)
    ins("ZMKFTRIGGER", 801, 10, ZHOME=1, ZNAME="Morning", ZCONFIGUREDNAME="Good Morning", ZACTIVE=0)
    ins("ZMKFTRIGGER", 810, 9, ZHOME=2, ZNAME="Country Sunset", ZACTIVE=1)
    ins("ZMKFEVENT", 900, 30, ZTRIGGER=800)
    cur.execute("INSERT INTO Z_7TRIGGERS_ VALUES (304, 800)")
    if profile:
        _add_profile_rows(cur, ins, group_join)
    conn.commit()
    conn.close()


class TestZmkfReader(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_file = Path(self._td.name) / DB

    def tearDown(self):
        self._td.cleanup()

    def _maple(self):
        _build_zmkf(self.db_file)
        return parse_homed_sqlite(self.db_file, household_name="Maple House")

    def _acc(self, household, name):
        return next(a for a in household.accessories.values() if a.name == name)

    def test_several_homes_need_a_name(self):
        _build_zmkf(self.db_file)
        with self.assertRaises(ValueError) as ctx:
            parse_homed_sqlite(self.db_file)
        for name in ("Maple House", "Lake Cabin", "Barge"):
            self.assertIn(name, str(ctx.exception))

    def test_unknown_home_raises_with_three_homes_and_with_one(self):
        for homes in (3, 1):
            with self.subTest(homes=homes), tempfile.TemporaryDirectory() as td:
                db = Path(td) / DB
                _build_zmkf(db, homes=homes)
                with self.assertRaises(ValueError) as ctx:
                    parse_homed_sqlite(db, household_name="Chalet")
                self.assertIn("Chalet", str(ctx.exception))
                self.assertIn("Maple House", str(ctx.exception))

    def test_named_home_gets_only_its_own_rows(self):
        household = self._maple()
        self.assertEqual(household.name, "Maple House")
        self.assertEqual(sorted(r.name for r in household.rooms.values()), ["Lounge"])
        self.assertEqual(sorted(a.name for a in household.accessories.values()), ["HomePod", "Hooli Motion", "Lamp Lounge"])
        self.assertEqual(len(household.scenes), 4)
        self.assertEqual(sorted(a.name for a in household.automations.values()), ["Good Morning", "Sunset"])

    def test_actions_resolve_by_subentity(self):
        household = self._maple()
        by_name = {s.name: s for s in household.scenes.values()}
        write = by_name["HomeArrival"].actions[0]
        lamp = self._acc(household, "Lamp Lounge")
        self.assertEqual(write.target_accessory_id, lamp.id)
        self.assertEqual(write.service_name, "Light")
        self.assertEqual(write.target_service_id, lamp.services[0].id)
        self.assertEqual(write.target_characteristic_type, ON_CHARACTERISTIC.decode())
        self.assertIs(write.target_value, True)  # OPACK 01
        self.assertEqual(by_name["HomeDeparture"].actions[0].target_accessory_id, self._acc(household, "Hooli Motion").id)
        self.assertEqual(by_name["Sleep"].actions[0].target_accessory_id, self._acc(household, "HomePod").id)
        self.assertEqual(household.unresolved_action_refs(), 0)

    def test_services_and_characteristics_sit_on_the_accessory(self):
        lamp = self._acc(self._maple(), "Lamp Lounge")
        self.assertEqual([(s.name, s.service_type) for s in lamp.services], [("Light", LIGHT_SERVICE.decode())])
        self.assertEqual([(c.type, c.format) for c in lamp.services[0].characteristics], [(ON_CHARACTERISTIC.decode(), "bool")])

    def test_names_and_firmware_fall_back(self):
        household = self._maple()
        self.assertEqual(self._acc(household, "Lamp Lounge").firmware_version, "2.1")
        self.assertEqual(self._acc(household, "Hooli Motion").firmware_version, "1.5")

    def test_scenes_and_trigger_owned_sets(self):
        household = self._maple()
        self.assertEqual(sorted(s.scene_type for s in household.scenes.values()),
                         ["HomeArrival", "HomeDeparture", "Sleep", "WakeUp"])
        sunset = next(a for a in household.automations.values() if a.name == "Sunset")
        self.assertEqual([(a.target_accessory_id, a.target_value) for a in sunset.actions],
                         [(self._acc(household, "Lamp Lounge").id, "hex:00")])

    def test_trigger_kind_and_enabled(self):
        autos = {a.name: a for a in self._maple().automations.values()}
        self.assertEqual(autos["Sunset"].trigger_type, "SignificantTime")
        self.assertTrue(autos["Sunset"].is_enabled)
        self.assertEqual(autos["Good Morning"].trigger_type, "timer")
        self.assertFalse(autos["Good Morning"].is_enabled)

    def test_planted_secrets_reach_neither_the_json_nor_the_bundle(self):
        household = self._maple()
        planted = [_secret(c, pk) for c in ACC_SECRETS for pk in (100, 101, 102)]
        planted += [_secret(c, 1) for c in HOME_SECRETS] + [_secret(c, 900) for c in EVENT_SECRETS]
        texts = {"to_json": household.to_json()}
        bundle = generate_llm_audit_bundle(household, Path(self._td.name) / "bundle")
        texts.update({name: (bundle / name).read_text(encoding="utf-8") for name in BUNDLE_FILES})
        for where, text in texts.items():
            for value in planted:
                self.assertNotIn(value, text, where)
        self.assertTrue(all(a.battery_level is None for a in household.accessories.values()))

    def test_each_missing_required_table_raises_and_names_it(self):
        for name, table in ZMKF_REQUIRED.items():
            with self.subTest(table=table), tempfile.TemporaryDirectory() as td:
                db = Path(td) / DB
                _build_zmkf(db, skip_table=table)
                with self.assertRaises(ValueError) as ctx:
                    parse_homed_sqlite(db, household_name="Maple House")
                self.assertIn(f"'{name}'", str(ctx.exception))

    def test_ids_are_stable_and_come_from_zmodelid(self):
        first, second = self._maple(), parse_homed_sqlite(self.db_file, household_name="Maple House")
        self.assertEqual(sorted(first.accessories), sorted(second.accessories))
        self.assertEqual(self._acc(first, "Lamp Lounge").id, _row_id(_mid("ZMKFACCESSORY", 100), "ZMKFACCESSORY", 100))
        self.assertEqual(first.id, _row_id(_mid("ZMKFHOME", 1), "ZMKFHOME", 1))


def _hap(code):
    return f"{code:08X}-0000-1000-8000-0026BB765291".encode()


VENDOR_SERVICE = b"deadbeef-0000-4000-8000-000000000001"  # fictional vendor service type
PLANTED_STRING = "PLANTED-CONDITION-STRING"


def _archive(root):
    """A minimal NSKeyedArchiver plist of nested {'$c': classname, key: value} dicts (lists become NSArray)."""
    objects = ["$null"]
    classes = {}

    def uid_of(name):
        if name not in classes:
            objects.append({"$classname": name, "$classes": [name, "NSObject"]})
            classes[name] = plistlib.UID(len(objects) - 1)
        return classes[name]

    def enc(value):
        if isinstance(value, (bool, int, float)):
            return value
        index = len(objects)
        objects.append(None)
        if isinstance(value, str):
            objects[index] = value
        elif isinstance(value, list):
            objects[index] = {"NS.objects": [enc(v) for v in value], "$class": uid_of("NSArray")}
        else:
            obj = {k: enc(v) for k, v in value.items() if k != "$c"}
            obj["$class"] = uid_of(value["$c"])
            objects[index] = obj
        return plistlib.UID(index)

    top = enc(root)
    return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": top},
                           "$objects": objects}, fmt=plistlib.FMT_BINARY)


def _keypath(path):
    return {"$c": "NSKeyPathExpression", "NSExpressionType": 3, "NSSelectorName": "valueForKeyPath:",
            "NSOperand": {"$c": "NSSelfExpression", "NSExpressionType": 1},
            "NSArguments": [{"$c": "NSKeyPathSpecifierExpression", "NSExpressionType": 10, "NSKeyPath": path}]}


def _constant(value):
    return {"$c": "NSConstantValueExpression", "NSExpressionType": 0, "NSConstantValue": value}


def _offset(path, minutes):
    return {"$c": "NSFunctionExpression", "NSExpressionType": 4, "NSSelectorName": "dateByAddingComponents:",
            "NSOperand": _keypath(path),
            "NSArguments": [_constant({"$c": "NSDateComponents", "NS.hour": 0, "NS.minute": minutes})]}


def _compare(left, operator, right):
    return {"$c": "NSComparisonPredicate", "NSLeftExpression": left, "NSRightExpression": right,
            "NSPredicateOperator": {"$c": "NSComparisonPredicateOperator", "NSOperatorType": operator,
                                    "NSModifier": 0, "NSOptions": 0}}


def _now():
    return {"$c": "NSFunctionExpression", "NSExpressionType": 4, "NSSelectorName": "now", "NSArguments": []}


NIGHT_CONDITION = {"$c": "NSCompoundPredicate", "NSCompoundPredicateType": 1, "NSSubpredicates": [
    _compare(_offset("sunset", -30), 1, _now()), _compare(_now(), 1, _offset("sunrise", 30))]}
STRING_CONDITION = _compare(_keypath("presence"), 4, _constant(PLANTED_STRING))


def _add_profile_rows(cur, ins, group_join):
    """Profiles, groups, events and conditions, all in Maple House (pk 1)."""
    cur.execute("UPDATE ZMKFACCESSORY SET ZCOMMUNICATIONPROTOCOL=2, ZSUPPORTEDLINKLAYERTYPES=16, ZMATTERVENDORID=65523, "
                "ZMATTERPRODUCTID=32770 WHERE Z_PK=100")
    cur.execute("UPDATE ZMKFACCESSORY SET ZCOMMUNICATIONPROTOCOL=1, ZSLEEPINTERVAL=2000, ZMATTERVENDORID=0, "
                "ZMATTERPRODUCTID=0 WHERE Z_PK=101")
    ins("ZMKFACCESSORY", 103, ZHOME=1, ZROOM=10, ZPROVIDEDNAME="Presence Sensor", ZMANUFACTURER="Globex",
        ZCOMMUNICATIONPROTOCOL=2, ZSUPPORTEDLINKLAYERTYPES=2, ZMATTERVENDORID=65521, ZMATTERPRODUCTID=32768)
    ins("ZMKFACCESSORY", 104, ZHOME=1, ZROOM=10, ZPROVIDEDNAME="Acme TV", ZCOMMUNICATIONPROTOCOL=1,
        ZACCESSORYCATEGORY=31)
    ins("ZMKFACCESSORY", 105, ZHOME=1, ZROOM=10, ZMANUFACTURER="Hooli", ZMODEL="TEST-MODEL",
        ZCOMMUNICATIONPROTOCOL=7, ZSUPPORTEDLINKLAYERTYPES=7, ZACCESSORYCATEGORY=99)
    for pk, acc, code, name in [(520, 101, 0x85, "Motion Probe"), (521, 101, 0x8A, "Température"),
                                (522, 101, 0x82, "Humidité"), (523, 101, 0x96, None), (524, 101, 0x701, None),
                                (530, 103, 0x86, "Capteur Occupation"), (531, 103, 0x84, "Lumière"),
                                (540, 104, 0x3E, None), (541, 104, 0xD8, "ZX8-TV")]:
        ins("ZMKFSERVICE", pk, ZACCESSORY=acc, ZNAME=name, ZSERVICETYPE=_hap(code))
    ins("ZMKFSERVICE", 525, ZACCESSORY=101, ZSERVICETYPE=VENDOR_SERVICE)
    for pk, svc, inst, code, extra in [
            (620, 520, 10, 0x22, {}), (621, 521, 11, 0x11, dict(ZMINIMUMVALUE=-20.0, ZMAXIMUMVALUE=100.0,
                                                                ZSTEPVALUE=0.1, ZUNITS="celsius")),
            (622, 522, 12, 0x10, {}), (623, 523, 13, 0x68, {}), (630, 530, 11, 0x71, {}),
            (631, 531, 12, 0x6B, dict(ZMINIMUMVALUE=0.0001, ZMAXIMUMVALUE=100000.0, ZUNITS="lux"))]:
        ins("ZMKFCHARACTERISTIC", pk, ZSERVICE=svc, ZINSTANCEID=inst, ZTYPE=_hap(code), **extra)
    ins("ZMKFSERVICEGROUP", 560, ZHOME=1, ZNAME="Lounge Group")
    ins("ZMKFSERVICEGROUP", 561, ZHOME=1, ZNAME="Maple House")
    ins("ZMKFSERVICEGROUP", 562, ZHOME=2, ZNAME="Country Group")
    if group_join:
        cur.execute("CREATE TABLE Z_40SERVICEGROUPS_ (Z_40SERVICES_ INTEGER, Z_41SERVICEGROUPS_ INTEGER)")
        cur.executemany("INSERT INTO Z_40SERVICEGROUPS_ VALUES (?, ?)", [(500, 560), (530, 560), (541, 561)])
    ins("ZMKFSOFTWAREUPDATE", 570, ZACCESSORY=100, ZSTATE=1, ZDISPLAYABLESOFTWAREVERSION="2.2")
    ins("ZMKFTRIGGER", 820, 9, ZHOME=1, ZNAME="Lux high", ZACTIVE=1)
    ins("ZMKFEVENT", 920, 31, ZTRIGGER=820, ZSERVICE=531, ZCHARACTERISTICID=12, ZMIN=b"\x31\x96\x00", ZENDEVENT=0)
    ins("ZMKFTRIGGER", 821, 9, ZHOME=1, ZNAME="Presence on", ZACTIVE=1, ZEVALUATIONCONDITION=_archive(NIGHT_CONDITION))
    ins("ZMKFEVENT", 921, 33, ZTRIGGER=821, ZSERVICE=530, ZCHARACTERISTICID=11, ZEVENTVALUE=b"\x09", ZENDEVENT=0)
    ins("ZMKFEVENT", 922, 32, ZTRIGGER=821, ZDURATION=300.0, ZENDEVENT=1)
    ins("ZMKFTRIGGER", 822, 9, ZHOME=1, ZNAME="Ghost", ZACTIVE=1, ZEVALUATIONCONDITION=_archive(STRING_CONDITION))
    ins("ZMKFTRIGGER", 823, 9, ZHOME=1, ZNAME="Bad condition", ZACTIVE=1, ZEVALUATIONCONDITION=b"not a plist")
    ins("ZMKFEVENT", 923, 33, ZTRIGGER=823, ZSERVICE=530, ZCHARACTERISTICID=11, ZEVENTVALUE=b"\x08", ZENDEVENT=0)


class TestZmkfProfiles(unittest.TestCase):
    """Device profiles, service groups, events and conditions."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_file = Path(self._td.name) / DB

    def tearDown(self):
        self._td.cleanup()

    def _maple(self, **kwargs):
        _build_zmkf(self.db_file, profile=True, **kwargs)
        return parse_homed_sqlite(self.db_file, household_name="Maple House")

    def _acc(self, household, name):
        return next(a for a in household.accessories.values() if a.name == name)

    def _auto(self, household, name):
        return next(a for a in household.automations.values() if a.name == name)

    def test_protocol_and_radio(self):
        h = self._maple()
        lamp, motion = self._acc(h, "Lamp Lounge"), self._acc(h, "Hooli Motion")
        presence, odd = self._acc(h, "Presence Sensor"), self._acc(h, "Accessory")
        self.assertEqual((lamp.protocol, lamp.radio), ("Matter", "Thread"))
        self.assertEqual((presence.protocol, presence.radio), ("Matter", "Wi-Fi"))
        self.assertEqual((motion.protocol, motion.radio), ("HomeKit (HAP)", "Thread"))  # Thread Transport service
        self.assertEqual((odd.protocol, odd.radio), ("unknown:7", "unknown:7"))
        self.assertEqual((lamp.matter_vendor_id, lamp.matter_product_id), (65523, 32770))
        self.assertEqual((motion.matter_vendor_id, motion.sleep_interval_ms), (None, 2000))

    def test_type_names_ranges_and_senses(self):
        h = self._maple()
        motion, presence = self._acc(h, "Hooli Motion"), self._acc(h, "Presence Sensor")
        types = {s.service_type: s.type_name for s in motion.services}
        self.assertEqual(types[_hap(0x85).decode()], "MotionSensor")
        self.assertEqual(types[VENDOR_SERVICE.decode()], "vendor:" + VENDOR_SERVICE.decode())
        temp = next(c for s in motion.services for c in s.characteristics if c.type_name == "CurrentTemperature")
        self.assertEqual((temp.min, temp.max, temp.step, temp.unit), (-20.0, 100.0, 0.1, "celsius"))
        self.assertEqual(presence.senses, ["LightLevel", "Occupancy"])
        self.assertEqual(motion.senses, ["Humidity", "Motion", "Temperature"])
        self.assertTrue(motion.has_battery)
        self.assertFalse(presence.has_battery)
        self.assertEqual(self._acc(h, "Lamp Lounge").category_name, "Lightbulb")
        self.assertEqual(self._acc(h, "Accessory").category_name, "99")
        self.assertEqual(self._acc(h, "Lamp Lounge").pending_update, {"state": 1, "version": "2.2"})
        self.assertIsNone(presence.pending_update)

    def test_display_name_takes_the_television_service_name_only(self):  # the television service name only
        h = self._maple()
        self.assertEqual(self._acc(h, "Acme TV").display_name, "ZX8-TV")
        self.assertEqual(self._acc(h, "Presence Sensor").display_name, "Presence Sensor")
        self.assertEqual(self._acc(h, "Hooli Motion").display_name, "Hooli Motion")

    def test_display_name_rule(self):
        h = self._maple()
        self.assertEqual(self._acc(h, "Lamp Lounge").display_name, "Lamp Lounge")
        unnamed = self._acc(h, "Accessory")
        self.assertEqual((unnamed.display_name, unnamed.unnamed), ("Hooli TEST-MODEL", True))
        self.assertFalse(self._acc(h, "Lamp Lounge").unnamed)

    def test_service_groups_through_the_discovered_join(self):
        h = self._maple()
        groups = {g.name: g for g in h.service_groups.values()}
        self.assertEqual(sorted(groups), ["Lounge Group", "Maple House"])
        self.assertEqual(sorted(groups["Lounge Group"].accessory_ids),
                         sorted([self._acc(h, "Lamp Lounge").id, self._acc(h, "Presence Sensor").id]))
        self.assertEqual(len(groups["Lounge Group"].service_ids), 2)
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / DB
            _build_zmkf(db, profile=True, group_join=False)
            with self.assertRaises(ValueError):
                parse_homed_sqlite(db, household_name="Maple House")

    def test_opack_subset(self):
        from src.extractor.hap_types import decode_opack
        import struct
        cases = [(b"\x08", 0), (b"\x09", 1), (b"\x01", True), (b"\x02", False), (b"\x30\x46", 70),
                 (b"\x31\xc8\x00", 200), (b"\x35" + struct.pack("<f", 100.0), 100.0), (b"\xff", "hex:ff")]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(decode_opack(raw), expected)
                self.assertIs(type(decode_opack(raw)), type(expected))

    def test_trigger_details_auto_off_and_hidden(self):
        h = self._maple()
        lux = self._auto(h, "Lux high")
        presence_id = self._acc(h, "Presence Sensor").id
        self.assertEqual(lux.triggers, [{"kind": "CharacteristicRange", "accessory": "Presence Sensor",
                                         "accessory_id": presence_id,
                                         "characteristic": "CurrentAmbientLightLevel", "min": 150, "max": None,
                                         "is_end_event": False}])
        on = self._auto(h, "Presence on")
        self.assertEqual(on.auto_off_s, 300)
        self.assertIn({"kind": "CharacteristicValue", "accessory": "Presence Sensor", "accessory_id": presence_id,
                       "characteristic": "OccupancyDetected", "value": 1, "is_end_event": False}, on.triggers)
        self.assertTrue(self._auto(h, "Ghost").hidden_in_app)
        self.assertFalse(on.hidden_in_app)
        self.assertFalse(self._auto(h, "Good Morning").hidden_in_app)  # a timer fires without events

    def test_conditions_render_without_constants(self):
        h = self._maple()
        night = self._auto(h, "Presence on").conditions[0]["text"]
        for part in ("sunset", "sunrise", "-0h30", "+0h30", "AND", "<="):
            self.assertIn(part, night)
        ghost = self._auto(h, "Ghost").conditions[0]["text"]
        self.assertIn("<value>", ghost)
        self.assertNotIn(PLANTED_STRING, ghost)
        self.assertEqual(self._auto(h, "Bad condition").conditions[0]["text"], "condition: undecoded (11 bytes)")
        self.assertEqual(self._auto(h, "Lux high").conditions, [])

    def test_bundle_shows_profiles_and_flags_and_no_secret(self):
        h = self._maple()
        bundle = generate_llm_audit_bundle(h, Path(self._td.name) / "bundle")
        files = {name: (bundle / name).read_text(encoding="utf-8") for name in BUNDLE_FILES}
        for part in ("Radio", "Thread", "Wi-Fi", "Senses", "Occupancy", "Groups", "Lounge Group",
                     "CurrentTemperature [-20.0..100.0 step 0.1 celsius]"):
            self.assertIn(part, files["01_devices_and_rooms.md"])
        for part in ("Auto-off: 300 s", "Condition", "Hidden in the Home app", "CurrentAmbientLightLevel"):
            self.assertIn(part, files["03_automations_analysis.md"])
        for part in ("Hooli TEST-MODEL", "Maple House", "Ghost", "home hub"):
            self.assertIn(part, files["04_hygiene_and_conflicts.md"])
        self.assertIn("inferred", files["00_README.md"])
        planted = [_secret(c, pk) for c in ACC_SECRETS for pk in (100, 101, 102, 103, 104, 105)]
        planted += [_secret(c, pk) for c in EVENT_SECRETS for pk in (900, 920, 921, 922, 923)] + [PLANTED_STRING]
        planted += [_secret(c, 1) for c in HOME_SECRETS]
        for name, text in files.items():
            for value in planted:
                self.assertNotIn(value, text, name)


if __name__ == "__main__":
    unittest.main()
