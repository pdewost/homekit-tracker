"""Generate LLM-ready audit bundles from a Household model."""

from __future__ import annotations
import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .. import safe_io
from ..extractor import hap_types
from ..models.schema import Household, Accessory, Room, Scene, Automation

# The cloud-bound set: exactly these files. Anything else in the folder is local-only.
BUNDLE_FILES = (
    "00_README.md",
    "01_devices_and_rooms.md",
    "02_scenes_matrix.md",
    "03_automations_analysis.md",
    "04_hygiene_and_conflicts.md",
    "05_llm_audit_prompt.md",
    "household_normalized.json",
)

# Kept out of household_normalized.json: floor-plan fields, household metadata, Shortcuts payloads, serial numbers
# and bridge identifiers.
STRIPPED_KEYS = frozenset({"plan_file", "polygon_coords", "area_m2", "coordinates_on_plan", "metadata", "shortcut_details",
                           "serial_number", "bridge_id"})


# A key naming a secret-ish thing is dropped, at any depth, whatever its case or spelling (camelCase, snake_case):
# the key is split into words and any word below (or the words "wi" + "fi" together) removes the entry.
SECRET_WORDS = frozenset({"serial", "serialnumber", "password", "passphrase", "psk", "token", "key", "secret", "ssid",
                          "mac", "wifi", "location", "latitude", "longitude"})
SERIAL_TYPE_CODE = 0x30  # Apple's public Serial Number characteristic type, 00000030-0000-1000-8000-0026BB765291


def _secret_key(key: Any) -> bool:
    text = str(key)
    words = [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", text)]
    joined = "".join(words)
    return (any(w in SECRET_WORDS for w in words) or joined in SECRET_WORDS
            or any(w in joined for w in ("wifi", "serialnumber", "passphrase", "password")))


def _is_serial(*names: Any) -> bool:
    for name in names:
        text = str(name or "")
        if hap_types.hap_code(text) == SERIAL_TYPE_CODE:
            return True
        if re.sub(r"[^a-z]", "", text.lower()) in ("serialnumber", "serial"):
            return True
    return False


def _scrub(value: Any) -> Any:
    """Drop every secret-ish key, recursively."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _secret_key(k)}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _scrub_events(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for item in items:
        item = _scrub(item) if isinstance(item, dict) else item
        if isinstance(item, dict) and _is_serial(item.get("characteristic"), item.get("characteristic_type")):
            item = {k: v for k, v in item.items() if k not in ("value", "min", "max")}
        out.append(item)
    return out


def _sanitized(household: Household) -> Household:
    """A copy with serial-number values and secret-ish fields removed: every parser's output goes through here."""
    h = copy.deepcopy(household)
    for acc in h.accessories.values():
        for svc in acc.services:
            for c in svc.characteristics:
                if _is_serial(c.type, c.type_name, c.description):
                    c.value = None
    for auto in h.automations.values():
        auto.triggers = _scrub_events(auto.triggers)
        auto.conditions = [_scrub(c) for c in auto.conditions]
        for act in auto.actions:
            act.target_value = None if _is_serial(act.target_characteristic_type) else _scrub(act.target_value)
    for scene in h.scenes.values():
        for act in scene.actions:
            act.target_value = None if _is_serial(act.target_characteristic_type) else _scrub(act.target_value)
    return h


def _strip_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_keys(v) for k, v in value.items() if k not in STRIPPED_KEYS and not _secret_key(k)}
    if isinstance(value, list):
        return [_strip_keys(v) for v in value]
    return value


