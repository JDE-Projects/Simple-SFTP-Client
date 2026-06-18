# Simple SFTP Client

A clean, dual-pane SFTP client: browse local and remote side by side, transfer
with a background queue, save sessions, compare and sync folders, and watch a
local folder for auto-upload. Secure connections only.

Built by [JDE-Projects](https://github.com/JDE-Projects).

## Highlights
- Dual-pane browser with breadcrumb paths, back/forward, recent locations, and
  an instant per-pane filter.
- Quick connect plus saved sessions (host, port, user, key path, start path;
  never a password).
- Key authentication, with a built-in generator for Ed25519 (default) or
  RSA-4096 key pairs.
- Background transfer queue with progress, ETA, resume, and auto-retry.
- Compare local vs remote, folder sync, and download-changed-only.
- Upload watcher: keep a remote folder up to date from a local one.
- Remote directory size calculation (on demand) and a connection health
  indicator.
- Built-in check for updates against GitHub Releases.
- Optional debug log, off by default, with credentials redacted.
- Secure transport only: weak or vulnerable algorithms are disabled, so the
  app connects securely or fails with a clear message (no unsafe fallback).

## How it works
- Backend: paramiko over SSH/SFTP.
- Saved sessions: `servers.json` next to the app (no passwords).
- Window: pywebview on the Qt backend, UI in `simple_sftp_client-UI.html`.

## Download and run
Grab the latest release zip from the [Releases](../../releases) page, extract
it, and run `Simple SFTP Client.exe` from inside the extracted folder. Keep the
folder together; the exe needs the files next to it. No Python or setup
required. Windows only.
Unsigned, so SmartScreen may warn the first time: More info > Run anyway.

## Verify this download (optional)
This release was built on GitHub from this public source - not on a personal
machine - and is signed with a build-provenance attestation. To confirm a
download is genuine, install the [GitHub CLI](https://cli.github.com) and run:

```
gh attestation verify SimpleSFTPClient-vX.Y.Z.zip \
  --repo JDE-Projects/Simple-SFTP-Client \
  --signer-repo JDE-Projects/Build-Tools
```

A `Verification succeeded!` line means the file was built by the published
pipeline from this repo. You can also check the file against the published
`.sha256`.

## Build from source (optional)
- Python 3 on PATH.
- `pip install -r requirements.txt` (pinned versions: PySide6, pywebview,
  paramiko, cryptography, keyring, and PyInstaller)
- Keep `simple_sftp_client.py`, `simple_sftp_client-UI.html`, the `fonts/`
  folder, the `.ico`, `.png`, and `-splash.png` together.
- Run from source: `python simple_sftp_client.py`
- Build the .exe: `Build_Simple_SFTP_Client.bat` -> `dist\Simple SFTP Client\Simple SFTP Client.exe`

## Using it
1. Enter host, port, and username, then a password or a private key. Connect.
   Verify the server fingerprint on first connection.
2. Save the connection for one-click reconnect (optionally a start path).
3. Browse the panes; transfer with the center arrows or drag-and-drop.
4. Use Compare / Sync to reconcile folders, or the watcher to auto-upload
   local changes.
5. Generate an Ed25519 or RSA key pair from the connection panel if you want
   to switch a host to key auth.

## Security and privacy
- Passwords and key passphrases live in memory only and are wiped on
  disconnect. `servers.json` never stores passwords.
- "Remember password" is opt-in per session and stores the password in the
  Windows Credential Manager (via `keyring`), not in any file.
- Only modern, secure key-exchange, ciphers, and MACs are offered; known-weak
  algorithms are disabled. There is no "compatibility" downgrade.
- Deleting a remote file or folder is permanent and cannot be undone; the app
  confirms first.
- The optional debug log is off by default; when on it writes
  `Debug_Log_MMDDYYYY_HHMMSS.txt` next to the app with credentials redacted.

## Updates
Use Check for Updates to compare your version against the latest GitHub
Release. If a newer version exists, the app shows what's new and links to the
Releases page to download it. The check is silent if you're offline.

## A note on how this was built
This project was built with AI assistance. The design decisions, feature
direction, and real-world testing were directed by me. The code was written
and revised with an AI assistant against that direction.

## License
Released under the PolyForm Noncommercial License 1.0.0 (see
[LICENSE](LICENSE)). Personal and any noncommercial use, modification, and
noncommercial redistribution are permitted; commercial use is not. Keep the
copyright notice; no warranty. This tool bundles third-party code; see
[THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt).

For commercial licensing, open a [GitHub issue](https://github.com/JDE-Projects/Simple-SFTP-Client/issues) with the title "Commercial License Inquiry".
