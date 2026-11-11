# Security and privacy

## Reporting a problem
To report a vulnerability or a privacy leak (for example, a code path that could write to HomeKit, open the live
store, or put a home, room, device or rule name into a log or notification), use GitHub's private vulnerability
reporting on this repository rather than a public issue.

## Scope
This is a same-user tool: it runs as you, on your Mac. It must never write into, delete in, or open with SQLite the
live HomeKit folder, however a path is spelled: with `..`, through a symlink or a hard link, in another letter case,
or as a folder that contains it. Those cases are guarded and tested. Out of scope: another program running as you
that rewrites your folders between a check and a write. The code scan that backs this is a lint, not a proof.

## What never to attach
Never attach, paste or upload a copy of the store, a description, the change log, a run log, an extraction bundle
or a screenshot of the Home app, to an issue or anywhere else. (Uploading your own extraction bundle to an LLM for
an audit is your choice and your data; the project itself never sends it.) If a bug needs data to reproduce, describe it with the fictional home in
`tests/fixtures/build_fake_home.py`.