def generate_llm_audit_bundle(
    household: Household,
    output_dir: Union[str, Path],
    include_raw_json: bool = True,
    include_markdown: bool = True
) -> Path:
    """Generate the structured markdown and JSON bundle ready for external LLM audit.

    With include_markdown=False only the stripped household_normalized.json is written (run_extraction --no-audit).
    """
    household = _sanitized(household)
    out_path = Path(output_dir)
    safe_io.check(out_path)  # the folder, and anything in it, must not resolve into the live store
    if out_path.exists():  # an existing folder must be ours, and becomes owner-only
        if out_path.stat().st_uid != os.getuid():
            raise PermissionError("refusing to write the bundle into a folder owned by another user")
        safe_io.chmod(out_path, 0o700)
    else:
        safe_io.mkdir(out_path, 0o700, parents=True, exist_ok=True)
        safe_io.chmod(out_path, 0o700)
    for name in BUNDLE_FILES:  # a link in the folder is refused, not followed
        if os.path.islink(out_path / name):
            raise safe_io.SymlinkError("refusing a bundle folder that holds a symbolic link")
    for name in BUNDLE_FILES:  # no stale file of the set survives a run
        safe_io.unlink(out_path / name, missing_ok=True)

    if include_markdown:
        _write_markdown(household, out_path)

    # 7. household_normalized.json
    if include_raw_json:
        stripped = _strip_keys(household.to_dict())
        _write_private(out_path / "household_normalized.json", json.dumps(stripped, indent=2, default=str))

    return out_path


def _write_private(path: Path, text: str) -> None:
    """Write a bundle file owner-only (0600), whatever the umask or a previous mode."""
    safe_io.write_text(path, text)
    safe_io.chmod(path, 0o600)


def _write_markdown(household: Household, out_path: Path) -> None:
    # 1. 00_README.md
    readme_content = _build_readme(household)
    _write_private(out_path / "00_README.md", readme_content)

    # 2. 01_devices_and_rooms.md
    devices_content = _build_devices_markdown(household)
    _write_private(out_path / "01_devices_and_rooms.md", devices_content)

    # 3. 02_scenes_matrix.md
    scenes_content = _build_scenes_markdown(household)
    _write_private(out_path / "02_scenes_matrix.md", scenes_content)

    # 4. 03_automations_analysis.md
    automations_content = _build_automations_markdown(household)
    _write_private(out_path / "03_automations_analysis.md", automations_content)

    # 5. 04_hygiene_and_conflicts.md
    hygiene_content = _build_hygiene_markdown(household)
    _write_private(out_path / "04_hygiene_and_conflicts.md", hygiene_content)

    # 6. 05_llm_audit_prompt.md
    prompt_content = _build_llm_prompt(household)
    _write_private(out_path / "05_llm_audit_prompt.md", prompt_content)


def _build_readme(household: Household) -> str:
    total_accessories = len(household.accessories)
    total_rooms = len(household.rooms)
    total_scenes = len(household.scenes)
    total_automations = len(household.automations)
    total_shortcuts = sum(1 for a in household.automations.values() if a.is_shortcut)

    unavailable = (f"\nInformation not available in this store (reported as unknown): "
                   f"{', '.join(sorted(household.unavailable))}\n") if household.unavailable else ""
    return f"""# HomeKit Setup Audit Bundle — {household.name}

## Overview
- **Household**: {household.name}
- **Rooms**: {total_rooms}
- **Accessories / Devices**: {total_accessories}
- **Scenes**: {total_scenes}
- **Automations**: {total_automations} (including {total_shortcuts} converted to Shortcuts)
{unavailable}
## Bundle Structure
1. `01_devices_and_rooms.md`: Complete device inventory organized by room, with category, model, battery, and reachable state.
2. `02_scenes_matrix.md`: Scenes and their target states.
3. `03_automations_analysis.md`: Standard and Shortcuts-based automations with triggers, conditions, and actions.
4. `04_hygiene_and_conflicts.md`: Pre-computed rule-based flags (orphans, duplicates, offline accessories).
5. `05_llm_audit_prompt.md`: Recommended prompt to submit to an LLM (Claude, Gemini, or ChatGPT) alongside these files.
6. `household_normalized.json`: The machine-readable raw payload.

## Reading notes
- Protocol (HomeKit (HAP) / Matter) and Radio (Thread / Wi-Fi) are inferred from the store's codes, not documented by Apple; `unknown:<n>` marks a code not seen before.
- Type names follow the HomeKit Accessory Protocol; `vendor:<uuid>` is a manufacturer's own type, `hap:<code>` an Apple type this tool does not name.
- Values are decoded where the encoding is known (e.g. `True`, `70`); `hex:<bytes>` marks one that is not.
- Conditions show key paths, operators, numbers and time offsets only; any other constant is printed `<value>`.
"""


