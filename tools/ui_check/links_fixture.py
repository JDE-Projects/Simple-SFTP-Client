"""ui_drive fixture for the "links" scenario (tools/ui_check/ui_drive.json).

Creates a local folder containing real.txt, new.txt (local only, so Sync Up
has something to transfer), a junction named j to a separate folder containing
secret.txt, and a folder up holding inner.txt and a junction k to that same
separate folder. The remote folder has real.txt and j/remote.txt. The
junctions are removed with os.rmdir during normal fixture shutdown, so their
target and secret.txt are never traversed or deleted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import paramiko  # noqa: E402

from tools import sftp_server_core  # noqa: E402
from tools.sftp_server_core import PASSWORD, USER  # noqa: E402


def main() -> int:
    out_dir = os.environ.get("UI_DRIVE_OUT_DIR")
    if not out_dir:
        print("UI_DRIVE_OUT_DIR is not set", file=sys.stderr)
        return 2

    server_root = os.path.join(out_dir, "fixture_server")
    local_dir = os.path.join(out_dir, "fixture_local")
    secret_dir = os.path.join(out_dir, "fixture_secret")
    link_path = os.path.join(local_dir, "j")
    up_dir = os.path.join(local_dir, "up")
    up_link = os.path.join(up_dir, "k")
    os.makedirs(os.path.join(server_root, "j"))
    os.makedirs(local_dir)
    os.makedirs(secret_dir)
    with open(os.path.join(server_root, "real.txt"), "w", encoding="utf-8") as f:
        f.write("real")
    with open(os.path.join(server_root, "j", "remote.txt"), "w", encoding="utf-8") as f:
        f.write("remote")
    with open(os.path.join(local_dir, "real.txt"), "w", encoding="utf-8") as f:
        f.write("real")
    with open(os.path.join(local_dir, "new.txt"), "w", encoding="utf-8") as f:
        f.write("new")
    os.makedirs(up_dir)
    with open(os.path.join(up_dir, "inner.txt"), "w", encoding="utf-8") as f:
        f.write("inner")
    with open(os.path.join(secret_dir, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("secret")
    subprocess.run(["cmd", "/c", "mklink", "/J", link_path, secret_dir], check=True, capture_output=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", up_link, secret_dir], check=True, capture_output=True)

    fs_cls = sftp_server_core.make_fs(server_root)
    _sock, port = sftp_server_core.start(fs_cls, paramiko.RSAKey.generate(2048))
    print(json.dumps({"port": port, "user": USER, "password": PASSWORD, "local_dir": local_dir}), flush=True)

    try:
        while True:
            time.sleep(3600)
    finally:
        for path in (link_path, up_link):
            if os.path.lexists(path):
                os.rmdir(path)


if __name__ == "__main__":
    sys.exit(main())
