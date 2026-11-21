"""Run Simple SFTP Server in a separate process for client fallback tests."""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-repo", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, args.server_repo)

    from app.server import QUICK_PERMISSIONS, SFTPService
    from tests.sftp_helpers import CountingApi, free_port, make_host_key, make_user

    root = Path(args.root)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    permissions = dict(QUICK_PERMISSIONS)
    if args.no_upload:
        permissions["upload"] = False
        permissions["rename_file"] = False
        permissions["delete"] = False
    user = make_user("fallback", home, permissions, password="fallback-password")
    service = SFTPService(CountingApi(users={user["username"]: user}))
    service.host_key = make_host_key(root)
    result = service.start(free_port())
    if not result["ok"]:
        raise RuntimeError(result["error"])
    print(result["port"], flush=True)
    try:
        sys.stdin.read()
    finally:
        service.stop()


if __name__ == "__main__":
    main()
