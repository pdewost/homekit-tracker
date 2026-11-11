"""Daily change tracking of the HomeKit store.

One run: lock, safe copy, validate on a scratch copy, list homes by id, describe each describable home, compare it
with its previous description, append the changes to data/equipment_ledger.jsonl (first, fsynced, deduplicated),
then publish the description; one line in data/tracking/runs.jsonl; retention. Never writes to HomeKit.

Only the standard library is imported at module level; the project's modules are imported inside run(), so a
failing import is caught by main() like any other error and only a fixed code reaches the log.
"""

from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
sys.dont_write_bytecode = True  # no .pyc beside the code: a cache folder could be a link elsewhere
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data"
LOGS = PROJECT_ROOT / "logs"
SOURCE = Path.home() / "Library" / "HomeKit"
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}Z$")
LOG_RE = re.compile(r"^track_changes\.(\d{4}-\d{2}-\d{2})\.log$")


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")


def _parse_stamp(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name[:18], "%Y-%m-%d_%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _append_lines(path: Path, records: List[Dict[str, Any]]) -> None:
    """Append-only, fsynced, created 0600."""
    if not records:
        return
    with _io().open_text(path, "a", root=_snapshot_module().SOURCE) as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _write_atomic(path: Path, text: str) -> None:
    safe_io, root = _io(), _snapshot_module().SOURCE
    safe_io.mkdir(path.parent, 0o777, parents=True, exist_ok=True, root=root)
    tmp = path.with_name(path.name + ".tmp")
    with safe_io.open_text(tmp, "w", root=root, perm=0o666) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    safe_io.replace(tmp, path, root=root)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _dedupe_key(entry: Dict[str, Any]) -> str:
    payload = json.dumps([entry.get("after"), entry.get("added"), entry.get("removed")], sort_keys=True, default=str)
    return json.dumps([entry.get("home_id"), entry.get("baseline"), entry.get("entity"), entry.get("id"),
                       entry.get("field"), entry.get("kind"), hashlib.sha256(payload.encode()).hexdigest()])


def _descriptions(home_dir: Path) -> List[Path]:
    if not home_dir.exists():
        return []
    return sorted(p for p in home_dir.iterdir() if p.suffix == ".json" and STAMP_RE.match(p.stem))


def run(source: Path = SOURCE, data: Path = DATA, logs: Path = LOGS, now: Optional[datetime] = None,
        rebaseline: bool = False, notifier: Optional[Callable[[str], bool]] = None,
        _crash_after_ledger: bool = False) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    data, logs = Path(data), Path(logs)
    guard = _snapshot_module()
    for folder in (data, data / "tracking", logs):  # before anything is created: nothing of ours goes into the live store
        guard.refuse_live_store(folder)
    safe_io, root = _io(), guard.SOURCE
    safe_io.mkdir(data, 0o777, parents=True, exist_ok=True, root=root)
    safe_io.chmod(data, 0o700, root=root)
    tracking = data / "tracking"
    safe_io.mkdir(tracking, 0o777, parents=True, exist_ok=True, root=root)
    runs_path, ledger_path = tracking / "runs.jsonl", data / "equipment_ledger.jsonl"
    ts = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    summary: Dict[str, Any] = {"ts": ts, "snapshot": None, "status": "failed", "failure_codes": [],
                               "failed_homes": [], "homes": 0, "changes": 0, "changed_homes": 0, "baselines": 0,
                               "unresolved": 0, "fallback_ids": 0, "kinds": {}, "message": None, "notify": None}

    def finish() -> Dict[str, Any]:
        _notify(summary, tracking, now, notifier)
        _append_lines(runs_path, [summary])
        return summary

    lock = safe_io.open_lock(tracking / ".lock", root=root)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        summary.update(status="locked", failure_codes=["E_LOCKED"])
        _append_lines(runs_path, [summary])  # no message for a locked run
        return summary

    snapshot_homekit = _snapshot_module()
    scratch_paths: List[Path] = []
    try:
        from src.extractor.sqlite_parser import parse_homed_sqlite
        from src.tracking import canon, diff

        snapshots, scratch_root = data / "raw" / "snapshots", data / "raw" / "scratch"
        stamp = _stamp(now)
        while (snapshots / stamp).exists():  # two runs in one second: wait for the next stamp
            time.sleep(0.2)
            now = datetime.now(timezone.utc)
            stamp = _stamp(now)
        try:
            snap = snapshot_homekit.take_snapshot(Path(source), snapshots, stamp=stamp)
            summary["snapshot"] = snap.name
            scratch = snapshot_homekit.validate(snap, scratch_root)
            scratch_paths.append(scratch)
        except snapshot_homekit.SnapshotError as exc:
            summary["failure_codes"] = [exc.code]
            finish()
            retention(data, logs, now)  # repeated failures must not pile up copies
            return summary
        db = scratch / snapshot_homekit.DB

        seen_keys = {_dedupe_key(e) for e in _read_jsonl(ledger_path)}

        def log(entries: List[Dict[str, Any]], home_id: str, baseline: Optional[str]) -> int:
            fresh = []
            for e in entries:
                e = dict(e, ts=ts, snapshot=snap.name, baseline=baseline, home_id=home_id)
                key = _dedupe_key(e)
                if key not in seen_keys:
                    seen_keys.add(key)
                    fresh.append(e)
            _append_lines(ledger_path, fresh)
            for e in fresh:
                summary["kinds"][e["kind"]] = summary["kinds"].get(e["kind"], 0) + 1
            return len(fresh)

        # Homes by id: the store's whole list, compared with the previous run's
        homes = canon.list_homes(db)
        homes_path = tracking / "homes.json"
        previous_homes = json.loads(homes_path.read_text()) if homes_path.exists() else None
        if previous_homes is not None:
            for e in diff.diff_home_lists(previous_homes["homes"], homes):
                summary["changes"] += log([e], e["id"], previous_homes["snapshot"])
        _write_atomic(homes_path, json.dumps({"snapshot": snap.name, "homes": homes}, sort_keys=True))

        describable, failures = canon.classify_homes(homes)
        codes = [code for _, code in failures]
        summary["failed_homes"] = [[ordinal, code] for ordinal, code in failures]
        for home in describable:
            try:
                household = parse_homed_sqlite(db, household_name=home["name"])
            except Exception:  # never keep the text: reader errors name homes
                codes.append("E_READER")
                summary["failed_homes"].append([home["ordinal"], "E_READER"])
                continue
            form = canon.canonical(household)
            summary["fallback_ids"] += household.metadata.get("fallback_ids", 0)
            summary["unresolved"] += sum(x["unresolved"] for k in ("scenes", "automations") for x in form[k].values())
            home_dir = tracking / home["id"]
            earlier = _descriptions(home_dir)
            previous = json.loads(earlier[-1].read_text()) if earlier else None
            if previous is None:
                entries, baseline = diff.diff(None, form), None
            else:
                baseline = previous["snapshot"]
                seen = set()
                for p in earlier:
                    seen |= canon.codes(json.loads(p.read_text())["form"])
                prev_form = previous["form"]
                if rebaseline or prev_form.get("reader_version") != form["reader_version"]:
                    old_snap = snapshots / previous["snapshot"]
                    prev_form = None
                    if not rebaseline and old_snap.is_dir():
                        try:
                            old_scratch = snapshot_homekit.validate(old_snap, scratch_root / "reread")
                            scratch_paths.append(old_scratch)
                            prev_form = canon.canonical(parse_homed_sqlite(old_scratch / snapshot_homekit.DB,
                                                                           household_name=home["name"]))
                        except Exception:
                            prev_form = None
                    if prev_form is None:
                        entries = [diff._change("rebaseline", "home", home["id"], None, None, None)]
                    else:
                        entries = diff.diff(prev_form, form, seen)
                else:
                    entries = diff.diff(prev_form, form, seen)
            count = log(entries, home["id"], baseline)  # 1. the ledger, fsynced
            if _crash_after_ledger:
                raise RuntimeError("injected crash after the ledger append")
            _write_atomic(home_dir / f"{snap.name}.json",  # 2. then the description
                          json.dumps({"snapshot": snap.name, "form": form}, sort_keys=True))
            if previous is None or any(e["kind"] == "rebaseline" for e in entries):
                summary["baselines"] += count
            else:
                summary["changes"] += count
                summary["changed_homes"] += bool(count)
            summary["homes"] += 1

        summary["failure_codes"] = codes
        summary["status"] = "ok" if not codes else ("partial" if summary["homes"] else "failed")
        if summary["status"] == "ok":
            _write_atomic(tracking / "last_success", ts)
        finish()
        retention(data, logs, now)
        return summary
    finally:
        for path in scratch_paths:
            safe_io.rmtree(path, ignore_errors=True, root=root)
        safe_io.rmtree(data / "raw" / "scratch" / "reread", ignore_errors=True, root=root)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _failure_text(summary: Dict[str, Any]) -> str:
    """Codes, with homes as ordinals: 'E_DUP_HOME_NAME (home #2, home #3)'."""
    by_code: Dict[str, List[int]] = {}
    for ordinal, code in summary["failed_homes"]:
        by_code.setdefault(code, []).append(ordinal)
    parts = [f"{code} ({', '.join(f'home #{o}' for o in sorted(ords))})" for code, ords in sorted(by_code.items())]
    parts += [code for code in summary["failure_codes"] if code not in by_code]
    return "; ".join(dict.fromkeys(parts))


def _notify(summary: Dict[str, Any], tracking: Path, now: datetime, notifier: Optional[Callable[[str], bool]]) -> None:
    """At most one message: changes, baseline or failure, with the weekly heartbeat folded in."""
    state_path = tracking / "notify_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    parts, new_state = [], dict(state)
    if summary["changes"]:
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(summary["kinds"].items()) if k not in ("baseline", "rebaseline"))
        parts.append(f"{summary['changes']} change(s) in {summary['changed_homes']} home(s): {kinds}; "
                     "see data/equipment_ledger.jsonl")
    elif summary["baselines"]:
        parts.append(f"baseline recorded for {summary['baselines']} home(s)")
    failure = _failure_text(summary) if summary["status"] in ("failed", "partial") else ""
    if failure:
        last = state.get("failure_sent")
        repeat = state.get("failure") == failure and last and \
            now - datetime.fromisoformat(last) < timedelta(days=7)
        if not repeat:
            parts.append(f"tracking failed: {failure}")
            new_state.update(failure=failure, failure_sent=now.isoformat())
    else:
        new_state.pop("failure", None)
        new_state.pop("failure_sent", None)
    if summary["fallback_ids"]:
        parts.append(f"fallback ids: {summary['fallback_ids']}")
    beat = state.get("heartbeat_sent")
    if not beat or now - datetime.fromisoformat(beat) >= timedelta(days=7):
        week = [r for r in _read_jsonl(tracking / "runs.jsonl")
                if r.get("ts") and now - datetime.fromisoformat(r["ts"]) < timedelta(days=7)] + [summary]
        last_ok = (tracking / "last_success").read_text()[:10] if (tracking / "last_success").exists() else "never"
        parts.append(f"tracking alive: {len(week)} runs in 7 days, {sum(r.get('changes', 0) for r in week)} changes, "
                     f"last success {last_ok}")
        new_state["heartbeat_sent"] = now.isoformat()
    if not parts:
        return
    text = "HOME_AUTO — " + " · ".join(parts)
    summary["message"] = text
    ok = False
    if notifier is not None:
        try:
            ok = bool(notifier(text))
        except Exception:
            ok = False
    summary["notify"] = "sent" if ok else "failed"
    if ok:  # an unsent heartbeat or failure is retried by the next run
        _write_atomic(state_path, json.dumps(new_state, sort_keys=True))


