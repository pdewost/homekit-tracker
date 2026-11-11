"""Compare canonical descriptions.

A change is {kind, entity, id, field, before, after, added, removed}. List fields report counts in before/after and
the differing items in added/removed (ids and type names only). `name` is the only field carrying names.
"""

from __future__ import annotations
import json
from typing import Any, Dict, List, Optional

from .canon import codes

ENTITIES = {"rooms": "room", "zones": "zone", "accessories": "accessory", "service_groups": "service group",
            "scenes": "scene", "automations": "automation"}


def _key(item: Any) -> str:
    return json.dumps(item, sort_keys=True, default=str)


def _change(kind: str, entity: str, id_: Any, field: Optional[str], before: Any, after: Any,
            added: Optional[List[Any]] = None, removed: Optional[List[Any]] = None) -> Dict[str, Any]:
    return {"kind": kind, "entity": entity, "id": id_, "field": field, "before": before, "after": after,
            "added": added or [], "removed": removed or []}


def _list_delta(old: List[Any], new: List[Any]):
    """Multiset difference by JSON text: (added, removed)."""
    remaining = {}
    for item in old:
        remaining.setdefault(_key(item), []).append(item)
    added = []
    for item in new:
        bucket = remaining.get(_key(item))
        if bucket:
            bucket.pop()
        else:
            added.append(item)
    removed = [item for bucket in remaining.values() for item in bucket]
    return added, removed


EVENT_FIELDS = ("triggers", "auto_off_s", "hidden_in_app", "trigger_type")  # derived from the event table


def diff(previous: Optional[Dict[str, Any]], new: Dict[str, Any],
         seen_codes: Optional[set] = None) -> List[Dict[str, Any]]:
    """Changes from previous to new; a home with no previous description gives one baseline entry."""
    home_id = new["home"]["id"]
    if previous is None:
        return [_change("baseline", "home", home_id, None, None, None)]
    changes: List[Dict[str, Any]] = []
    if previous["home"]["name"] != new["home"]["name"]:
        changes.append(_change("changed", "home", home_id, "name", previous["home"]["name"], new["home"]["name"]))
    unknown = set(previous.get("unavailable", [])) | set(new.get("unavailable", []))  # not described: not compared
    for collection, entity in ENTITIES.items():
        if collection in unknown:
            continue
        old_items, new_items = previous.get(collection, {}), new.get(collection, {})
        for id_ in sorted(new_items.keys() - old_items.keys()):
            changes.append(_change("added", entity, id_, None, None, new_items[id_].get("name")))
        for id_ in sorted(old_items.keys() - new_items.keys()):
            changes.append(_change("removed", entity, id_, None, old_items[id_].get("name"), None))
        for id_ in sorted(old_items.keys() & new_items.keys()):
            old, cur = old_items[id_], new_items[id_]
            for field in sorted(old.keys() | cur.keys()):
                if field == "pending_update" and "pending_updates" in unknown:
                    continue
                if field in EVENT_FIELDS and "events" in unknown:  # trigger detail was not readable on one side
                    continue
                before, after = old.get(field), cur.get(field)
                if _key(before) == _key(after):
                    continue
                if isinstance(before, list) or isinstance(after, list):
                    added, removed = _list_delta(before or [], after or [])
                    changes.append(_change("changed", entity, id_, field, len(before or []), len(after or []),
                                           added, removed))
                else:
                    changes.append(_change("changed", entity, id_, field, before, after))
    known = codes(previous) if seen_codes is None else seen_codes
    for code in sorted(codes(new) - known):
        changes.append(_change("new_code", "code", code, None, None, None))
    return changes


def diff_home_lists(previous: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Home added / removed / renamed, by id, over the store's whole home list (skipped homes included)."""
    old = {h["id"]: h["name"] for h in previous}
    cur = {h["id"]: h["name"] for h in new}
    changes = [_change("added", "home", i, None, None, cur[i]) for i in sorted(cur.keys() - old.keys())]
    changes += [_change("removed", "home", i, None, old[i], None) for i in sorted(old.keys() - cur.keys())]
    changes += [_change("changed", "home", i, "name", old[i], cur[i])
                for i in sorted(old.keys() & cur.keys()) if old[i] != cur[i]]
    return changes
