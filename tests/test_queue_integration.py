"""
Integration tests for the transfer queue backend (transfer_queue.py wired
into simple_sftp_client.Api) against a real, in-process paramiko SFTP server.

Nothing is installed or left running: the server is a throwaway thread bound
to an ephemeral port on 127.0.0.1, with a throwaway in-memory host key, and
everything it serves lives under a pytest tmp_path. Adapted from the manual
server in tools/test_sftp_server.py (see the test-sftp-server branch).
"""
import os
import posixpath
import socket
import threading
import time

import paramiko
import pytest

import simple_sftp_client
from transfer_queue import COMPLETED, CANCELLED, WAITING

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
@pytest.fixture
def sftp_env(tmp_path):
    """Spin up a throwaway SFTP server rooted at tmp_path, connect an Api to
    it, and tear everything down afterward. Yields (api, server_root, local_dir)."""
    server_root = tmp_path / "server_root"
    server_root.mkdir()
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    fs_cls = type("FSForTest", (FS,), {"ROOT": str(server_root)})

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


def _wait_for_drain(api, timeout=15):
    """Poll api.queue.pending() until it hits 0, or fail on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api.queue.pending() == 0:
            return
        time.sleep(0.05)
    pytest.fail(f"queue did not drain within {timeout}s, pending={api.queue.pending()}")


def _state_of(api, item_id):
    for entry in api.queue.snapshot():
        if entry["id"] == item_id:
            return entry
    return None


# ───────────── tests ─────────────
def test_upload_byte_integrity(sftp_env):
    api, server_root, local_dir = sftp_env
    data = os.urandom(64 * 1024 + 37)  # not a round chunk multiple, on purpose
    src = local_dir / "up.bin"
    src.write_bytes(data)

    result = api.enqueue([{"name": "up.bin", "is_dir": False}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    item_id = api.queue.snapshot()[0]["id"]

    _wait_for_drain(api)

    entry = _state_of(api, item_id)
    assert entry["state"] == COMPLETED
    served_copy = server_root / "up.bin"
    assert served_copy.read_bytes() == data


def test_download_byte_integrity(sftp_env):
    api, server_root, local_dir = sftp_env
    data = os.urandom(96 * 1024 + 5)
    (server_root / "down.bin").write_bytes(data)

    result = api.enqueue([{"name": "down.bin", "is_dir": False}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    item_id = api.queue.snapshot()[0]["id"]

    _wait_for_drain(api)

    entry = _state_of(api, item_id)
    assert entry["state"] == COMPLETED
    local_copy = local_dir / "down.bin"
    assert local_copy.read_bytes() == data


def test_multiple_files_drain_in_order_and_all_complete(sftp_env):
    api, server_root, local_dir = sftp_env
    names = [f"file{i}.bin" for i in range(4)]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(2048))

    jobs = [{"name": name, "is_dir": False} for name in names]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["queued"] == len(names)

    _wait_for_drain(api)

    snap = api.queue.snapshot()
    assert len(snap) == len(names)
    assert all(entry["state"] == COMPLETED for entry in snap)
    assert api.queue.pending() == 0


def test_cancel_waiting_item_behind_a_slower_active_one(sftp_env):
    api, server_root, local_dir = sftp_env
    # a few MB, so the first item's byte loop keeps the worker busy long
    # enough for the second (waiting) item to be cancelled deterministically
    big = local_dir / "big.bin"
    big.write_bytes(os.urandom(4 * 1024 * 1024))
    small = local_dir / "small.bin"
    small.write_bytes(os.urandom(16))

    jobs = [{"name": "big.bin", "is_dir": False}, {"name": "small.bin", "is_dir": False}]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    snap = api.queue.snapshot()
    big_id = next(e["id"] for e in snap if e["name"] == "big.bin")
    small_id = next(e["id"] for e in snap if e["name"] == "small.bin")

    # wait until the big item is claimed (active) before cancelling the one
    # still waiting behind it
    deadline = time.time() + 15
    while time.time() < deadline:
        entry = _state_of(api, big_id)
        if entry["state"] != WAITING:
            break
        time.sleep(0.02)

    assert api.cancel_item(small_id) == {"ok": True}

    _wait_for_drain(api)

    assert _state_of(api, small_id)["state"] == CANCELLED
    assert _state_of(api, big_id)["state"] == COMPLETED
    served_small = server_root / "small.bin"
    assert not served_small.exists()


def test_enqueue_locked_out_while_legacy_transfer_active(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "file.bin").write_bytes(os.urandom(16))

    api._legacy_active.set()
    try:
        result = api.enqueue([{"name": "file.bin", "is_dir": False}], "upload",
                              str(local_dir), "/", "overwrite")
    finally:
        api._legacy_active.clear()

    assert result["ok"] is False
    assert "sync" in result["error"] or "watch" in result["error"]
    assert api.queue.pending() == 0


def test_upload_paths_locked_out_while_queue_pending(sftp_env):
    api, server_root, local_dir = sftp_env
    # a few MB, so the item is reliably still pending when upload_paths is
    # called a moment later, instead of racing the worker to drain first
    queued_file = local_dir / "queued.bin"
    queued_file.write_bytes(os.urandom(4 * 1024 * 1024))

    result = api.enqueue([{"name": "queued.bin", "is_dir": False}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    dropped_file = local_dir / "dropped.bin"
    dropped_file.write_bytes(os.urandom(16))

    result = api.upload_paths([str(dropped_file)], "/", "overwrite")

    assert result["ok"] is False
    assert "queue" in result["error"].lower()

    _wait_for_drain(api)
