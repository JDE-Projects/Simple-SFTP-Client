"""
Tests that Compare and Sync refuse a folder pair once either side's map would
grow past COMPARE_SYNC_ENTRY_LIMIT entries (files plus empty-folder markers),
the same way they refuse an incomplete scan (see
test_compare_sync_incomplete_scan.py, which this file follows the style of).
_compute_pair_maps holds both sides in memory at once to classify anything, so
a tree larger than the limit is refused with a clear message instead of
running out of memory partway through. The limit is patched down to a tiny
value for these tests so they run fast; the constant itself is not tested for
its real-world size, only that the check fires at the configured value.
"""
import stat

import pytest

import simple_sftp_client
from app import constants


LIMIT = 3


@pytest.fixture(autouse=True)
def _small_limit(monkeypatch):
    monkeypatch.setattr(constants, "COMPARE_SYNC_ENTRY_LIMIT", LIMIT)


class _FakeAttr:
    """Stand-in for paramiko.SFTPAttributes: just the fields _iter_remote
    reads (filename, st_mode, st_size, st_mtime)."""
    def __init__(self, filename, is_dir=False, size=0, mtime=0):
        self.filename = filename
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG
        self.st_size = size
        self.st_mtime = mtime


class _FakeSftp:
    """Stand-in sftp session whose listdir_iter yields a canned list per
    remote path; a path missing from tree reads as empty."""
    def __init__(self, tree):
        self.tree = tree  # {remote_path: [attrs]}

    def listdir_iter(self, rp):
        return iter(self.tree.get(rp, []))


class _NeverCalledSftp:
    """Fails the test the moment anything asks it to list a folder, proving
    the remote (network) walk was never entered."""
    def listdir_iter(self, rp):
        pytest.fail(f"remote walk was entered (listdir_iter({rp!r})) after "
                     "the local side already hit the entry limit")


def _make_local_files(local_dir, n):
    for i in range(n):
        (local_dir / f"f{i}.txt").write_bytes(b"x")


def _remote_tree(n):
    return {"/": [_FakeAttr(f"f{i}.txt", size=0) for i in range(n)]}


# ───────────── local side over the limit refuses before the remote walk starts ─────────────

def test_compare_refuses_when_local_passes_limit_remote_never_entered(tmp_path):
    api = simple_sftp_client.Api(simple_sftp_client.APP_VERSION)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _make_local_files(local_dir, LIMIT + 1)
    sftp = _NeverCalledSftp()

    with pytest.raises(simple_sftp_client.ScanIncomplete, match="Too many files to compare"):
        api._compute_compare(sftp, str(local_dir), "/")


def test_sync_refuses_when_local_passes_limit_remote_never_entered(tmp_path):
    api = simple_sftp_client.Api(simple_sftp_client.APP_VERSION)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _make_local_files(local_dir, LIMIT + 1)
    sftp = _NeverCalledSftp()

    with pytest.raises(simple_sftp_client.ScanIncomplete, match="Too many files to compare"):
        api._compute_sync(sftp, str(local_dir), "/", "upload")


# ───────────── remote side over the limit also refuses ─────────────

def test_compare_refuses_when_remote_passes_limit(tmp_path):
    api = simple_sftp_client.Api(simple_sftp_client.APP_VERSION)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _make_local_files(local_dir, LIMIT)
    sftp = _FakeSftp(_remote_tree(LIMIT + 1))

    with pytest.raises(simple_sftp_client.ScanIncomplete, match="Too many files to compare"):
        api._compute_compare(sftp, str(local_dir), "/")


def test_sync_refuses_when_remote_passes_limit(tmp_path):
    api = simple_sftp_client.Api(simple_sftp_client.APP_VERSION)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _make_local_files(local_dir, LIMIT)
    sftp = _FakeSftp(_remote_tree(LIMIT + 1))

    with pytest.raises(simple_sftp_client.ScanIncomplete, match="Too many files to compare"):
        api._compute_sync(sftp, str(local_dir), "/", "download")


# ───────────── exactly at the limit on both sides still succeeds ─────────────

def test_exactly_at_limit_on_both_sides_succeeds_for_compare(tmp_path):
    api = simple_sftp_client.Api(simple_sftp_client.APP_VERSION)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _make_local_files(local_dir, LIMIT)
    sftp = _FakeSftp(_remote_tree(LIMIT))

    data = api._compute_compare(sftp, str(local_dir), "/")

    assert data is not None
    assert len(data["files"]) == LIMIT


def test_exactly_at_limit_on_both_sides_succeeds_for_sync(tmp_path):
    api = simple_sftp_client.Api(simple_sftp_client.APP_VERSION)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _make_local_files(local_dir, LIMIT)
    sftp = _FakeSftp(_remote_tree(LIMIT))

    plan, transfers, conflicts = api._compute_sync(sftp, str(local_dir), "/", "download")

    assert plan is not None
    assert transfers is not None
    assert conflicts is not None


# ───────────── the full async job turns the refusal into a clean failure ─────────────

def test_run_compare_reports_entry_limit_as_a_normal_failure_not_a_crash(
        sftp_env, wait_for_compare):
    api, server_root, local_dir = sftp_env
    _make_local_files(local_dir, LIMIT + 1)

    r = api.compare(str(local_dir), "/")
    assert r["ok"] is True

    done = wait_for_compare(api, kind="compare")
    assert done["ok"] is False
    assert done["result"] is None
    assert "Too many files to compare" in done["error"]


def test_run_compare_sync_plan_reports_entry_limit_and_produces_no_transfers(
        sftp_env, wait_for_compare):
    api, server_root, local_dir = sftp_env
    _make_local_files(local_dir, LIMIT + 1)

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True

    done = wait_for_compare(api, kind="sync")
    assert done["ok"] is False
    assert done["result"] is None
    assert "Too many files to compare" in done["error"]
    assert "token" not in (done["result"] or {})
    assert done.get("transfers") is None


def test_run_compare_entry_has_no_result_and_no_transfers_after_refusal(sftp_env):
    """Drives _run_compare directly (synchronously, on this thread) so the
    finished entry can be inspected before poll_queue() delivers and
    deregisters it, to confirm a refusal leaves nothing partial behind."""
    api, server_root, local_dir = sftp_env
    _make_local_files(local_dir, LIMIT + 1)

    cid, stop_event = api._start_compare("sync")
    api._run_compare(cid, str(local_dir), "/", stop_event, direction="upload")

    with api._compare_lock:
        entry = api._compares[cid]
    assert entry["ok"] is False
    assert "Too many files to compare" in entry["error"]
    assert entry["result"] is None
    assert entry["transfers"] is None


def test_refusal_is_written_to_the_log(sftp_env):
    """Drives _run_compare directly (synchronously) and checks the console
    buffer before poll_queue() would drain it, since poll_queue() moves
    buffered lines into its own response and clears the buffer."""
    api, server_root, local_dir = sftp_env
    _make_local_files(local_dir, LIMIT + 1)

    cid, stop_event = api._start_compare("compare")
    api._run_compare(cid, str(local_dir), "/", stop_event)

    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("Too many files to compare" in m for m in messages)
