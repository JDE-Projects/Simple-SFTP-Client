"""
Shared test setup.

Hosts a throwaway, in-process SFTP server and the fixtures that wire a real
simple_sftp_client.Api to it. Nothing is installed or left running: the server
(tools/sftp_server_core.py, shared with the manual test server) runs on a
daemon thread bound to an ephemeral port on 127.0.0.1, with a throwaway
in-memory host key, serving a pytest tmp_path.
"""
import hashlib
import time
from pathlib import Path

import paramiko
import pytest

import simple_sftp_client
from tools import sftp_server_core
from tools.sftp_server_core import PASSWORD, USER


# ───────── data-file isolation ─────────
def _file_digest(path):
    """Return a file SHA-256, or absent when path does not exist."""
    if not path.is_file():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_data_snapshot():
    """Capture app data files at the repository root for the session guard."""
    repo_root = Path(__file__).resolve().parents[1]
    named_files = (
        "servers.json",
        "known_hosts",
        "simple_sftp_client.pref",
    )
    return {
        "files": {name: _file_digest(repo_root / name) for name in named_files},
        "debug_logs": {
            path.name for path in repo_root.glob("Debug_Log_*.txt") if path.is_file()
        },
    }


@pytest.fixture(scope="session", autouse=True)
def protect_repo_data_files():
    """Fail when a test changes app data in the repository root.

    Running the app from source during a test run can trip this guard.
    """
    before = _repo_data_snapshot()
    yield
    after = _repo_data_snapshot()
    changed = [
        name for name, digest in before["files"].items()
        if after["files"][name] != digest
    ]
    if after["debug_logs"] != before["debug_logs"]:
        changed.extend(sorted(before["debug_logs"] ^ after["debug_logs"]))
    if changed:
        pytest.fail("Repository app data changed during tests: " + ", ".join(changed))


@pytest.fixture(autouse=True)
def isolate_app_data_files(tmp_path_factory, monkeypatch):
    """Redirect application data writes to a per-test temporary folder."""
    data_dir = tmp_path_factory.mktemp("app-data")
    monkeypatch.setattr(simple_sftp_client, "SESSIONS_FILE", str(data_dir / "servers.json"))
    monkeypatch.setattr(simple_sftp_client, "KNOWN_HOSTS_FILE", str(data_dir / "known_hosts"))
    monkeypatch.setattr(simple_sftp_client, "_pref_path",
                        lambda: str(data_dir / "simple_sftp_client.pref"))
    monkeypatch.setattr(simple_sftp_client.debug, "log_dir", str(data_dir))
    return data_dir


# ───────────── fixtures ─────────────
def _bring_up_server(tmp_path, fs_extra_attrs=None):
    """Spin up the throwaway SFTP server rooted at tmp_path and return
    (port, server_root, local_dir, srv_sock). The caller owns closing
    srv_sock. Shared by the pre-connected sftp_env fixtures and the
    sftp_server fixture, which hands back connection params so a test can
    drive the real Api.connect() (trust-on-first-use and all)."""
    server_root = tmp_path / "server_root"
    server_root.mkdir()
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    fs_cls = sftp_server_core.make_fs(server_root, **(fs_extra_attrs or {}))
    srv_sock, port = sftp_server_core.start(fs_cls, paramiko.RSAKey.generate(2048))
    return port, server_root, local_dir, srv_sock


def _start_sftp_env(tmp_path, fs_extra_attrs=None):
    """Shared setup behind sftp_env and its variants: spin up the throwaway
    server rooted at tmp_path, connect an Api to it, and yield (api,
    server_root, local_dir). fs_extra_attrs overrides class attributes on the
    per-test FS subclass, e.g. to disable posix-rename support."""
    port, server_root, local_dir, srv_sock = _bring_up_server(tmp_path, fs_extra_attrs)

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
def sftp_server(tmp_path):
    """Start a throwaway SFTP server but do NOT connect an Api to it. Yields
    (params, server_root, local_dir) where params is a dict ready for the
    real Api.connect(), so a test can exercise the actual connect path
    (trust-on-first-use, partial-failure cleanup, the on-connect scratch
    sweep) end to end rather than injecting a pre-made client."""
    port, server_root, local_dir, srv_sock = _bring_up_server(tmp_path)
    params = {"host": "127.0.0.1", "port": port, "username": USER,
              "password": PASSWORD}
    try:
        yield params, server_root, local_dir
    finally:
        srv_sock.close()


@pytest.fixture
def sftp_env_no_posix_rename(tmp_path):
    """Same as sftp_env, but the server reports posix-rename unsupported, the
    way a server without the posix-rename@openssh.com extension would."""
    yield from _start_sftp_env(tmp_path, {"POSIX_RENAME_SUPPORTED": False})


@pytest.fixture
def sftp_env_no_set_time(tmp_path):
    """Same as sftp_env, but the server refuses to set file modification
    times, the way a server without SFTP time-setting support would."""
    yield from _start_sftp_env(tmp_path, {"SET_TIME_SUPPORTED": False})


@pytest.fixture
def sftp_env_no_times(tmp_path):
    """Same as sftp_env, but the server leaves modification times out of its
    stat and listing replies, so the client never learns a remote file's time."""
    yield from _start_sftp_env(tmp_path, {"REPORT_TIMES": False})


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
def wait_until():
    """Return a helper that polls a condition callable until it is truthy, or
    fails loudly after timeout. Used by the watcher tests, whose uploads land a
    couple of poll intervals after a change."""
    def _wait(cond, timeout=5, interval=0.02):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(interval)
        pytest.fail(f"condition not met within {timeout}s")
    return _wait


@pytest.fixture
def wait_for_compare():
    """Return a helper that polls poll_queue() until a compare_done payload
    of the given kind ("compare" or "sync", or None for either) arrives, and
    returns that payload. Used by tests driving compare()/sync_plan()'s
    async contract instead of a pure _compute_compare/_compute_sync call."""
    def _wait(api, kind=None, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = api.poll_queue()
            done = status.get("compare_done")
            if done is not None and (kind is None or done["kind"] == kind):
                return done
            time.sleep(0.02)
        pytest.fail(f"compare_done ({kind or 'any'}) not delivered within {timeout}s")
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
