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

Design notes worth keeping:
  - canonicalize() returns a POSIX-clean absolute path. paramiko's default uses
    os.path, which on Windows hands the client back-slashed / doubled paths and
    leaves the app's remote pane blank.
  - The server key is loaded from / written to a file so restarts do not change
    the host key.
"""
import argparse
import os
import posixpath
import shutil
import socket
import threading

import paramiko

# ───────────── layout (all git-ignored) ─────────────
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(TOOLS_DIR, ".sftp_test")
KEY_FILE = os.path.join(RUNTIME, "hostkey")
SRV_ROOT = os.path.join(RUNTIME, "data")
SAMPLES = os.path.join(RUNTIME, "samples")

HOST = "127.0.0.1"
PORT = 2222
USER = "test"
PASSWORD = "testpass"

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


# ───────────── filesystem-backed SFTP interface ─────────────
class Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class FS(paramiko.SFTPServerInterface):
    ROOT = SRV_ROOT

    def _real(self, path):
        p = path if posixpath.isabs(path) else "/" + path
        p = posixpath.normpath(p).strip("/")
        return os.path.join(self.ROOT, *p.split("/")) if p else self.ROOT

    def list_folder(self, path):
        rp = self._real(path)
        try:
            out = []
            for name in os.listdir(rp):
                attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(rp, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        rp = self._real(path)
        try:
            flags |= getattr(os, "O_BINARY", 0)
            fd = os.open(rp, flags, 0o666)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        try:
            f = os.fdopen(fd, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = Handle(flags)
        h.filename = rp
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, path, attr):
        return paramiko.SFTP_OK

    def canonicalize(self, path):
        # POSIX-clean absolute path. The default uses os.path (Windows
        # back-slashes) which hands the client a malformed remote path and
        # leaves the app's remote pane blank.
        if not path.startswith("/"):
            path = "/" + path
        return posixpath.normpath(path)


class Server(paramiko.ServerInterface):
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_auth_password(self, username, password):
        if username == USER and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"


def _serve(sock, host_key):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, FS)
        try:
            t.start_server(server=Server())
        except Exception:
            continue


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

    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_sock.bind((HOST, PORT))
    srv_sock.listen(16)
    threading.Thread(target=_serve, args=(srv_sock, host_key), daemon=True).start()

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
