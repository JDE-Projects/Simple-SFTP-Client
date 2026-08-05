#!/usr/bin/env python3
"""What the README screenshot shows: an invented, active SFTP session between
a deploy workstation and an internal app server.

None of this is real. No real host, IP, username or key ever appears here.
The version shown in the image always comes from simple_sftp_client.py, never
from here, so this fixture holds no version number.
"""

from datetime import datetime, timezone


def _ts(y, mo, d, h, mi):
    """Fixed UTC timestamp (unix seconds) so the fixture, and the picture it
    produces, never drifts just because time passed since it was written."""
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


HOST = "sftp-app01.internal"
PORT = "22"
USERNAME = "rpatel"
START_PATH = "/srv/releases/storefront/current"

LOCAL_CWD = "D:\\Deploy\\storefront\\releases"
REMOTE_CWD = "/srv/releases/storefront/current"

LATENCY_MS = 38

# name, is_dir, size (bytes, None for dirs), mtime
LOCAL_ENTRIES = [
    dict(name="build-output", is_dir=True, size=None, mtime=_ts(2026, 8, 4, 9, 10)),
    dict(name="releases", is_dir=True, size=None, mtime=_ts(2026, 8, 3, 16, 45)),
    dict(name="app.conf", is_dir=False, size=2380, mtime=_ts(2026, 8, 4, 8, 55)),
    dict(name="deploy.log", is_dir=False, size=184_320, mtime=_ts(2026, 8, 5, 7, 12)),
    dict(name="notes.txt", is_dir=False, size=1_140, mtime=_ts(2026, 7, 29, 11, 3)),
]

REMOTE_ENTRIES = [
    dict(name="shared", is_dir=True, size=None, mtime=_ts(2026, 7, 20, 10, 0)),
    dict(name="releases", is_dir=True, size=None, mtime=_ts(2026, 8, 3, 16, 50)),
    dict(name="app.conf", is_dir=False, size=2380, mtime=_ts(2026, 8, 4, 8, 55)),
    dict(name="deploy.log", is_dir=False, size=201_728, mtime=_ts(2026, 8, 5, 7, 15)),
    dict(name="backup-2026-08-01.tar.gz", is_dir=False, size=48_302_112,
         mtime=_ts(2026, 8, 1, 2, 30)),
]

SELECTED_LOCAL = "deploy.log"
SELECTED_REMOTE = "deploy.log"

CONSOLE_LINES = [
    ("Connected to rpatel@sftp-app01.internal", "ok"),
    ("Compared: 1 newer here, 0 newer remote, 2 local-only, 0 remote-only", None),
    ("Transfer complete: 3 file(s)", "ok"),
]
