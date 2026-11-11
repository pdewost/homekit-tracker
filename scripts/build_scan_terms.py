#!/usr/bin/env python3.12
"""Build the private terms file a release scan needs, from this installation's own descriptions. Local only.

It reads the newest description of every home under <data>/tracking/ and writes one term per line: home, room,
zone, accessory, manufacturer, model, Matter vendor and product id, service-group, scene and automation names, each with and without accents;
single words are marked case-sensitive ("cs:") so ordinary vocabulary does not cry wolf. Lines from --extra (person
or network names typed by the owner) are added the same way. Terms containing "#" are skipped (the scanner treats
"#" as a comment). Service names are not collected: the tracking form keeps service types only, so a name a user
gave to a service is not in the terms (add it with --extra if it must be scanned for). The file is written with mode 0600 and only counts are printed: never a name.
Usage: python3.12 scripts/build_scan_terms.py [--data DIR] [--out FILE] [--extra FILE]
"""

from __future__ import annotations
import argparse
import json
import os
import re
import stat
import sys
sys.dont_write_bytecode = True  # no .pyc beside the code: a cache folder could be a link elsewhere
import unicodedata
from pathlib import Path
from typing import Iterable, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import safe_io  # noqa: E402
from src.store_files import LiveStoreError, inside_live_root  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}Z\.json$")
NAMED = {"rooms": ("name",), "zones": ("name",), "service_groups": ("name",), "scenes": ("name",),
         "automations": ("name",), "accessories": ("name", "manufacturer", "model")}


def _newest_forms(data: Path) -> List[dict]:
    forms = []
    for home_dir in sorted(p for p in (data / "tracking").iterdir() if p.is_dir()):
        files = sorted(p for p in home_dir.iterdir() if STAMP_RE.match(p.name))
        if files:
            forms.append(json.loads(files[-1].read_text(encoding="utf-8"))["form"])
    return forms


def _names(form: dict) -> Iterable[str]:
    yield form.get("home", {}).get("name") or ""
    for key, fields in NAMED.items():
        for entity in form.get(key, {}).values():
            for f in fields:
                yield entity.get(f) or ""
    for acc in form.get("accessories", {}).values():  # Matter vendor and product ids, as plain decimal strings
        for f in ("matter_vendor_id", "matter_product_id"):
            if acc.get(f) is not None:
                yield str(acc[f])


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def terms(names: Iterable[str]) -> Tuple[List[str], int]:
    """Return (sorted term lines, skipped count). One line per spelling; single words get the cs: prefix."""
    out: Set[str] = set()
    skipped = 0
    for raw in names:
        name = " ".join(raw.split())
        if len(name) < 3:
            continue
        if "#" in name:
            skipped += 1
            continue
        for spelling in {name, _plain(name)}:
            out.add(("cs:" if " " not in spelling else "") + spelling)
    return sorted(out), skipped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "release" / "publish_scan_terms")
    parser.add_argument("--extra", type=Path, help="a file of extra names, one per line (persons, networks)")
    args = parser.parse_args(argv)
    forms = _newest_forms(args.data)
    names = [n for f in forms for n in _names(f)]
    if args.extra:
        names += args.extra.read_text(encoding="utf-8").splitlines()
    lines, skipped = terms(names)
    if inside_live_root(args.out):
        print("build_scan_terms: refusing an --out inside the live HomeKit store", file=sys.stderr)
        return 2
    safe_io.mkdir(args.out.parent, 0o777, parents=True, exist_ok=True)
    try:
        st = os.lstat(args.out)
    except FileNotFoundError:
        st = None
    if st is not None and not (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()):
        print("build_scan_terms: --out exists and is not a regular file owned by you", file=sys.stderr)
        return 2
    try:  # the mode is set before the first byte (an existing file keeps no older mode); a hard link is refused
        safe_io.write_text(args.out, "\n".join(lines) + "\n")
    except LiveStoreError as exc:
        print(f"build_scan_terms: {exc}", file=sys.stderr)
        return 2
    print(f"build_scan_terms: {len(forms)} home(s), {len(lines)} term line(s), {skipped} skipped (contain #)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
