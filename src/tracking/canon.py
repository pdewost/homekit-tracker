"""Canonical description of a home and the home list.

The canonical form holds only compared fields, keyed by id, as JSON-native values with every list sorted, so a form
reloaded from disk equals a freshly built one. Other entities are referred to by id, never by a copied name.
"""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import safe_io
from ..extractor.sqlite_parser import _row_id
from ..models.schema import Action, Household

_SRC = Path(__file__).resolve().parent.parent
READER_FILES = (_SRC / "extractor" / "sqlite_parser.py", _SRC / "extractor" / "hap_types.py", Path(__file__).resolve())


def reader_version() -> str:
    h = hashlib.sha256()
    for path in READER_FILES:
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def _sorted(items: List[Any]) -> List[Any]:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# --- Homes ---

def list_homes(db_path: Path) -> List[Dict[str, Any]]:
    """The store's homes in Z_PK order: ordinal, id, name. Reads ZMKFHOME's Z_PK, ZMODELID and ZNAME only."""
    conn = safe_io.sqlite_ro(db_path)
    try:
        rows = conn.execute("SELECT Z_PK, ZMODELID, ZNAME FROM ZMKFHOME ORDER BY Z_PK").fetchall()
    finally:
        conn.close()
    return [{"ordinal": i, "id": _row_id(mid, "ZMKFHOME", pk), "name": name} for i, (pk, mid, name) in enumerate(rows, 1)]


def classify_homes(homes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Split homes into describable ones and (ordinal, code) failures: no name, or a name shared case-folded."""
    folded: Dict[str, int] = {}
    for h in homes:
        if h["name"]:
            key = str(h["name"]).casefold()
            folded[key] = folded.get(key, 0) + 1
    ok, failures = [], []
    for h in homes:
        if not h["name"]:
            failures.append((h["ordinal"], "E_NO_HOME_NAME"))
        elif folded[str(h["name"]).casefold()] > 1:
            failures.append((h["ordinal"], "E_DUP_HOME_NAME"))
        else:
            ok.append(h)
    return ok, failures


# --- Canonical form ---

def _actions(actions: List[Action], household: Household) -> Tuple[List[Any], int]:
    listed, unresolved = [], 0
    for a in actions:
        if a.target_accessory_id in household.accessories:
            listed.append([a.target_accessory_id, a.target_service_id, a.target_characteristic_type, a.target_value])
        else:
            unresolved += 1
    return _sorted(_json(listed)), unresolved


def _triggers(triggers: List[Dict[str, Any]]) -> List[Any]:
    """End events with a duration are left out: they are the automation's auto_off_s, compared on its own."""
    out = []
    for t in triggers:
        if t.get("is_end_event") and t.get("duration_s") is not None:
            continue
        out.append([t.get("kind"), t.get("accessory_id"), t.get("characteristic"), t.get("min"), t.get("max"),
                    t.get("value"), t.get("duration_s"), bool(t.get("is_end_event"))])
    return _sorted(_json(out))


def canonical(household: Household) -> Dict[str, Any]:
    accessories = {}
    for a in household.accessories.values():
        capabilities = [[s.type_name, _sorted([[c.type_name, c.min, c.max, c.step, c.unit] for c in s.characteristics])]
                        for s in a.services]
        accessories[a.id] = {
            # the shown name: one field, so a rename is one change (display_name and name share a source)
            "name": a.label,
            "room_id": a.room_id, "manufacturer": a.manufacturer, "model": a.model,
            "firmware_version": a.firmware_version, "protocol": a.protocol, "radio": a.radio,
            "category_name": a.category_name, "matter_vendor_id": a.matter_vendor_id,
            "matter_product_id": a.matter_product_id, "has_battery": a.has_battery,
            "pending_update": a.pending_update, "senses": sorted(a.senses), "capabilities": _sorted(capabilities),
        }
    scenes = {}
    for s in household.scenes.values():
        actions, unresolved = _actions(s.actions, household)
        scenes[s.id] = {"name": s.name, "scene_type": s.scene_type, "actions": actions, "unresolved": unresolved}
    automations = {}
    for t in household.automations.values():
        actions, unresolved = _actions(t.actions, household)
        automations[t.id] = {
            "name": t.name, "is_enabled": t.is_enabled, "hidden_in_app": t.hidden_in_app,
            "trigger_type": t.trigger_type, "auto_off_s": t.auto_off_s, "action_set_ids": sorted(t.action_set_ids),
            "conditions": sorted(c.get("text", "") for c in t.conditions), "triggers": _triggers(t.triggers),
            "actions": actions, "unresolved": unresolved,
        }
    form = {
        "reader_version": reader_version(),
        "home": {"id": household.id, "name": household.name},
        "rooms": {r.id: {"name": r.name} for r in household.rooms.values()},
        "zones": {z.id: {"name": z.name, "room_ids": sorted(z.room_ids)} for z in household.zones.values()},
        "accessories": accessories,
        "service_groups": {g.id: {"name": g.name, "service_ids": sorted(g.service_ids),
                                  "accessory_ids": sorted(g.accessory_ids)} for g in household.service_groups.values()},
        "scenes": scenes,
        "automations": automations,
    }
    if household.unavailable:  # only when non-empty: the form of a complete store is unchanged
        form["unavailable"] = sorted(household.unavailable)
    return _json(form)


def codes(form: Dict[str, Any]) -> set:
    """Codes the tables do not name yet: unknown:<n> protocols and radios, hap:<code> and vendor:<uuid> types."""
    found = set()
    for a in (form or {}).get("accessories", {}).values():
        for value in (a.get("protocol"), a.get("radio")):
            if isinstance(value, str) and value.startswith("unknown:"):
                found.add(value)
        for service_type, chars in a.get("capabilities", []):
            for name in [service_type] + [c[0] for c in chars]:
                if isinstance(name, str) and name.startswith(("hap:", "vendor:")):
                    found.add(name)
    return found
