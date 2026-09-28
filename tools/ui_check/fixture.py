"""ui_drive fixture for the "smoke" scenario (tools/ui_check/ui_drive.json).

Builds a small server folder and a small local folder, both under
UI_DRIVE_OUT_DIR (the whole run folder is deleted afterward by
`drive.py cleanup`, not by drive.py itself at the end of the run, so this
fixture never has to clean up its own files), starts the same in-process
test SFTP server the pytest fixtures use (tools/sftp_server_core.py) on
127.0.0.1, then prints one line of JSON with everything the scenario needs:
the port, the login, the local folder to point the app's local pane at, the
file names on each side, and a second port that was briefly bound and
released so a connect to it is refused (used for the error-path check).

Also sets up the symlinked-folder check: a server folder LINK_FOLDER holding
LINK_FILE, and a local folder of the same name that is a junction (a Windows
folder link, creatable without admin rights) to link_target, a separate folder
in the run folder. Downloading the server folder writes through the junction
into link_target, and the app logs that it is symlinked.

Stays running (does nothing) until drive.py tears down the job.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import paramiko  # noqa: E402

from tools import sftp_server_core  # noqa: E402
from tools.sftp_server_core import PASSWORD, USER  # noqa: E402

SERVER_FILES = ["s1.txt", "s2.txt"]
LOCAL_FILES = ["l1.txt", "l2.txt"]
LINK_FOLDER = "Photos"
LINK_FILE = "photo.txt"


def _closed_port() -> int:
    """Binds a loopback port, then closes it immediately: a moment later
    that port number is very likely still free, so a connect to it is
    refused rather than answered, exactly what the error-path check needs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    out_dir = os.environ.get("UI_DRIVE_OUT_DIR")
    if not out_dir:
        print("UI_DRIVE_OUT_DIR is not set", file=sys.stderr)
        return 2

    server_root = os.path.join(out_dir, "fixture_server")
    local_dir = os.path.join(out_dir, "fixture_local")
    os.makedirs(server_root)
    os.makedirs(local_dir)
    os.makedirs(os.path.join(local_dir, "sub"))

    for name in SERVER_FILES:
        with open(os.path.join(server_root, name), "w", encoding="utf-8") as f:
            f.write(name)
    for name in LOCAL_FILES:
        with open(os.path.join(local_dir, name), "w", encoding="utf-8") as f:
            f.write(name)
    with open(os.path.join(local_dir, "sub", "insub.txt"), "w", encoding="utf-8") as f:
        f.write("insub")

    os.makedirs(os.path.join(server_root, LINK_FOLDER))
    with open(os.path.join(server_root, LINK_FOLDER, LINK_FILE), "w", encoding="utf-8") as f:
        f.write(LINK_FILE)
    link_target = os.path.join(out_dir, "link_target")
    os.makedirs(link_target)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.path.join(local_dir, LINK_FOLDER), link_target],
        check=True, capture_output=True,
    )

    fs_cls = sftp_server_core.make_fs(server_root)
    host_key = paramiko.RSAKey.generate(2048)
    _sock, port = sftp_server_core.start(fs_cls, host_key)

    print(
        json.dumps(
            {
                "port": port,
                "closed_port": _closed_port(),
                "user": USER,
                "password": PASSWORD,
                "local_dir": local_dir,
                "server_files": SERVER_FILES,
                "local_files": LOCAL_FILES,
                "link_folder": LINK_FOLDER,
                "link_file": LINK_FILE,
                "link_target": os.path.realpath(link_target),
            }
        ),
        flush=True,
    )

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
