# HomeKit change tracker

A small, local, read-only tool for macOS that answers one question the Home app never does: **what changed in my
home's setup, and when?** Once a day it takes a safe copy of Apple Home's local store, describes every home in it
(rooms, zones, accessories, scenes, automations with their triggers, conditions and actions), compares each home
with the previous day's description, and appends the differences it can see to a change log (see
[What the comparison covers](#what-the-comparison-covers)).

Tuning home automations by trial and error is hard: a rule edited on the phone leaves no trace, an auto-off
silently overrides another rule, and a light-level trigger can loop with the lamps it drives. A daily, exact
record of the setup turns "something changed" into "this rule's window moved yesterday".

## What it never does
- **It never writes to HomeKit**, to `~/Library/HomeKit/` or to any accessory.
- **It never opens the live store.** It copies the store files twice, accepts the copy only if both copies are
  byte-identical and the source did not move meanwhile, and reads only a throwaway copy of that copy.
  `scripts/run_extraction.py` refuses any input under `~/Library/HomeKit/` and, for a SQLite file, reads a throwaway
  copy of the file you give it, never the file itself.
- **It never sends your data anywhere.** Everything it writes stays in `data/`, `logs/` and `exports/` (all
  git-ignored). The one path that leaves the machine is one you take yourself: see
  [The extraction bundle](#the-extraction-bundle).
  Notifications hold fixed codes and counts only, never a home, room, device or rule name.

Every write, delete and database open made by the tool's commands goes through one helper, `src/safe_io.py`; the
commands write no Python bytecode cache. The smoke test's fictional home is built by test fixtures inside a
temporary folder that is checked first. A test scans the code to keep it that way; that scan is a lint, not a
proof. See [SECURITY.md](SECURITY.md) for what is guarded.

Read [DISCLAIMER.md](DISCLAIMER.md) before use.

## Requirements
- macOS with the Home app set up, and Python 3.12 or later (standard library only; see `requirements.txt`).
- **Full Disk Access** for the Python binary that runs the tool (System Settings → Privacy & Security → Full Disk
  Access). Grant it to the real binary, not a symlink: if access is missing, a run fails with `E_FDA`. Run by hand
  (in a terminal), it also prints the interpreter path to add on stderr; the scheduled run logs the code only.

## Try it first on a fictional home
```bash
python3.12 scripts/smoke_test.py --fixture
```
It builds a made-up store in a temporary folder, reads it and runs one tracking pass, then prints counts. It never
touches your real store or this folder's `data/` and `logs/` (a test checks that).

## Use it on your home
```bash
python3.12 scripts/track_changes.py
```
The first run records a baseline for each home. Each later run appends the differences to
`data/equipment_ledger.jsonl` (one JSON line per change: entity, field, before, after, time) and one line per run
to `data/tracking/runs.jsonl`. `--rebaseline` makes the current descriptions the new baselines.

To read one home into a single normalised file (and optional Markdown summaries):
```bash
python3.12 scripts/run_extraction.py --input <a copy of the store file> --home "<home name>" --no-audit
```
The copy to pass is one the tool made: `python3.12 scripts/snapshot_homekit.py` (or the tracker) writes it to
`data/raw/snapshots/<stamp>/` (pass the store file inside that folder). An input under `~/Library/HomeKit/` is refused.

### The extraction bundle
Without `--no-audit`, `run_extraction.py` writes a bundle meant to be pasted into a third-party LLM (Claude, Gemini,
ChatGPT): six Markdown files (rooms, accessories with makers, models, firmware and Matter ids, scenes, automations
with their trigger thresholds and condition offsets, hygiene notes, and a ready-made prompt) plus
`household_normalized.json`. Before anything is written, the value of every Serial Number characteristic is dropped,
and so is any field whose key names a secret (serial, password, passphrase, psk, token, key, secret, ssid, mac, wifi,
location, latitude, longitude, matched case-insensitively, at any depth); the JSON file also loses floor-plan fields,
metadata, Shortcuts payloads, serial numbers and bridge identifiers. A secret kept under a key with another name, or
inside a free-text value, is not detected: read the bundle before you share it. The tool sends nothing: the bundle leaves your machine only if you upload it. It describes
your home, so treat it like the rest of `data/`. It is written to `exports/<home name as a slug>_audit/` (or
`--output`), owner-only (an existing folder you own is set to 0700, the files to 0600; a destination inside the live
HomeKit folder is refused). `--no-audit` writes the JSON file alone.

## What the comparison covers
The tracker compares the daily descriptions of each home: rooms, zones, accessories (name, room, maker, model,
firmware, protocol, radio, capabilities), service groups, scenes and automations (name, state, triggers, conditions,
auto-off, linked scenes, actions). It does **not** compare:
- the fire time of a timer automation, or the offset of a sunrise/sunset trigger (offsets inside a condition are compared);
- the value of a non-numeric constant in a condition (it renders as `<value>`);
- the targets of an action that matches no accessory of the home (only their count);
- Shortcuts payloads (stripped).

It compares successful daily samples: an edit made and undone between two runs is not seen.

### Run it every day
Fill in `launchd/track-changes.plist.template` (`{{PROJECT_DIR}}`, `{{PYTHON}}`), save it under
`~/Library/LaunchAgents/`, and load it with the `launchctl bootstrap` line written in the template. It runs at
12:00; change `StartCalendarInterval` to taste. The scheduled command is Python alone (`--log-dir logs`): the
tracker creates `logs/` and appends to `logs/track_changes.<date>.log` itself, through the same guarded helper.

### Notifications
By default a run prints its message on one line (captured in the day's log). To be notified another way, add a
file `scripts/local_notifier.py` with a function `notifier()` that returns a callable `send(text) -> bool`; the
tracker uses it when present. Start from `scripts/local_notifier.example.py`. `scripts/local_notifier.py` is
git-ignored, because it usually holds an address or a token (the ignore line does not protect a file you have
already committed). The adapter is Python run inside the tracker's process, so it has the tracker's Full Disk
Access: write or review it as you would the tracker itself.

## Error codes
| Code | Meaning |
|---|---|
| `E_FDA` | The store cannot be read: grant Full Disk Access to the interpreter (printed on stderr in a terminal run). |
| `E_TORN` | The store kept changing during three copy attempts; the next run retries. |
| `E_INTEGRITY` / `E_EMPTY` | The copy is corrupt, or holds no home. |
| `E_DUP_HOME_NAME` / `E_NO_HOME_NAME` | Two homes share a name (case-insensitive), or a home has none; that home is skipped. |
| `E_READER` | The reader failed on a home (the store's layout may have changed). |
| `E_LOCKED` | Another run was in progress. |
| `E_OTHER` | Any other failure; only the exception type is printed. |

## Tests
```bash
python3.12 -m unittest discover -t . -s tests
```
All tests run on fictional data built on the fly.

## Licence
MIT; see [LICENSE](LICENSE).
