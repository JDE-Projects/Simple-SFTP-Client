"""
Tests for the one idempotent shutdown path (Disconnect and window/app close):
a large queue must wind down promptly on disconnect instead of grinding every
remaining file through its retries, a shutdown mid-transfer must leave the
real destination untouched and stop worker threads within a bounded time, a
failed connect() must never leak a client or corrupt state for the next
attempt, a mid-batch server drop must stop the queue promptly and report the
failure once (not once per remaining file), the scratch sweep on connect must
touch only this app's own scratch files, and shutdown() must be safe to call
more than once.

Runs against the in-process SFTP server from conftest.py (sftp_env) plus a
few pure Api() instances with a stubbed _open for the connect-only cases.
"""
import os
import threading
import time

import simple_sftp_client
from transfer_queue import CANCELLED, FAILED, WAITING

from simple_sftp_client import Api, is_temp_part


def _remote_temp_files(server_root):
    return [n for n in os.listdir(server_root) if is_temp_part(n)]


def _wait_until_not_waiting(api, item_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = next((e for e in api.queue.snapshot() if e["id"] == item_id), None)
        if entry is not None and entry["state"] != WAITING:
            return
        time.sleep(0.02)


# ───────────── large-queue disconnect ─────────────

def test_large_queue_disconnect_stops_workers_promptly(sftp_env, wait_for_queue_count):
    api, server_root, local_dir = sftp_env
    names = [f"f{i}.bin" for i in range(60)]
    for n in names:
        (local_dir / n).write_bytes(os.urandom(4096))
    jobs = [{"name": n, "is_dir": False} for n in names]
    assert api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")["ok"] is True
    wait_for_queue_count(api, len(jobs))

    start = time.time()
    assert api.disconnect() == {"ok": True}
    elapsed = time.time() - start

    # Bounded: even with dozens of queued files this must not fall back to
    # each one grinding through three retries with a 0.6s sleep apiece
    # (60 files * 3 * 0.6s would be almost two minutes).
    assert elapsed < 8
    assert not any(w.is_alive() for w in api._workers)

    states = [e["state"] for e in api.queue.snapshot()]
    # Every item left WAITING when the disconnect landed goes straight to
    # CANCELLED (cancel_all()), never ground through retries into FAILED.
    assert FAILED not in states


# ───────────── shutdown mid-transfer ─────────────

def test_shutdown_mid_transfer_leaves_destination_intact_and_threads_stop(
        sftp_env, wait_for_queue_count):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    original = b"O" * 4096
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(os.urandom(8 * 1024 * 1024))  # stays mid-flight a while

    result = api.enqueue([{"name": name, "is_dir": False}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_queue_count(api, 1)
    item_id = api.queue.snapshot()[0]["id"]
    _wait_until_not_waiting(api, item_id)

    start = time.time()
    assert api.shutdown() == {"ok": True}
    elapsed = time.time() - start

    assert elapsed < 8
    assert not any(w.is_alive() for w in api._workers)
    # The real destination is never touched until the atomic swap at the very
    # end (task 3): a shutdown mid-flight must leave it exactly as it was.
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── repeated / partial-failure connect ─────────────

class _FakeSFTPOk:
    def normalize(self, path):
        return "/home/test"

    def stat(self, path):
        return object()


class _FakeClientFail:
    """Stands in for a client whose transport connects but whose SFTP
    subsystem never comes up: open_sftp() fails after client.connect()
    already succeeded inside _open()."""
    def __init__(self, calls):
        self._calls = calls

    def open_sftp(self):
        raise OSError("sftp subsystem refused")

    def close(self):
        self._calls.append("closed_fail_client")

    def get_transport(self):
        return None


class _FakeClientOk:
    def __init__(self, calls):
        self._calls = calls

    def open_sftp(self):
        return _FakeSFTPOk()

    def close(self):
        self._calls.append("closed_ok_client")

    def get_transport(self):
        return None


def test_connect_failure_then_success_leaves_no_dangling_client(monkeypatch, tmp_path):
    api = Api()
    # Keep the post-connect scratch sweep off the real home directory.
    api._local_cwd = str(tmp_path)
    calls = []
    attempts = [_FakeClientFail(calls), _FakeClientOk(calls)]

    def fake_open(*a, **k):
        return attempts.pop(0)
    monkeypatch.setattr(api, "_open", fake_open)

    payload = {"host": "h", "username": "u", "password": "p"}

    r1 = api.connect(payload)
    assert r1["ok"] is False
    assert api.client is None
    assert api.sftp is None
    assert api.connected is False
    assert "closed_fail_client" in calls

    r2 = api.connect(payload)
    assert r2["ok"] is True
    assert api.connected is True
    assert isinstance(api.client, _FakeClientOk)
    # The failed attempt's client was closed and never touched the good one.
    assert "closed_fail_client" in calls
    assert "closed_ok_client" not in calls


# ───────────── mid-batch server drop ─────────────

def test_mid_batch_server_drop_stops_queue_and_reports_once(sftp_env, wait_for_queue_count, wait_for_drain):
    api, server_root, local_dir = sftp_env
    names = [f"f{i}.bin" for i in range(24)]
    for n in names:
        (local_dir / n).write_bytes(os.urandom(4 * 1024 * 1024))
    jobs = [{"name": n, "is_dir": False} for n in names]
    assert api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")["ok"] is True
    wait_for_queue_count(api, len(jobs))

    # Simulate the server vanishing mid-batch, deterministically rather than
    # racing a sleep: close the shared transport from inside the very first
    # progress callback any worker makes, so at least one item is still
    # mid-flight when the drop happens. Closing the transport is
    # indistinguishable, from a worker's side, from a real network drop.
    drop_lock = threading.Lock()
    dropped = {"done": False}
    real_progress = api._progress

    def drop_then_progress(*a, **kw):
        with drop_lock:
            if not dropped["done"]:
                dropped["done"] = True
                api.client.close()
        return real_progress(*a, **kw)
    api._progress = drop_then_progress

    start = time.time()
    wait_for_drain(api)
    elapsed = time.time() - start
    # Promptly: not every remaining file grinding through three retries at
    # 0.6s apiece.
    assert elapsed < 10

    lines = []
    for _ in range(30):
        polled = api.poll_queue()
        lines += [c["msg"] for c in polled["console"]]
        if any("Connection lost" in m for m in lines):
            break
        time.sleep(0.05)
    conn_lost_lines = [m for m in lines if "Connection lost" in m]
    assert len(conn_lost_lines) == 1


# ───────────── scratch sweep on connect ─────────────

def test_scratch_sweep_on_connect_removes_only_matching_files(monkeypatch, tmp_path):
    local_dir = tmp_path / "browse"
    local_dir.mkdir()
    (local_dir / "real.bin").write_bytes(b"data")
    (local_dir / ".not_scratch.txt").write_bytes(b"x")
    (local_dir / ".foo.bin.deadbeef.sxtpart").write_bytes(b"partial")
    (local_dir / ".bar.bin.cafebabe.sxtpart").write_bytes(b"partial2")

    api = Api()
    api._local_cwd = str(local_dir)
    calls = []
    monkeypatch.setattr(api, "_open", lambda *a, **k: _FakeClientOk(calls))

    result = api.connect({"host": "h", "username": "u", "password": "p"})
    assert result["ok"] is True

    remaining = {p.name for p in local_dir.iterdir()}
    assert remaining == {"real.bin", ".not_scratch.txt"}


# ───────────── real connect() end to end ─────────────

def test_real_connect_lifecycle_sweeps_scratch_and_shuts_down(
        sftp_server, wait_for_drain, monkeypatch, tmp_path):
    """Drive the actual Api.connect() against a live server: trust-on-first-use,
    the on-connect scratch sweep against the browsed folder, a real transfer
    over the freshly opened session, and an orderly disconnect. The pre-wired
    sftp_env fixtures skip connect() entirely, so this is the only coverage of
    the connect path the shutdown work rewrote."""
    params, server_root, local_dir = sftp_server
    # Never touch the user's real known_hosts: point the trust store at a
    # throwaway file for this test.
    monkeypatch.setattr(simple_sftp_client, "KNOWN_HOSTS_FILE",
                        str(tmp_path / "known_hosts"))

    api = Api()
    # Browse the local folder first so the sweep is scoped to it, then drop a
    # leftover scratch file there for connect() to clean up.
    api.list_local(str(local_dir))
    scratch = local_dir / ".ghost.bin.deadbeef.sxtpart"
    scratch.write_bytes(b"orphan")

    # First contact with an unknown host: trust-on-first-use asks, it does not
    # silently accept.
    r1 = api.connect(params)
    assert r1.get("host_key_unknown") is True
    assert api.connected is False
    assert api.client is None

    assert api.trust_host_key()["ok"] is True

    r2 = api.connect(params)
    assert r2["ok"] is True
    assert api.connected is True
    assert api.client is not None and api.sftp is not None
    assert "home" in r2 and "cwd" in r2
    # The on-connect sweep removed the orphaned scratch file.
    assert not scratch.exists()

    # A real transfer works through the freshly connected session.
    (local_dir / "hello.txt").write_bytes(b"hello world")
    assert api.enqueue([{"name": "hello.txt", "is_dir": False}], "upload",
                       str(local_dir), "/", "overwrite")["ok"] is True
    wait_for_drain(api)
    assert (server_root / "hello.txt").read_bytes() == b"hello world"

    # Orderly disconnect tears the real session down and clears state.
    assert api.disconnect() == {"ok": True}
    assert api.connected is False
    assert api.client is None and api.sftp is None
    assert not any(w.is_alive() for w in api._workers)


# ───────────── shutdown idempotence ─────────────

def test_shutdown_is_safe_to_call_twice(sftp_env):
    api, server_root, local_dir = sftp_env
    assert api.shutdown() == {"ok": True}
    assert api.shutdown() == {"ok": True}
    assert api.connected is False
    assert api.client is None
    assert api.sftp is None