def log_line(summary: Dict[str, Any]) -> str:
    """The one log line per run: codes and counts only."""
    return (f"{summary['ts']} {summary['status']} snapshot={summary['snapshot']} homes={summary['homes']} "
            f"changes={summary['changes']} baselines={summary['baselines']} "
            f"codes={_failure_text(summary) or '-'} fallback_ids={summary['fallback_ids']} "
            f"notify={summary['notify'] or '-'}")


def _old(path: Path, now: datetime, days: float) -> bool:
    return now.timestamp() - path.stat().st_mtime > days * 86400


def retention(data: Path, logs: Path, now: datetime) -> None:
    """Remove only this job's own items, by name pattern and age."""
    safe_io, root = _io(), _snapshot_module().SOURCE
    changed = {r.get("snapshot") for r in _read_jsonl(data / "tracking" / "runs.jsonl") if r.get("changes")}
    snapshots = data / "raw" / "snapshots"
    if snapshots.exists():
        for p in snapshots.iterdir():
            when = _parse_stamp(p.name)
            if not p.is_dir() or when is None:
                continue
            age = (now - when).days
            if p.name.endswith(".partial"):
                if age >= 1:
                    safe_io.rmtree(p, ignore_errors=True, root=root)
            elif STAMP_RE.match(p.name) and age >= (90 if p.name in changed else 14):
                safe_io.rmtree(p, ignore_errors=True, root=root)
    scratch = data / "raw" / "scratch"
    if scratch.exists():
        for p in scratch.iterdir():
            if p.is_dir() and (STAMP_RE.match(p.name) or p.name == "reread") and _old(p, now, 1):
                safe_io.rmtree(p, ignore_errors=True, root=root)
    tracking = data / "tracking"
    ledger = _read_jsonl(data / "equipment_ledger.jsonl")
    if tracking.exists():
        for home_dir in (p for p in tracking.iterdir() if p.is_dir()):
            for tmp in home_dir.glob("*.tmp"):
                if _old(tmp, now, 1):
                    safe_io.unlink(tmp, root=root)
            files = _descriptions(home_dir)
            wanted = {e.get("baseline") for e in ledger if e.get("home_id") == home_dir.name and e["kind"] != "baseline"}
            wanted |= {e.get("snapshot") for e in ledger if e.get("home_id") == home_dir.name and e["kind"] != "baseline"}
            for p in files[:-30]:
                when = _parse_stamp(p.stem)
                if p.stem in wanted and when and (now - when).days < 90:
                    continue
                safe_io.unlink(p, root=root)
    if logs.exists():
        for p in logs.iterdir():
            m = LOG_RE.match(p.name)
            if m:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (now - day).days >= 30:
                    safe_io.unlink(p, root=root)


