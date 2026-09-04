"""
Shared test setup.

Beyond making the project root importable, this hosts a throwaway, in-process
paramiko SFTP server and the fixtures that wire a real simple_sftp_client.Api
to it. Nothing is installed or left running: the server is a daemon thread
bound to an ephemeral port on 127.0.0.1, with a throwaway in-memory host key,
serving a pytest tmp_path. The server pieces are adapted from the manual
tools/test_sftp_server.py (see the test-sftp-server branch).
"""
import os
import posixpath
import socket
import sys
import threading
import time

# Make the project root (one level up from tests/) importable so
# `import simple_sftp_client` finds the module during test collection.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko
import pytest

import simple_sftp_client

USER = "test"
PASSWORD = "testpass"


# ───────────── in-process SFTP server (adapted from tools/test_sftp_server.py) ─────────────
class Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class FS(paramiko.SFTPServerInterface):
    ROOT = None  # set per test to a tmp_path subfolder

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

    # Set to False on a subclass to make posix_rename behave like a server
    # without the posix-rename@openssh.com extension, for the refuse-and-keep
    # test: the client must never fall back to a risky delete-then-rename.
    POSIX_RENAME_SUPPORTED = True

    def posix_rename(self, oldpath, newpath):
        # Backs the posix-rename@openssh.com extension, which the client uses
        # to publish a finished upload over its real destination. os.replace
        # overwrites the target atomically, matching real posix-rename
        # servers (the base SFTPServerInterface implementation just returns
        # unsupported, which is what a server without the extension does).
        if not self.POSIX_RENAME_SUPPORTED:
            return paramiko.SFTP_OP_UNSUPPORTED
        try:
            os.replace(self._real(oldpath), self._real(newpath))
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


def _serve(sock, host_key, fs_cls):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, fs_cls)
        try:
            t.start_server(server=Server())
        except Exception:
            continue


# ───────────── fixtures ─────────────
def _start_sftp_env(tmp_path, fs_extra_attrs=None):
    """Shared setup behind sftp_env and its variants: spin up the throwaway
    server rooted at tmp_path, connect an Api to it, and yield (api,
    server_root, local_dir). fs_extra_attrs overrides class attributes on the
    per-test FS subclass, e.g. to disable posix-rename support."""
    server_root = tmp_path / "server_root"
    server_root.mkdir()
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    attrs = {"ROOT": str(server_root)}
    attrs.update(fs_extra_attrs or {})
    fs_cls = type("FSForTest", (FS,), attrs)

    host_key = paramiko.RSAKey.generate(2048)
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_sock.bind(("127.0.0.1", 0))
    port = srv_sock.getsockname()[1]
    srv_sock.listen(16)
    thread = threading.Thread(target=_serve, args=(srv_sock, host_key, fs_cls), daemon=True)
    thread.start()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("127.0.0.1", port=port, username=USER, password=PASSWORD,
                    look_for_keys=False, allow_agent=False)
    sftp = client.open_sftp()

    api = simple_sftp_client.Api()
    api.client = client
    api.sftp = sftp
    api.connected = True

    try:
        yield api, server_root, local_dir
    finally:
        try:
            sftp.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        srv_sock.close()


@pytest.fixture
def sftp_env(tmp_path):
    """Spin up a throwaway SFTP server rooted at tmp_path, connect an Api to
    it, and tear everything down afterward. Yields (api, server_root, local_dir)."""
    yield from _start_sftp_env(tmp_path)


@pytest.fixture
def sftp_env_no_posix_rename(tmp_path):
    """Same as sftp_env, but the server reports posix-rename unsupported, the
    way a server without the posix-rename@openssh.com extension would."""
    yield from _start_sftp_env(tmp_path, {"POSIX_RENAME_SUPPORTED": False})


@pytest.fixture
def wait_for_drain():
    """Return a helper that polls until the queue is truly empty: no
    background scan still streaming files in, AND api.queue.pending() == 0.
    enqueue()/upload_paths() now return before the scan has queued anything,
    so pending() can read 0 for an instant before the scan starts; checking
    only pending() would let this fixture return too early on a slow scan."""
    def _wait(api, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not api._scan_active() and api.queue.pending() == 0:
                return
            time.sleep(0.05)
        pytest.fail(
            f"queue did not drain within {timeout}s, "
            f"scanning={api._scan_active()}, pending={api.queue.pending()}")
    return _wait


@pytest.fixture
def wait_for_queue_count():
    """Return a helper that polls until at least n items have appeared on the
    queue (or fails loudly after timeout). Scanning is async now, so a test
    that wants an item's id right after enqueue()/upload_paths() must wait
    for it to actually land first."""
    def _wait(api, n, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(api.queue.snapshot()) >= n:
                return
            time.sleep(0.02)
        pytest.fail(
            f"queue did not reach {n} item(s) within {timeout}s, "
            f"has {len(api.queue.snapshot())}")
    return _wait


@pytest.fixture
def state_of():
    """Return a helper that finds a queue item's snapshot entry by id."""
    def _state(api, item_id):
        for entry in api.queue.snapshot():
            if entry["id"] == item_id:
                return entry
        return None
    return _state
