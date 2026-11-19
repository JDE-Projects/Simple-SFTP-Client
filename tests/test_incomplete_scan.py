"""Regression coverage for incomplete ordinary transfer scans."""
import os
import time

from app.transfer_queue import FAILED


def _wait_scan(api, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not api._scan_active() and api._queue.pending() == 0:
            return
        time.sleep(.01)
    raise AssertionError("scan did not finish")


def _summary(api):
    rows = [row for row in api._queue.snapshot() if row["name"].startswith("Scan ")]
    lines = [line for line in api.poll_queue()["console"] if line["msg"].startswith("Scan ")]
    assert len(rows) == len(lines) == 1
    return rows[0], lines[0]


def test_local_unreadable_subfolder(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    root = local / "top"
    root.mkdir()
    (root / "good.txt").write_text("ok")
    denied = root / "denied"
    denied.mkdir()
    real_scandir = os.scandir
    monkeypatch.setattr("app.services.scanning.os.scandir",
                        lambda path: (_ for _ in ()).throw(PermissionError("denied"))
                        if os.fspath(path) == str(denied) else real_scandir(path))

    assert api.enqueue([{"name": "top", "is_dir": True}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    row, line = _summary(api)
    assert row["state"] == FAILED and row["retryable"] is False
    assert line["msg"].startswith("Scan incomplete") and line["scan_report"] == row["scan_report"]
    report = api.get_scan_problems(row["scan_report"])
    assert report["ok"] and any(str(denied) in item for item in report["problems"])


def test_remote_listing_failure(sftp_env, monkeypatch):
    api, server, local = sftp_env
    (server / "top" / "denied").mkdir(parents=True)
    (server / "top" / "good.txt").write_text("ok")
    real_open = api._client.open_sftp

    def open_sftp():
        sftp = real_open()
        real_list = sftp.listdir_iter
        sftp.listdir_iter = lambda path: (_ for _ in ()).throw(OSError("denied")) if path == "/top/denied" else real_list(path)
        return sftp

    monkeypatch.setattr(api._client, "open_sftp", open_sftp)
    assert api.enqueue([{"name": "top", "is_dir": True}], "download", str(local), "/")["ok"]
    _wait_scan(api)
    row, _line = _summary(api)
    assert api.get_scan_problems(row["scan_report"])["ok"]


class _EntryStatFails:
    """A scandir entry whose metadata read fails, standing in for one the
    walker cannot stat."""

    def __init__(self, entry):
        self._entry = entry
        self.name = entry.name
        self.path = entry.path

    def stat(self, follow_symlinks=True):
        raise PermissionError("metadata")

    def is_dir(self, follow_symlinks=True):
        return self._entry.is_dir(follow_symlinks=follow_symlinks)


class _ScandirWrap:
    def __init__(self, it, bad_name):
        self._it = it
        self._bad = bad_name

    def __enter__(self):
        self._it.__enter__()
        return self

    def __exit__(self, *exc):
        return self._it.__exit__(*exc)

    def __iter__(self):
        for entry in self._it:
            yield _EntryStatFails(entry) if entry.name == self._bad else entry


def test_local_entry_metadata_failure(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    root = local / "top"
    root.mkdir()
    (root / "good.txt").write_text("ok")
    (root / "bad.txt").write_text("no")
    real_scandir = os.scandir
    monkeypatch.setattr("app.services.scanning.os.scandir",
                        lambda path: _ScandirWrap(real_scandir(path), "bad.txt")
                        if os.fspath(path) == str(root) else real_scandir(path))
    assert api.enqueue([{"name": "top", "is_dir": True}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    row, _line = _summary(api)
    names = [r["name"] for r in api._queue.snapshot() if "scan_report" not in r]
    assert "bad.txt" not in names and "good.txt" in names
    assert any("bad.txt" in p for p in api.get_scan_problems(row["scan_report"])["problems"])


def _no_summary(api):
    rows = [row for row in api._queue.snapshot() if row["name"].startswith("Scan ")]
    lines = [line["msg"] for line in api.poll_queue()["console"]]
    assert rows == []
    assert not any(m.startswith(("Scan incomplete", "Scan halted")) for m in lines)
    return lines


def test_local_single_file_root_metadata_failure_still_queued(sftp_env, monkeypatch):
    """A file picked by name is queued even when its size cannot be read,
    so the transfer reports on its own retryable row, not as a scan problem."""
    api, _server, local = sftp_env
    path = local / "bad.txt"
    path.write_text("no")
    real_lstat = os.lstat
    calls = 0

    def fail_third(path_arg):
        # 1: enqueue's link check, 2: the walker's root link check,
        # 3: the walker's size read, the one under test.
        nonlocal calls
        if os.fspath(path_arg) == str(path):
            calls += 1
            if calls == 3:
                raise PermissionError("metadata")
        return real_lstat(path_arg)

    monkeypatch.setattr("app.services.scanning.os.lstat", fail_third)
    assert api.enqueue([{"name": "bad.txt"}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    assert calls >= 3
    _no_summary(api)
    assert [r["name"] for r in api._queue.snapshot()] == ["bad.txt"]


def test_remote_single_file_root_stat_failure_still_queued(sftp_env, monkeypatch):
    """A remote file picked by name is queued even when the server will not
    describe it, so the download fails on its own retryable row."""
    api, server, local = sftp_env
    (server / "bad.txt").write_text("no")
    real_open = api._client.open_sftp

    def open_sftp():
        sftp = real_open()
        sftp.stat = lambda _path: (_ for _ in ()).throw(OSError("actual stat error"))
        return sftp

    monkeypatch.setattr(api._client, "open_sftp", open_sftp)
    assert api.enqueue([{"name": "bad.txt"}], "download", str(local), "/")["ok"]
    _wait_scan(api)
    _no_summary(api)
    assert [r["name"] for r in api._queue.snapshot()] == ["bad.txt"]


def test_connection_lost_midscan(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    (local / "top").mkdir()
    monkeypatch.setattr(api, "_iter_local", lambda *a, **k: iter(()))
    scan_id, stop = api._register_scan()
    api._report_dead_connection()
    api._scan_and_queue([], "upload", "overwrite", scan_id, stop)
    row, line = _summary(api)
    assert row["name"] == "Scan halted" and line["msg"].startswith("Scan halted")


def test_user_stop_no_row(sftp_env):
    api, _server, _local = sftp_env
    scan_id, stop = api._register_scan()
    api._stop_all_scans("stopped")
    api._scan_and_queue([], "upload", "overwrite", scan_id, stop)
    assert not api._queue.snapshot()
    assert any("Scan stopped" in line["msg"] for line in api.poll_queue()["console"])


def test_shutdown_no_row(sftp_env):
    api, _server, _local = sftp_env
    scan_id, stop = api._register_scan()
    api._stop_all_scans("shutdown")
    api._scan_and_queue([], "upload", "overwrite", scan_id, stop)
    assert not api._queue.snapshot()


def test_clear_finished_during_scan_keeps_summary(sftp_env):
    api, _server, _local = sftp_env
    scan_id, stop = api._register_scan()
    api._queue.clear_finished()
    api._scan_and_queue([], "upload", "overwrite", scan_id, stop, initial_problems=["denied"])
    assert _summary(api)[0]["name"] == "Scan incomplete"


def test_session_open_failure_reports(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    monkeypatch.setattr(api._client, "open_sftp", lambda: (_ for _ in ()).throw(OSError("no session")))
    assert api.enqueue([{"name": "x"}], "download", str(local), "/")["ok"]
    _wait_scan(api)
    assert "no session" in api.get_scan_problems(_summary(api)[0]["scan_report"])["problems"][0]


def test_exception_after_partial_progress(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    (local / "top").mkdir()
    def broken(*_args, **_kwargs):
        yield (str(local / "good"), "/good", 1, 0, False)
        raise RuntimeError("boom")
    monkeypatch.setattr(api, "_iter_local", broken)
    assert api.enqueue([{"name": "top", "is_dir": True}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    row, _line = _summary(api)
    assert "boom" in api.get_scan_problems(row["scan_report"])["problems"][0]


def test_upload_root_read_failure_counted(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    (local / "good.txt").write_text("ok")
    real_kind = __import__("app.services.transfers", fromlist=["local_link_kind"]).local_link_kind
    monkeypatch.setattr("app.services.transfers.local_link_kind",
                        lambda path: (_ for _ in ()).throw(PermissionError("root denied"))
                        if os.fspath(path).endswith("bad.txt") else real_kind(path))
    assert api.enqueue([{"name": "good.txt"}, {"name": "bad.txt"}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    assert "root denied" in api.get_scan_problems(_summary(api)[0]["scan_report"])["problems"][0]
    assert api.upload_paths([str(local / "bad.txt")], "/")["ok"] is False


def test_report_store_cap(sftp_env):
    api, _server, _local = sftp_env
    ids = []
    for number in range(11):
        scan_id, stop = api._register_scan()
        api._scan_and_queue([], "upload", "overwrite", scan_id, stop,
                            initial_problems=[f"problem {number}"])
        ids.append(scan_id)
    assert api.get_scan_problems(ids[0])["ok"] is False
    assert api.get_scan_problems(ids[-1])["ok"] is True
    from app.services.scanning import ScanProblems, _store_scan_report
    scan_id, _stop = api._register_scan()
    problems = ScanProblems(api, scan_id)
    for number in range(10_001):
        problems.append(f"many {number}")
    _store_scan_report(api, scan_id, "upload", problems)
    api._deregister_scan(scan_id)
    report = api.get_scan_problems(scan_id)
    assert len(report["problems"]) == 10_000 and report["more"] == 1


def test_stop_promptly_on_many_failures(sftp_env, monkeypatch):
    api, _server, local = sftp_env
    root = local / "top"
    root.mkdir()
    for number in range(20):
        (root / f"bad{number}").mkdir()
    seen = []
    real_scandir = os.scandir
    def denied(path):
        if os.fspath(path).startswith(str(root / "bad")):
            seen.append(path)
            api._stop_all_scans("stopped")
            raise PermissionError("denied")
        return real_scandir(path)
    monkeypatch.setattr("app.services.scanning.os.scandir", denied)
    assert api.enqueue([{"name": "top", "is_dir": True}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    assert len(seen) == 1


def test_poll_scan_not_read(sftp_env):
    api, _server, _local = sftp_env
    scan_id, stop = api._register_scan()
    from app.services.scanning import ScanProblems
    problems = ScanProblems(api, scan_id)
    problems.append("denied")
    assert api.poll_queue()["scan_not_read"] == 1
    api._stop_all_scans("stopped")
    api._scan_and_queue([], "upload", "overwrite", scan_id, stop)


def test_clean_scan_unchanged(sftp_env):
    api, _server, _local = sftp_env
    scan_id, stop = api._register_scan()
    api._scan_and_queue([], "upload", "overwrite", scan_id, stop)
    assert not api._queue.snapshot()
    assert any(line["msg"] == "Scan complete: nothing to transfer" for line in api.poll_queue()["console"])

def test_permission_failure_wording_is_about_reading(sftp_env, monkeypatch):
    """A folder that cannot be read is described as a read failure, never
    with the shared write advice ("Choose a location you can write to")."""
    api, _server, local = sftp_env
    root = local / "top"
    root.mkdir()
    denied = root / "denied"
    denied.mkdir()
    real_scandir = os.scandir
    monkeypatch.setattr("app.services.scanning.os.scandir",
                        lambda path: (_ for _ in ()).throw(PermissionError(13, "Access is denied"))
                        if os.fspath(path) == str(denied) else real_scandir(path))
    assert api.enqueue([{"name": "top", "is_dir": True}], "upload", str(local), "/")["ok"]
    _wait_scan(api)
    row, _line = _summary(api)
    problems = api.get_scan_problems(row["scan_report"])["problems"]
    assert problems == [f"could not list {denied}: permission denied (no access to read it)"]
