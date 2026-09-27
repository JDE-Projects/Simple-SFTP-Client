"""Local test SFTP server for manually smoke-testing Simple SFTP Client.

Runs a small paramiko-backed SFTP server on 127.0.0.1:2222 so you can connect
the app to something real without a remote host. Nothing is installed or left
running system-wide, and everything it generates is cleaned up on exit.

    Connect with:  host 127.0.0.1   port 2222   user test   pass testpass

Run it:
    .venv/Scripts/python.exe tools/test_sftp_server.py
    .venv/Scripts/python.exe tools/test_sftp_server.py --samples

Stop it with Ctrl-C. On start the served folder is emptied; on stop the served
folder and any generated sample files are removed. The host key is kept (see
below) so the app is not re-prompted every launch.

Everything it writes lives under tools/.sftp_test/ (git-ignored):
    hostkey     a stable server identity, generated once and reused. Keeping it
                stable is deliberate: a fresh key each run would make the app
                show its "host key changed" warning every time.
    data/       the folder served as "/". Emptied on start, removed on stop.
    samples/    sample upload files (only with --samples). Removed on stop.

The server itself lives in tools/sftp_server_core.py, shared with the pytest
fixtures in tests/conftest.py. This script adds the fixed port, the stable host
key, the served folder, and the sample files.
"""
import argparse
import os
import shutil
import threading

import paramiko

from sftp_server_core import PASSWORD, USER, make_fs, start

# ───────────── layout (all git-ignored) ─────────────
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(TOOLS_DIR, ".sftp_test")
KEY_FILE = os.path.join(RUNTIME, "hostkey")
SRV_ROOT = os.path.join(RUNTIME, "data")
SAMPLES = os.path.join(RUNTIME, "samples")

HOST = "127.0.0.1"
PORT = 2222

# Sample upload files: name -> size in bytes. The 60MB file makes a transfer
# long enough to catch a cancel.
SAMPLE_FILES = {
    "small_1kb.bin": 1024,
    "mid_250kb.bin": 250 * 1024,
    "mid_2mb.bin": 2 * 1024 * 1024,
    "big_60mb.bin": 60 * 1024 * 1024,
}


def _load_host_key():
    """A stable RSA host key, generated once and reused across runs."""
    os.makedirs(RUNTIME, exist_ok=True)
    if os.path.exists(KEY_FILE):
        return paramiko.RSAKey(filename=KEY_FILE)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(KEY_FILE)
    return key


def _make_samples():
    os.makedirs(SAMPLES, exist_ok=True)
    for name, size in SAMPLE_FILES.items():
        with open(os.path.join(SAMPLES, name), "wb") as f:
            f.write(os.urandom(size))
    print(f"Sample upload files in: {SAMPLES}")
    for name in SAMPLE_FILES:
        print(f"    {name}")


def _cleanup():
    """Remove everything this run generated except the stable host key."""
    for path in (SRV_ROOT, SAMPLES):
        shutil.rmtree(path, ignore_errors=True)
    print("Cleaned up served files and samples.")


def main():
    ap = argparse.ArgumentParser(description="Local test SFTP server for the app.")
    ap.add_argument("--samples", action="store_true",
                    help="also generate sample files to upload from")
    args = ap.parse_args()

    host_key = _load_host_key()
    # Fresh, empty served folder each run.
    shutil.rmtree(SRV_ROOT, ignore_errors=True)
    os.makedirs(SRV_ROOT, exist_ok=True)
    if args.samples:
        _make_samples()

    srv_sock, _ = start(make_fs(SRV_ROOT), host_key, HOST, PORT)

    print(f"SFTP test server on {HOST}:{PORT}   user={USER}   pass={PASSWORD}")
    print(f"Serving folder: {SRV_ROOT}")
    print("Press Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print()
    finally:
        srv_sock.close()
        _cleanup()


if __name__ == "__main__":
    main()