def _build_devices_markdown(household: Household) -> str:
    lines = [f"# Devices & Rooms Inventory — {household.name}\n"]

    # Group by room
    for room_id, room in sorted(household.rooms.items(), key=lambda x: x[1].name):
        accessories = household.get_accessories_by_room(room_id)
        lines.append(f"## Room: {room.name} ({len(accessories)} devices)")
        if not accessories:
            lines.append("_No accessories assigned to this room._\n")
            continue

        lines.append("| Name | Category | Manufacturer | Model | Firmware | Protocol | Radio | Senses | Battery | Reachable |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for acc in accessories:
            if acc.battery_level is not None:
                batt = f"{acc.battery_level}%"
            else:
                batt = "Yes" if acc.has_battery else "-"
            reach = "Yes" if acc.reachable else "**OFFLINE**"
            category = acc.category_name or acc.category
            senses = ", ".join(acc.senses) or "-"
            lines.append(f"| {acc.label} | {category} | {acc.manufacturer} | {acc.model} | {acc.firmware_version or '-'} | "
                         f"{acc.protocol} | {acc.radio} | {senses} | {batt} | {reach} |")
        lines.append("")
        for acc in accessories:
            capabilities = _capabilities(acc)
            if capabilities:
                lines.append(f"**{acc.label}** capabilities:")
                lines.extend(capabilities)
                lines.append("")

    # Orphan devices
    orphans = household.get_orphan_accessories()
    if orphans:
        lines.append(f"## ⚠️ Unassigned / Orphan Devices ({len(orphans)} devices)")
        lines.append("| Name | Category | Manufacturer | Model | Reachable |")
        lines.append("|---|---|---|---|---|")
        for acc in orphans:
            reach = "Yes" if acc.reachable else "**OFFLINE**"
            lines.append(f"| {acc.label} | {acc.category_name or acc.category} | {acc.manufacturer} | {acc.model} | {reach} |")
        lines.append("")

    if household.service_groups:
        lines.append("## Groups")
        for group in sorted(household.service_groups.values(), key=lambda g: g.name):
            members = [household.accessories[a].label for a in group.accessory_ids if a in household.accessories]
            lines.append(f"- **{group.name}**: {', '.join(members) or '(no member)'}")
        lines.append("")

    return "\n".join(lines)


def _capabilities(acc: Accessory) -> List[str]:
    """One line per service that does something: its type and its characteristics with range and unit."""
    lines = []
    for svc in acc.services:
        if svc.type_name in ("AccessoryInformation", "ProtocolInformation", "Pairing"):
            continue
        chars = []
        for c in svc.characteristics:
            label = c.type_name or c.type
            if c.max is not None:
                label += f" [{c.min}..{c.max}" + (f" step {c.step}" if c.step is not None else "") + \
                         (f" {c.unit}" if c.unit else "") + "]"
            chars.append(label)
        name = f" « {svc.name} »" if svc.name else ""
        lines.append(f"- {svc.type_name or svc.service_type}{name}: {', '.join(chars) or '-'}")
    return lines


def _build_scenes_markdown(household: Household) -> str:
    lines = [f"# Scenes Matrix — {household.name}\n"]
    if not household.scenes:
        lines.append("_No scenes defined in this household._\n")
        return "\n".join(lines)

    for sid, scene in sorted(household.scenes.items(), key=lambda x: x[1].name):
        lines.append(f"### Scene: `{scene.name}`")
        if not scene.actions:
            lines.append("_No direct actions recorded._\n")
            continue

        lines.append("| Accessory | Service | Characteristic | Target Value |")
        lines.append("|---|---|---|---|")
        for act in scene.actions:
            acc_name = act.accessory_name or household.accessories.get(act.target_accessory_id, Accessory(id="", name="Unknown")).label
            lines.append(f"| {acc_name} | {act.service_name or '-'} | {act.target_characteristic_type or '-'} | `{act.target_value}` |")
        lines.append("")

    return "\n".join(lines)


def _build_automations_markdown(household: Household) -> str:
    lines = [f"# Automations & Shortcuts Analysis — {household.name}\n"]
    if not household.automations:
        lines.append("_No automations found in this household._\n")
        return "\n".join(lines)

    visible = sorted((a for a in household.automations.values() if not a.hidden_in_app), key=lambda a: a.name)
    hidden = sorted((a for a in household.automations.values() if a.hidden_in_app), key=lambda a: a.name)
    for auto in visible + hidden:
        if auto is (hidden[0] if hidden else None):
            lines.append("## Hidden in the Home app (no trigger event: these rules cannot fire)\n")
        kind_tag = " [CONVERTED TO SHORTCUT]" if auto.is_shortcut else " [NATIVE HOMEKIT]"
        status_tag = "ACTIVE" if auto.is_enabled else "DISABLED"
        lines.append(f"### Automation: `{auto.name}` ({status_tag}){kind_tag}")
        lines.append(f"- **Trigger Type**: `{auto.trigger_type}`")

        if auto.triggers:
            lines.append("- **Triggers**:")
            for t in auto.triggers:
                lines.append(f"  - {_trigger_text(t)}")
        for c in auto.conditions:
            lines.append(f"- **Condition**: `{c.get('text', json.dumps(c, default=str))}`")
        if auto.auto_off_s is not None:
            lines.append(f"- **Auto-off: {auto.auto_off_s:g} s** after the trigger")
        if auto.action_set_ids:
            scene_names = [household.scenes.get(s, Scene(id="", name=s)).name for s in auto.action_set_ids]
            lines.append(f"- **Triggered Scenes**: {', '.join(scene_names)}")

        if auto.actions:
            lines.append("- **Direct Actions**:")
            for act in auto.actions:
                acc_name = act.accessory_name or household.accessories.get(act.target_accessory_id, Accessory(id="", name="Unknown")).label
                lines.append(f"  - Set `{acc_name}` -> `{act.target_characteristic_type}` to `{act.target_value}`")

        lines.append("")

    return "\n".join(lines)


def _trigger_text(t: Dict[str, Any]) -> str:
    if t.get("is_end_event") and "duration_s" in t:
        return f"end: after {t['duration_s']:g} s"
    text = t.get("kind", "event")
    if t.get("characteristic") or t.get("accessory"):
        text += f": {t.get('accessory') or '?'} → {t.get('characteristic') or '?'}"
    if "min" in t or "max" in t:
        text += f" in [{t.get('min')}..{t.get('max')}]"
    if "value" in t:
        text += f" = {t['value']}"
    if "duration_s" in t:
        text += f" for {t['duration_s']:g} s"
    return text


def _build_hygiene_markdown(household: Household) -> str:
    lines = [f"# Pre-Computed Hygiene & Anomaly Flags — {household.name}\n"]

    # Check 1: Orphans
    orphans = household.get_orphan_accessories()
    if orphans:
        lines.append(f"### 1. Unassigned Accessories ({len(orphans)})")
        lines.append("Accessories without a designated room:")
        for o in orphans:
            lines.append(f"- `{o.name}` ({o.category})")
        lines.append("")

    # Check 2: Offline devices
    offline = [a for a in household.accessories.values() if not a.reachable]
    if offline:
        lines.append(f"### 2. Offline Devices ({len(offline)})")
        lines.append("Devices currently flagged as unreachable:")
        for off in offline:
            lines.append(f"- `{off.name}` in `{off.room_name}`")
        lines.append("")

    # Check 3: Low battery
    low_batt = [a for a in household.accessories.values() if a.battery_level is not None and a.battery_level <= 20]
    if low_batt:
        lines.append(f"### 3. Low Battery Warnings ({len(low_batt)})")
        for lb in low_batt:
            lines.append(f"- `{lb.name}` in `{lb.room_name}`: **{lb.battery_level}%**")
        lines.append("")

    # Check 4: Disabled automations
    disabled_autos = [a for a in household.automations.values() if not a.is_enabled]
    if disabled_autos:
        lines.append(f"### 4. Disabled Automations ({len(disabled_autos)})")
        for da in disabled_autos:
            lines.append(f"- `{da.name}`")
        lines.append("")

    # Check 5: Unnamed accessories (no name in the store; shown as manufacturer + model)
    unnamed = [a for a in household.accessories.values() if a.unnamed]
    if unnamed:
        lines.append(f"### 5. Unnamed Accessories ({len(unnamed)})")
        for un in unnamed:
            lines.append(f"- `{un.label}` in `{un.room_name}`")
        lines.append("")

    # Check 6: Names equal to the home's name (Siri may confuse them)
    home = household.name.strip().lower()
    clashes = [g.name for g in household.service_groups.values() if g.name.strip().lower() == home]
    clashes += [a.label for a in household.accessories.values() if a.label.strip().lower() == home]
    if clashes:
        lines.append(f"### 6. Names Equal to the Home's Name `{household.name}` ({len(clashes)})")
        for name in clashes:
            lines.append(f"- `{name}`")
        lines.append("")

    # Check 7: Rules hidden in the Home app (no trigger event)
    hidden = [a for a in household.automations.values() if a.hidden_in_app]
    if hidden:
        lines.append(f"### 7. Hidden Rules ({len(hidden)})")
        lines.append("Stored and active, but with no trigger event: the Home app does not show them and they cannot fire.")
        for h in hidden:
            lines.append(f"- `{h.name}`")
        lines.append("")

    # Check 8: Thread and Matter devices depend on a home hub (how many hubs exist is not read)
    dependent = [a for a in household.accessories.values() if a.radio == "Thread" or a.protocol == "Matter"]
    if dependent:
        lines.append(f"### 8. Devices That Need a Home Hub ({len(dependent)})")
        lines.append("Thread devices need a Thread border router and Matter devices a home hub. The number of home hubs "
                     "is not in this bundle: check it; with one home hub, its outage stops all of these.")
        for d in sorted(dependent, key=lambda a: a.label):
            lines.append(f"- `{d.label}` ({d.protocol}, {d.radio})")
        lines.append("")

    if not any([orphans, offline, low_batt, disabled_autos, unnamed, clashes, hidden, dependent]):
        lines.append("✅ No obvious structural anomalies detected by pre-computation filters.")

    return "\n".join(lines)


def _build_llm_prompt(household: Household) -> str:
    return f"""# LLM Audit Prompt

Copy and paste the text below into your prompt along with the files from this audit bundle.

---

```text
You are an expert Home Automation Architect and Apple HomeKit specialist.
Attached is the exported HomeKit configuration for the household "{household.name}".
This bundle contains: Rooms, accessories, scenes, automations; service and characteristic states only where the source provides them.
Shortcuts logic is not in this bundle (by design), so dimension 3 covers only the automations' names and types.

Please review:
1. `01_devices_and_rooms.md` (Devices per room, naming conventions, connectivity)
2. `02_scenes_matrix.md` (Scenes and target states)
3. `03_automations_analysis.md` (Native and Shortcut automations)
4. `04_hygiene_and_conflicts.md` (Pre-computed anomalies)

Conduct a rigorous audit across the following 6 dimensions:

1. NAMING CONSISTENCY & SIRI ERGONOMICS:
   - Identify ambiguous, redundant, or confusing accessory and scene names (e.g., room names repeated inside accessory names, confusing homophones for voice control).

2. AUTOMATION CONFLICTS & RACE CONDITIONS:
   - Identify conflicting rules (e.g., one automation turning on a device while another turns it off within the same time window).
   - Flag motion sensor timeout conflicts with manual wall switches.

3. "NATIVE VS SHORTCUT" TRIAGE:
   - Review all automations converted to Shortcuts: can any be simplified and downgraded back to Native HomeKit automations for 0-latency hub reliability?
   - Identify native automations that fail or behave unreliably because they lack necessary preconditions (e.g. need an 'if light is off' condition or luminance threshold).

4. RELIABILITY & FAILURE MODES:
   - Analyze failure modes when a bridge or internet goes down. Which critical automations will fail?

5. PRIVACY & SECURITY:
   - Review automations touching security devices (locks, garage doors, cameras, presence geofences). Flag any insecure automatic triggers (e.g. unlocking doors without authentication).

6. PRIORITIZED ACTION PLAN:
   - Provide a table of recommendations:
     | Severity (High/Medium/Low) | Item / Automation | Issue Detected | Proposed Solution | Can be Safely Automated? |
```
"""
