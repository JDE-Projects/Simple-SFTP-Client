"""
Tests that Compare and Sync refuse (report failure) whenever the tree walk
behind them was incomplete: a folder that could not be listed, a file whose
metadata could not be read, or a lost connection mid-listing. Before this
change, _compute_pair_maps had no way to tell a caller that part of the tree
was silently skipped, so _compute_compare/_compute_sync could build a result
or a transfer plan from a tree that was not fully seen. An unsafe remote name
is the exception: it can never be represented locally, so it is skipped and
logged (as the transfer scan does), not treated as an incomplete scan.

The ordinary transfer scan (_scan_and_queue / _iter_local / _iter_remote with
no problems collector) is untouched: see test_streaming_scan.py, which still
asserts the old log-and-stop/skip-and-continue behavior unchanged.

Most cases here call the pure recursive cores (_compute_compare/
_compute_sync) directly against a real local tmp_path folder and a fake sftp
object, the same style as test_streaming_scan.py's _FakeSftp/RaisingSftp. Two
cases go through the full async job (compare()/sync_plan() + poll_queue())
against the real in-process sftp server from conftest.py, to prove the
background job (_run_compare) turns ScanIncomplete into a normal failed
result instead of an unhandled exception.
"""
import os
import stat

import pytest

import simple_sftp_client


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
    remote path; a path missing from tree reads as empty (a normal, readable,
    empty folder), not a failure."""
    def __init__(self, tree):
        self.tree = tree  # {remote_path: [attrs]}

    def listdir_iter(self, rp):
        return iter(self.tree.get(rp, []))


class _RaisingSftp:
    """Every listdir_iter() call raises immediately, simulating a folder (or
    the root) that cannot be listed at all."""
    def __init__(self, exc=None):
        self.exc = exc or OSError("boom")

    def listdir_iter(self, rp):
        def gen():
            raise self.exc
            yield  # pragma: no cover - never reached, makes this a generator
        return gen()


class _PartialFailSftp:
    """Lists every path normally except one chosen path, which fails --
    simulating a single unreadable subfolder mid-tree, or (with a path that
    yields a couple of entries before raising) a connection lost partway
    through a listing."""
    def __init__(self, tree, fail_path, exc=None):
        self.tree = tree
        self.fail_path = fail_path
        self.exc = exc or OSError("boom")

    def listdir_iter(self, rp):
        if rp == self.fail_path:
            def gen():
                raise self.exc
                yield  # pragma: no cover
            return gen()
        return iter(self.tree.get(rp, []))


def _unreadable_local_scandir(monkeypatch, bad_path):
    """Monkeypatch os.scandir so it raises OSError for exactly bad_path and
    behaves normally everywhere else, simulating a nested local subfolder
    that lost its read permission (permission bits alone are not reliably
    enforceable cross-platform, so this stands in for that)."""
    real_scandir = os.scandir
    bad_abs = os.path.abspath(bad_path)

    def fake_scandir(path):
        if os.path.abspath(path) == bad_abs:
            raise OSError("permission denied (test)")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)


# ───────────── _compute_compare / _compute_sync refuse on an incomplete scan ─────────────

def test_unreadable_local_root_refuses_compare(tmp_path):
    api = simple_sftp_client.Api()
    missing = tmp_path / "does_not_exist"
    sftp = _FakeSftp({})

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_compare(sftp, str(missing), "/")


def test_unreadable_local_root_refuses_sync(tmp_path):
    api = simple_sftp_client.Api()
    missing = tmp_path / "does_not_exist"
    sftp = _FakeSftp({})

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_sync(sftp, str(missing), "/", "upload")


def test_unreadable_remote_root_refuses_compare(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "kept.txt").write_bytes(b"hello")
    sftp = _RaisingSftp()

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_compare(sftp, str(local_dir), "/")


def test_unreadable_remote_root_refuses_sync(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    sftp = _RaisingSftp()

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_sync(sftp, str(local_dir), "/", "download")


def test_unreadable_nested_local_subfolder_refuses_compare(tmp_path, monkeypatch):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "kept.txt").write_bytes(b"hello")
    bad = local_dir / "bad"
    bad.mkdir()
    (bad / "hidden.txt").write_bytes(b"secret")
    _unreadable_local_scandir(monkeypatch, str(bad))
    sftp = _FakeSftp({})

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_compare(sftp, str(local_dir), "/")


def test_unreadable_nested_local_subfolder_refuses_sync(tmp_path, monkeypatch):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    bad = local_dir / "bad"
    bad.mkdir()
    (bad / "hidden.txt").write_bytes(b"secret")
    _unreadable_local_scandir(monkeypatch, str(bad))
    sftp = _FakeSftp({})

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_sync(sftp, str(local_dir), "/", "upload")


def test_unreadable_nested_remote_subfolder_refuses_compare(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    sftp = _PartialFailSftp(
        tree={"/": [_FakeAttr("good.txt", size=5), _FakeAttr("bad", is_dir=True)]},
        fail_path="/bad",
    )

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_compare(sftp, str(local_dir), "/")


def test_unreadable_nested_remote_subfolder_refuses_sync(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    sftp = _PartialFailSftp(
        tree={"/": [_FakeAttr("good.txt", size=5), _FakeAttr("bad", is_dir=True)]},
        fail_path="/bad",
    )

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_sync(sftp, str(local_dir), "/", "download")


def test_server_loss_mid_listing_refuses_compare(tmp_path):
    """A listing that yields a couple of entries and then raises, as a
    dropped connection partway through a READDIR would look."""
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    class _MidListingLossSftp:
        def listdir_iter(self, rp):
            def gen():
                yield _FakeAttr("first.txt", size=1)
                raise EOFError("connection lost")
            return gen()

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_compare(_MidListingLossSftp(), str(local_dir), "/")


def test_server_loss_mid_listing_refuses_sync(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    class _MidListingLossSftp:
        def listdir_iter(self, rp):
            def gen():
                yield _FakeAttr("first.txt", size=1)
                raise EOFError("connection lost")
            return gen()

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_sync(_MidListingLossSftp(), str(local_dir), "/", "download")


def test_unsafe_remote_name_is_skipped_not_refused_compare(tmp_path):
    # An unsafe remote name can never be represented locally, so compare
    # skips and logs it (like the transfer scan) rather than refusing the
    # whole folder. Every other file still compares normally.
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    sftp = _FakeSftp({
        "/": [_FakeAttr("good.bin", size=5), _FakeAttr("../escape.bin", size=5)],
    })

    data = api._compute_compare(sftp, str(local_dir), "/")

    assert data is not None
    assert "good.bin" in data["files"]
    assert "../escape.bin" not in data["files"]
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("unsafe remote name" in m for m in messages)


def test_unsafe_remote_name_is_skipped_not_refused_sync(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    sftp = _FakeSftp({
        "/": [_FakeAttr("good.bin", size=5), _FakeAttr("../escape.bin", size=5)],
    })

    plan, _transfers = api._compute_sync(sftp, str(local_dir), "/", "download")

    assert plan is not None
    names = {p["name"] for p in plan}
    assert "good.bin" in names
    assert "../escape.bin" not in names
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("unsafe remote name" in m for m in messages)


class _FakeEntry:
    """Minimal os.DirEntry stand-in: a file whose stat() raises, to simulate
    a file that vanished or lost read permission between scandir and stat."""
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)

    def is_dir(self, follow_symlinks=True):
        return False

    def stat(self, follow_symlinks=True):
        raise OSError("permission denied (test)")


def _stat_failing_scandir(monkeypatch, folder, bad_name):
    """Make os.scandir(folder) yield one entry whose stat() raises."""
    class _Ctx:
        def __enter__(self):
            return iter([_FakeEntry(os.path.join(folder, bad_name))])

        def __exit__(self, *a):
            return False

    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.abspath(path) == os.path.abspath(folder):
            return _Ctx()
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)


def test_unreadable_file_metadata_refuses_compare(tmp_path, monkeypatch):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _stat_failing_scandir(monkeypatch, str(local_dir), "locked.txt")
    sftp = _FakeSftp({})

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_compare(sftp, str(local_dir), "/")


def test_unreadable_file_metadata_refuses_sync(tmp_path, monkeypatch):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _stat_failing_scandir(monkeypatch, str(local_dir), "locked.txt")
    sftp = _FakeSftp({})

    with pytest.raises(simple_sftp_client.ScanIncomplete):
        api._compute_sync(sftp, str(local_dir), "/", "upload")


# ───────────── cancellation still wins over an incomplete-scan refusal ─────────────

def test_cancellation_before_any_problem_check_returns_none_not_scan_incomplete(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    stop_event = _AlreadySetEvent()
    sftp = _RaisingSftp()

    # Must return the cancelled signal (None), never raise ScanIncomplete,
    # even though the remote walk also hit an unreadable root.
    data = api._compute_compare(sftp, str(local_dir), "/", stop_event=stop_event)
    assert data is None


def test_cancellation_before_any_problem_check_returns_none_for_sync(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    stop_event = _AlreadySetEvent()
    sftp = _RaisingSftp()

    plan, transfers = api._compute_sync(sftp, str(local_dir), "/", "download", stop_event=stop_event)
    assert plan is None
    assert transfers is None


class _AlreadySetEvent:
    """Minimal stand-in for threading.Event that reports set from the start,
    without needing a real walk to have progressed far enough to notice."""
    def is_set(self):
        return True


# ───────────── an empty-but-readable tree still succeeds normally ─────────────

def test_empty_readable_tree_succeeds_with_no_files_or_plan(tmp_path):
    api = simple_sftp_client.Api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    sftp = _FakeSftp({})

    data = api._compute_compare(sftp, str(local_dir), "/")
    assert data == {"files": {}, "folders": {}}

    plan, transfers = api._compute_sync(sftp, str(local_dir), "/", "upload")
    assert plan == []
    assert transfers == []


# ───────────── full async job: _run_compare turns ScanIncomplete into a clean failure ─────────────

def test_run_compare_reports_incomplete_scan_as_a_normal_failure_not_a_crash(
        sftp_env, wait_for_compare):
    api, _server_root, local_dir = sftp_env
    # Give the local side an unreadable root by pointing compare at a local
    # folder that does not exist.
    missing_local = local_dir / "does_not_exist"

    r = api.compare(str(missing_local), "/")
    assert r["ok"] is True

    done = wait_for_compare(api, kind="compare")
    assert done["ok"] is False
    assert done["result"] is None
    assert done["error"]
    assert "could not be read" in done["error"] or "could not list" in done["error"]


def test_run_compare_sync_plan_reports_incomplete_scan_and_produces_no_transfers(
        sftp_env, wait_for_compare):
    api, _server_root, local_dir = sftp_env
    missing_local = local_dir / "does_not_exist"

    r = api.sync_plan(str(missing_local), "/", "upload")
    assert r["ok"] is True

    done = wait_for_compare(api, kind="sync")
    assert done["ok"] is False
    assert done["result"] is None
    assert done["error"]
    # No token was handed back, so start_sync has nothing runnable to act on.
    assert "token" not in (done["result"] or {})