def _io():
    """src/safe_io.py: every destination of this job goes through it (live root and symlinks refused)."""
    from src import safe_io
    return safe_io


def _snapshot_module():
    """scripts/snapshot_homekit.py as a module (scripts/ is not a package), loaded once."""
    if "snapshot_homekit_module" not in sys.modules:
        import importlib.util
        spec = importlib.util.spec_from_file_location("snapshot_homekit_module", Path(__file__).resolve().parent /
                                                      "snapshot_homekit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["snapshot_homekit_module"] = module
        spec.loader.exec_module(module)
    return sys.modules["snapshot_homekit_module"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Track changes in the HomeKit store (read-only).")
    parser.add_argument("--rebaseline", action="store_true", help="make this run's descriptions the new baselines")
    parser.add_argument("--notify-test", action="store_true", help="send one test message and exit")
    parser.add_argument("--log-dir", type=Path, help="append the output (stdout and stderr) to "
                        "DIR/track_changes.<date>.log, written by this program, instead of printing it")
    args = parser.parse_args(argv)
    if args.log_dir is None:
        return _run_main(args)
    try:  # the log is opened through safe_io before anything else runs
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        safe_io, root = _io(), _snapshot_module().SOURCE
        safe_io.mkdir(args.log_dir, 0o700, parents=True, exist_ok=True, root=root)
        log = safe_io.open_text(Path(args.log_dir) / f"track_changes.{datetime.now():%Y-%m-%d}.log", "a", root=root)
    except Exception as exc:  # fixed code only: the text of an error may name a path
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} E_LOGDIR {type(exc).__name__}",
              file=sys.stderr)
        return 1
    with log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        return _run_main(args)


