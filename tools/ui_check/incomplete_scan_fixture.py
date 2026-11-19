"""ui_drive fixture for the "incomplete-scan" scenario (tools/ui_check/ui_drive.json).

Starts the in-process test SFTP server (tools/sftp_server_core.py) on
127.0.0.1 serving a folder under UI_DRIVE_OUT_DIR. The server folder Photos
holds one readable file and a subfolder, Private, whose listing the server
refuses with "permission denied", the way a real server answers for a folder
the user cannot read. A download of Photos should fetch the readable file and
end with one "Scan incomplete" summary naming Private.

Stays running (does nothing) until drive.py tears down the job. The run folder
is deleted afterward by `drive.py cleanup`.
"""
from __future__ import annotations

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

SCAN_FOLDER = "Photos"
REFUSED_FOLDER = "Private"


def main() -> int:
    out_dir = os.environ.get("UI_DRIVE_OUT_DIR")
    if not out_dir:
        print("UI_DRIVE_OUT_DIR is not set", file=sys.stderr)
        return 2

    server_root = os.path.join(out_dir, "fixture_server")
    local_dir = os.path.join(out_dir, "fixture_local")
    os.makedirs(os.path.join(server_root, SCAN_FOLDER, REFUSED_FOLDER))
    os.makedirs(local_dir)
    with open(os.path.join(server_root, SCAN_FOLDER, "beach.jpg"), "wb") as f:
        f.write(os.urandom(2048))
    with open(os.path.join(server_root, SCAN_FOLDER, REFUSED_FOLDER, "hidden.jpg"), "wb") as f:
        f.write(os.urandom(1024))

    base = sftp_server_core.make_fs(server_root)
    refused = f"/{SCAN_FOLDER}/{REFUSED_FOLDER}"

    class RefusingFS(base):
        def list_folder(self, path):
            if path.rstrip("/") == refused:
                return paramiko.sftp.SFTP_PERMISSION_DENIED
            return super().list_folder(path)

    _sock, port = sftp_server_core.start(RefusingFS, paramiko.RSAKey.generate(2048))

    print(
        json.dumps(
            {
                "port": port,
                "user": USER,
                "password": PASSWORD,
                "local_dir": local_dir,
                "scan_folder": SCAN_FOLDER,
                "refused_folder": REFUSED_FOLDER,
            }
        ),
        flush=True,
    )

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
