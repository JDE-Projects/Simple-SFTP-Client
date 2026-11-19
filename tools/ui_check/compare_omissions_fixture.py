"""ui_drive fixture for the "compare-omissions" scenario.

The Shared server folder holds one transferable file plus a case-clashing
pair and a Windows-unsafe name supplied only by the SFTP listing. The fake
entries refuse stat and open requests, so an attempted transfer fails the
scenario instead of reading a real file.
"""
from __future__ import annotations

import copy
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
FAKE_NAMES = {"report.txt", "a:b.txt"}


def main() -> int:
    out_dir = os.environ.get("UI_DRIVE_OUT_DIR")
    if not out_dir:
        print("UI_DRIVE_OUT_DIR is not set", file=sys.stderr)
        return 2

    server_root = os.path.join(out_dir, "fixture_server")
    local_dir = os.path.join(out_dir, "fixture_local")
    shared_dir = os.path.join(server_root, SHARED_FOLDER)
    os.makedirs(shared_dir)
    os.makedirs(local_dir)
    with open(os.path.join(shared_dir, "good.txt"), "w", encoding="utf-8") as f:
        f.write("good")
    with open(os.path.join(shared_dir, "Report.txt"), "w", encoding="utf-8") as f:
        f.write("real report")

    base = sftp_server_core.make_fs(server_root)

    class OmissionFS(base):
        @staticmethod
        def _is_fake(path):
            clean = posixpath.normpath(path if path.startswith("/") else "/" + path)
            return posixpath.dirname(clean) == "/Shared" and posixpath.basename(clean) in FAKE_NAMES

        def list_folder(self, path):
            out = super().list_folder(path)
            if path.rstrip("/") == "/Shared" and isinstance(out, list):
                extra = []
                for attr in out:
                    if attr.filename == "Report.txt":
                        for name in ("report.txt", "a:b.txt"):
                            fake = copy.copy(attr)
                            fake.filename = name
                            extra.append(fake)
                out += extra
            return out

        def stat(self, path):
            if self._is_fake(path):
                return paramiko.SFTP_NO_SUCH_FILE
            return super().stat(path)

        def lstat(self, path):
            if self._is_fake(path):
                return paramiko.SFTP_NO_SUCH_FILE
            return super().lstat(path)

        def open(self, path, flags, attr):
            if self._is_fake(path):
                return paramiko.SFTP_NO_SUCH_FILE
            return super().open(path, flags, attr)

    _sock, port = sftp_server_core.start(OmissionFS, paramiko.RSAKey.generate(2048))
    print(json.dumps({"port": port, "user": USER, "password": PASSWORD,
                      "local_dir": local_dir, "shared_folder": SHARED_FOLDER}), flush=True)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