def _run_main(args: argparse.Namespace) -> int:
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        notifier = _default_notifier()
        if args.notify_test:
            ok = notifier("HOME_AUTO — scheduled test")
            print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} notify-test {'sent' if ok else 'failed'}")
            return 0 if ok else 1
        summary = run(rebaseline=args.rebaseline, notifier=notifier)
    except Exception as exc:  # never print the text: errors may name homes
        code = exc.code if exc.__class__.__name__ == "SnapshotError" else "E_OTHER"
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {code} {type(exc).__name__}")
        return 1
    print(log_line(summary))
    if "E_FDA" in summary["failure_codes"] and sys.stdin.isatty():  # interactive only: the scheduled log keeps codes
        _snapshot_module().print_fda_hint()
    return 0 if summary["status"] in ("ok", "partial") else 1


def _default_notifier(scripts_dir: Optional[Path] = None) -> Callable[[str], bool]:
    """The notifier of a real run. A local, unpublished adapter `scripts/local_notifier.py`, when present, provides
    it through `notifier()` (its import errors are not hidden: main() turns them into a fixed code). Without the
    adapter, each message is printed on one line; messages hold codes and counts only."""
    path = (scripts_dir or Path(__file__).resolve().parent) / "local_notifier.py"
    if path.is_file():
        import importlib.util
        spec = importlib.util.spec_from_file_location("local_notifier", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.notifier()

    def notify(text: str) -> bool:
        print(f"NOTIFY {text}")
        return True
    return notify


if __name__ == "__main__":
    sys.exit(main())
