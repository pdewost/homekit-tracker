# Disclaimer

- **Unofficial.** This project is not affiliated with, endorsed by or supported by Apple. "Apple", "Home" and
  "HomeKit" are Apple's trademarks.
- **Undocumented format.** It reads the local database the Home system keeps on a Mac. Apple does not document that
  database and may change it in any macOS update. When a table the reader requires is missing it fails with
  `E_READER` rather than guess; a missing optional table (for example the trigger events) is not fatal: what it would
  have described is reported as unknown.
- **Full Disk Access.** Reading the store needs Full Disk Access for the Python binary you run. That permission
  lets that binary read every file of your account; grant it only to an interpreter you trust and use for this.
- **Your outputs are personal data.** The change log and descriptions in `data/` describe your homes, rooms,
  devices and routines. The raw snapshots in `data/raw/snapshots/` are byte copies of the HomeKit store, which also
  holds Wi-Fi keys, passwords, serial numbers, MAC addresses and the home's location. Snapshots are kept 14 days
  (90 when a change was logged on that run), descriptions 30 runs (90 days when a logged change needs them), logs
  30 days. Keep them on your machine; never commit, upload or attach them (including to an issue), and keep this
  project out of synced or backed-up folders (iCloud Drive, Dropbox, Time Machine). The folders are created with
  owner-only permissions and are git-ignored.
- **The extraction bundle is for you to share, or not.** `scripts/run_extraction.py` writes, by default, a bundle
  made to be pasted into a third-party LLM. It holds names, rooms, makers, models and rules. The tool removes the value of every
  Serial Number characteristic and every field whose key names a serial, password, passphrase, psk, token, key,
  secret, ssid, mac, wifi or location; it cannot recognise a secret stored under another key or inside a free-text
  value, so read the bundle before you share it. The tool never sends it; if you upload it, the provider's terms apply. Use `--no-audit` to write only the
  JSON file.
- **Read-only by design, no warranty.** The tool never writes to HomeKit, but it is provided "as is", without
  warranty of any kind (see [LICENSE](LICENSE)).
