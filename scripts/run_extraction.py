#!/usr/bin/env python3.12
"""CLI tool to ingest HomeKit exports and generate LLM audit bundles."""

import argparse
import importlib.util
import os
import re
import sys
sys.dont_write_bytecode = True  # no .pyc beside the code: a cache folder could be a link elsewhere
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.extractor.app_parser import parse_app_export, detect_export_type
from src.extractor.sqlite_parser import parse_homed_sqlite
from src.audit.bundle_generator import generate_llm_audit_bundle
from src.store_files import DB


def _snapshot_module():
    """scripts/snapshot_homekit.py as a module (scripts/ is not a package), loaded once."""
    if "snapshot_homekit_module" not in sys.modules:
        spec = importlib.util.spec_from_file_location("snapshot_homekit_module",
                                                      Path(__file__).resolve().parent / "snapshot_homekit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["snapshot_homekit_module"] = module
        spec.loader.exec_module(module)
    return sys.modules["snapshot_homekit_module"]


HOMEKIT_ROOT = _snapshot_module().SOURCE  # the live store: never opened, never read


def _under_live_store(path: Path) -> bool:
    root = os.path.realpath(HOMEKIT_ROOT)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _slug(name: str) -> str:
    return re.sub(r"[^\w-]", "_", name).lower() or "home"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest HomeKit data and generate LLM audit bundle.")
    parser.add_argument(
        "--input", "-i",
        required=True,
        help=f"Path to HomeKit export file (JSON from Controller/Catalyst, CSV from HomeCare, or {DB})"
    )
    parser.add_argument(
        "--home",
        default=None,
        help="Target household name (if export contains multiple homes)"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output directory for generated LLM audit bundle (defaults to exports/<home_name>_audit/)"
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="Skip the markdown audit files; write only the stripped household_normalized.json"
    )

    args = parser.parse_args()
    input_path = Path(args.input).expanduser()

    if not input_path.exists():
        print(f"Error: Input file '{input_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if _under_live_store(input_path):
        print("Error: refusing to read the live HomeKit store. Take a safe copy first "
              "(python3.12 scripts/snapshot_homekit.py, which writes data/raw/snapshots/<stamp>/) and pass that.",
              file=sys.stderr)
        sys.exit(1)

    print(f"[*] Processing HomeKit input: {input_path}")

    # Determine parser
    if input_path.name.lower() in (DB, DB + "3") or input_path.suffix.lower() == Path(DB).suffix:
        print("[*] Detected direct Apple homed SQLite database.")
        snap = _snapshot_module()
        scratch = None
        try:  # the given file is only copied; SQLite reads a throwaway copy, removed afterwards
            scratch = snap.copy_to_scratch(input_path, snap.SCRATCH)
            household = parse_homed_sqlite(scratch / input_path.name, household_name=args.home)
        except Exception as e:
            print(f"Error reading SQLite database: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            if scratch is not None:
                snap.remove_scratch(scratch, snap.SCRATCH)
    else:
        fmt = detect_export_type(input_path)
        print(f"[*] Detected format: {fmt}")
        try:
            household = parse_app_export(input_path, household_name=args.home)
        except Exception as e:
            print(f"Error parsing export file: {e}", file=sys.stderr)
            sys.exit(1)

    # Print summary
    print(f"\n[+] Household Loaded: '{household.name}'")
    print(f"    - Rooms: {len(household.rooms)}")
    print(f"    - Accessories: {len(household.accessories)}")
    print(f"    - Scenes: {len(household.scenes)}")
    print(f"    - Automations: {len(household.automations)}")
    print(f"    - Unresolved action references: {household.unresolved_action_refs()} (a non-zero count should be explained before the bundle is shared)")

    # Generate audit bundle
    if args.output:
        out_dir = Path(args.output)
    else:
        exports = (PROJECT_ROOT / "exports").resolve()
        out_dir = (exports / f"{_slug(household.name)}_audit").resolve()
        if exports not in out_dir.parents:
            print("Error: the export folder would fall outside exports/.", file=sys.stderr)
            sys.exit(1)
    if _under_live_store(out_dir):
        print("Error: refusing to write the bundle inside the live HomeKit store.", file=sys.stderr)
        sys.exit(1)
    _snapshot_module()._mkdir_private(out_dir)  # new folders are 0700

    if args.no_audit:
        bundle_path = generate_llm_audit_bundle(household, out_dir, include_markdown=False)
        print(f"[+] Wrote household_normalized.json (stripped: no floor-plan, metadata or Shortcuts fields) to: {bundle_path / 'household_normalized.json'}")
    else:
        bundle_path = generate_llm_audit_bundle(household, out_dir)
        print(f"\n[+] Successfully generated LLM Audit Bundle at:")
        print(f"    {bundle_path.resolve()}/")
        print("    Files generated:")
        print("      - 00_README.md")
        print("      - 01_devices_and_rooms.md")
        print("      - 02_scenes_matrix.md")
        print("      - 03_automations_analysis.md")
        print("      - 04_hygiene_and_conflicts.md")
        print("      - 05_llm_audit_prompt.md (Copy-paste prompt for LLM)")
        print("      - household_normalized.json (stripped: no floor-plan, metadata or Shortcuts fields)")


if __name__ == "__main__":
    main()
