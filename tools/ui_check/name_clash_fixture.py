"""ui_drive fixture for the "name-clash" scenario (tools/ui_check/ui_drive.json).

Starts the in-process test SFTP server (tools/sftp_server_core.py) on
127.0.0.1 serving a folder under UI_DRIVE_OUT_DIR. Windows cannot hold
Report.txt and report.txt in one folder, so the server's listing adds a
lowercase report.txt entry beside the real Report.txt, the way a Linux server
would show two distinct files. The app should download Report.txt and show
report.txt as a name clash.

Also puts notes.txt on the server and NOTES.txt in the local folder, so a
download of notes.txt must raise the overwrite prompt.

Stays running (does nothing) until drive.py tears down the job. The run folder
is deleted afterward by `drive.py cleanup`.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import paramiko  # noqa: E402

from tools import sftp_server_core  # noqa: E402
from tools.sftp_server_core import PASSWORD, USER  # noqa: E402

CLASH_FOLDER = "Docs"


def main() -> int:
    out_dir = os.environ.get("UI_DRIVE_OUT_DIR")
    if not out_dir:
        print("UI_DRIVE_OUT_DIR is not set", file=sys.stderr)
        return 2

    server_root = os.path.join(out_dir, "fixture_server")
    local_dir = os.path.join(out_dir, "fixture_local")
    os.makedirs(os.path.join(server_root, CLASH_FOLDER))
    os.makedirs(local_dir)
    with open(os.path.join(server_root, CLASH_FOLDER, "Report.txt"), "w", encoding="utf-8") as f:
        f.write("first")
    with open(os.path.join(server_root, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("remote notes")
    with open(os.path.join(local_dir, "NOTES.txt"), "w", encoding="utf-8") as f:
        f.write("local notes")

    base = sftp_server_core.make_fs(server_root)

    class ClashFS(base):
        def list_folder(self, path):
            out = super().list_folder(path)
            if isinstance(out, list):
                extra = []
                for attr in out:
                    if attr.filename == "Report.txt":
                        twin = copy.copy(attr)
                        twin.filename = "report.txt"
                        extra.append(twin)
                out = out + extra
            return out

    _sock, port = sftp_server_core.start(ClashFS, paramiko.RSAKey.generate(2048))

    print(
        json.dumps(
            {
                "port": port,
                "user": USER,
                "password": PASSWORD,
                "local_dir": local_dir,
                "clash_folder": CLASH_FOLDER,
            }
        ),
        flush=True,
    )

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
