"""
Tests for the streaming background scanner (Stage 2a): enqueue()/upload_paths()
start a daemon thread that streams files into the queue as they are found,
instead of walking the whole tree up front. Runs against the same in-process
paramiko SFTP server and sftp_env/wait_for_drain fixtures as
test_queue_integration.py.
"""
import os
import stat
import time

import simple_sftp_client
from transfer_queue import COMPLETED


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
    remote path, so remote-side behavior (confinement, skips) can be tested
    without a real server producing hostile filenames."""
    def __init__(self, tree):
        self.tree = tree  # {remote_path: [attrs]}

    def listdir_iter(self, rp):
        return iter(self.tree.get(rp, []))


def test_nested_remote_folder_downloads_every_file_with_correct_bytes(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    # a few levels of nesting, with files at every level, not just the leaves
    (server_root / "top").mkdir()
    (server_root / "top" / "a.bin").write_bytes(os.urandom(1000))
    (server_root / "top" / "mid").mkdir()
    (server_root / "top" / "mid" / "b.bin").write_bytes(os.urandom(2000))
    (server_root / "top" / "mid" / "deep").mkdir()
    (server_root / "top" / "mid" / "deep" / "c.bin").write_bytes(os.urandom(3000))

    result = api.enqueue([{"name": "top", "is_dir": True}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["scanning"] is True

    wait_for_drain(api)

    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    assert states["a.bin"] == COMPLETED
    assert states["b.bin"] == COMPLETED
    assert states["c.bin"] == COMPLETED
    assert (local_dir / "top" / "a.bin").read_bytes() == \
        (server_root / "top" / "a.bin").read_bytes()
    assert (local_dir / "top" / "mid" / "b.bin").read_bytes() == \
        (server_root / "top" / "mid" / "b.bin").read_bytes()
    assert (local_dir / "top" / "mid" / "deep" / "c.bin").read_bytes() == \
        (server_root / "top" / "mid" / "deep" / "c.bin").read_bytes()
    # every file actually arrived, not just empty directory shells
    assert (local_dir / "top" / "mid" / "deep" / "c.bin").stat().st_size == 3000


def test_nested_local_folder_uploads_creating_remote_dirs_lazily(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "top").mkdir()
    (local_dir / "top" / "a.bin").write_bytes(os.urandom(500))
    (local_dir / "top" / "mid").mkdir()
    (local_dir / "top" / "mid" / "b.bin").write_bytes(os.urandom(700))
    (local_dir / "top" / "mid" / "deep").mkdir()
    (local_dir / "top" / "mid" / "deep" / "c.bin").write_bytes(os.urandom(900))

    # nothing exists on the remote side yet: no pre-created folder shells
    assert not (server_root / "top").exists()

    result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["scanning"] is True

    wait_for_drain(api)

    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    assert states["a.bin"] == COMPLETED
    assert states["b.bin"] == COMPLETED
    assert states["c.bin"] == COMPLETED
    assert (server_root / "top" / "a.bin").read_bytes() == \
        (local_dir / "top" / "a.bin").read_bytes()
    assert (server_root / "top" / "mid" / "b.bin").read_bytes() == \
        (local_dir / "top" / "mid" / "b.bin").read_bytes()
    assert (server_root / "top" / "mid" / "deep" / "c.bin").read_bytes() == \
        (local_dir / "top" / "mid" / "deep" / "c.bin").read_bytes()


def test_poll_queue_reports_scanning_while_running_then_false_once_drained(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "top").mkdir()
    for i in range(20):
        (local_dir / "top" / f"f{i}.bin").write_bytes(os.urandom(256))

    result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    # scanning should show True at some point before the batch drains (a tiny
    # window given 20 small files, so poll promptly and tolerate a miss by
    # also accepting the scan_found count moving)
    saw_scanning = False
    deadline = time.time() + 5
    while time.time() < deadline:
        status = api.poll_queue()
        if status["scanning"]:
            saw_scanning = True
            break
        if not api._scan_active() and api.queue.pending() == 0 and len(api.queue.snapshot()) == 20:
            break
        time.sleep(0.005)
    assert saw_scanning, "poll_queue never reported scanning=True while the scan ran"

    wait_for_drain(api)

    status = api.poll_queue()
    assert status["scanning"] is False
    assert status["counts"]["completed"] == 20


def test_backpressure_holds_queue_waiting_at_the_high_water_mark(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    # a tiny high-water so the cap is observable without creating thousands
    # of real files
    monkeypatch.setattr(simple_sftp_client, "SCAN_QUEUE_HIGH_WATER", 5)
    # nothing drains the queue at all, so any growth past the high-water can
    # only be explained by backpressure not holding
    monkeypatch.setattr(api, "_ensure_worker", lambda: None)

    (local_dir / "top").mkdir()
    names = [f"f{i}.bin" for i in range(40)]
    for name in names:
        (local_dir / "top" / name).write_bytes(os.urandom(16))

    try:
        result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                              str(local_dir), "/", "overwrite")
        assert result["ok"] is True

        # watch waiting() for a bit while the scan tries to run ahead; with
        # nothing draining, it must stop right at the high-water mark instead
        # of queuing all 40 files
        max_seen = 0
        deadline = time.time() + 2
        while time.time() < deadline:
            max_seen = max(max_seen, api.queue.waiting())
            time.sleep(0.01)

        assert max_seen <= 5, f"queue.waiting() reached {max_seen}, expected it held at the high-water mark"
        assert max_seen > 0, "expected at least the first batch to have been queued"
    finally:
        # stop the scan so it does not keep spinning after the test ends
        api._stop_all_scans()
        deadline = time.time() + 15
        while time.time() < deadline:
            if not api._scan_active():
                break
            time.sleep(0.02)


def test_iter_local_yields_every_file_in_a_nested_tree_in_any_order(tmp_path):
    # _iter_local no longer sorts each directory level, so its order is
    # arbitrary; compare the results as a set instead of a list.
    api = simple_sftp_client.Api()
    root = tmp_path / "top"
    root.mkdir()
    (root / "a.bin").write_bytes(b"a" * 10)
    (root / "b.bin").write_bytes(b"b" * 20)
    (root / "mid").mkdir()
    (root / "mid" / "c.bin").write_bytes(b"c" * 30)
    (root / "mid" / "deep").mkdir()
    (root / "mid" / "deep" / "d.bin").write_bytes(b"d" * 40)
    # in-progress transfer scratch file must stay hidden
    (root / "e.bin.sxtpart").write_bytes(b"e" * 50)
    # distinct, known mtimes so the 4th field can be asserted exactly, not
    # just type-checked
    stamps = {
        root / "a.bin": 1_700_000_001,
        root / "b.bin": 1_700_000_002,
        root / "mid" / "c.bin": 1_700_000_003,
        root / "mid" / "deep" / "d.bin": 1_700_000_004,
    }
    for path, ts in stamps.items():
        os.utime(path, (ts, ts))

    results = set(api._iter_local(str(root), "/top", True))

    assert results == {
        (str(root / "a.bin"), "/top/a.bin", 10, stamps[root / "a.bin"]),
        (str(root / "b.bin"), "/top/b.bin", 20, stamps[root / "b.bin"]),
        (str(root / "mid" / "c.bin"), "/top/mid/c.bin", 30, stamps[root / "mid" / "c.bin"]),
        (str(root / "mid" / "deep" / "d.bin"), "/top/mid/deep/d.bin", 40,
         stamps[root / "mid" / "deep" / "d.bin"]),
    }


def test_iter_local_logs_and_stops_on_unlistable_dir(tmp_path):
    api = simple_sftp_client.Api()
    missing = tmp_path / "does_not_exist"

    results = list(api._iter_local(str(missing), "/top", True))

    assert results == []
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("could not list" in m for m in messages)


def test_iter_remote_yields_every_file_in_a_nested_tree_in_any_order(tmp_path):
    api = simple_sftp_client.Api()
    root = str(tmp_path)
    sftp = _FakeSftp({
        "/top": [
            _FakeAttr("a.bin", size=10, mtime=1_700_000_001),
            _FakeAttr("b.bin", size=20, mtime=1_700_000_002),
            _FakeAttr("mid", is_dir=True),
        ],
        "/top/mid": [
            _FakeAttr("c.bin", size=30, mtime=1_700_000_003),
            _FakeAttr("deep", is_dir=True),
        ],
        "/top/mid/deep": [
            _FakeAttr("d.bin", size=40, mtime=1_700_000_004),
        ],
    })

    results = set(api._iter_remote(sftp, "/top", str(tmp_path / "top"), True, root))

    assert results == {
        (str(tmp_path / "top" / "a.bin"), "/top/a.bin", 10, 1_700_000_001),
        (str(tmp_path / "top" / "b.bin"), "/top/b.bin", 20, 1_700_000_002),
        (str(tmp_path / "top" / "mid" / "c.bin"), "/top/mid/c.bin", 30, 1_700_000_003),
        (str(tmp_path / "top" / "mid" / "deep" / "d.bin"), "/top/mid/deep/d.bin", 40, 1_700_000_004),
    }


def test_iter_remote_skips_temp_parts_and_confines_hostile_names(tmp_path):
    api = simple_sftp_client.Api()
    root = str(tmp_path)
    sftp = _FakeSftp({
        "/top": [
            _FakeAttr("good.bin", size=5, mtime=1_700_000_005),
            _FakeAttr("upload.bin.sxtpart", size=5),
            _FakeAttr("../escape.bin", size=5),
        ],
    })

    results = list(api._iter_remote(sftp, "/top", str(tmp_path / "top"), True, root))

    assert results == [(str(tmp_path / "top" / "good.bin"), "/top/good.bin", 5, 1_700_000_005)]
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("unsafe remote name" in m for m in messages)


def test_iter_remote_logs_and_stops_when_listing_raises():
    api = simple_sftp_client.Api()

    class RaisingSftp:
        def listdir_iter(self, rp):
            def gen():
                raise OSError("boom")
                yield  # pragma: no cover - never reached, makes this a generator
            return gen()

    results = list(api._iter_remote(RaisingSftp(), "/top", "/local/top", True, "/local"))

    assert results == []
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("could not list" in m for m in messages)
