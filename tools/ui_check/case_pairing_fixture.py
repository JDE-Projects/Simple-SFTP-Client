"""ui_drive fixture for the "case-pairing" scenario.

The local folder holds report.txt (newer, larger) and Docs/new.txt. The
Shared server folder holds Report.txt and docs/other.txt, so each local name
matches a server name only when capitals are ignored. The server refuses any
write or folder creation under the local spellings, so a sync that ignored the
server's existing spelling fails the scenario instead of passing silently on
this case-insensitive disk.
"""
from __future__ import annotations

import json
import os
import posixpath
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import paramiko  # noqa: E402

from tools import sftp_server_core  # noqa: E402
from tools.sftp_server_core import PASSWORD, USER  # noqa: E402

SHARED_FOLDER = "Shared"
WRONG_SPELLINGS = ("/Shared/report.txt", "/Shared/Docs")
OLD_TIME = 1_700_000_000
NEW_TIME = 1_700_003_600


def _write(path, text, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.utime(path, (mtime, mtime))


def main() -> int:
    out_dir = os.environ.get("UI_DRIVE_OUT_DIR")
    if not out_dir:
        print("UI_DRIVE_OUT_DIR is not set", file=sys.stderr)
        return 2

    server_root = os.path.join(out_dir, "fixture_server")
    local_dir = os.path.join(out_dir, "fixture_local")
    shared_dir = os.path.join(server_root, SHARED_FOLDER)
    _write(os.path.join(shared_dir, "Report.txt"), "old", OLD_TIME)
    _write(os.path.join(shared_dir, "docs", "other.txt"), "other", OLD_TIME)
    _write(os.path.join(local_dir, "report.txt"), "newer local report", NEW_TIME)
    _write(os.path.join(local_dir, "Docs", "new.txt"), "new", NEW_TIME)

    base = sftp_server_core.make_fs(server_root)

    class CaseFS(base):
        @staticmethod
        def _wrong(path):
            clean = posixpath.normpath(path if path.startswith("/") else "/" + path)
            return any(clean == bad or clean.startswith(bad + "/") for bad in WRONG_SPELLINGS)

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR) and self._wrong(path):
                return paramiko.SFTP_PERMISSION_DENIED
            return super().open(path, flags, attr)

        def mkdir(self, path, attr):
            if self._wrong(path):
                return paramiko.SFTP_PERMISSION_DENIED
            return super().mkdir(path, attr)

        def rename(self, oldpath, newpath):
            if self._wrong(newpath):
                return paramiko.SFTP_PERMISSION_DENIED
            return super().rename(oldpath, newpath)

        def posix_rename(self, oldpath, newpath):
            if self._wrong(newpath):
                return paramiko.SFTP_PERMISSION_DENIED
            return super().posix_rename(oldpath, newpath)

    _sock, port = sftp_server_core.start(CaseFS, paramiko.RSAKey.generate(2048))
    print(json.dumps({"port": port, "user": USER, "password": PASSWORD,
                      "local_dir": local_dir, "shared_folder": SHARED_FOLDER}), flush=True)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
