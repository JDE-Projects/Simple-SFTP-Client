"""Regression coverage for Windows-equivalent download destinations."""
import os
import stat
import threading
import time

from app.services.compare_sync import _stream_sync_transfers
from app.services.scanning import _iter_remote
from app.services.transfers import _download_claims
from app.transfer_queue import ACTIVE, FAILED, WAITING, TransferQueue


class _FakeAttr:
    def __init__(self, filename, is_dir=False, size=0, mtime=0):
        self.filename = filename
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG
        self.st_size = size
        self.st_mtime = mtime


class _FakeSftp:
    def __init__(self, tree):
        self.tree = tree
        self.listed = []

    def listdir_iter(self, path):
        self.listed.append(path)
        return iter(self.tree.get(path, []))


class _FakeApi:
    def __init__(self):
        self.logs = []

    def _rstat(self, _sftp, _path):
        return 0, 0

    def _worker_log(self, message, *args, **kwargs):
        self.logs.append(message)

    def _iter_remote(self, *args, **kwargs):
        return _iter_remote(self, *args, **kwargs)


def _remote(tree, root="L"):
    api = _FakeApi()
    sftp = _FakeSftp(tree)
    clashes = []
    got = list(_iter_remote(api, sftp, "/top", root, True, root, clashes=clashes))
    return api, sftp, got, clashes


def test_iter_remote_sibling_file_clash():
    api, _sftp, got, clashes = _remote({"/top": [_FakeAttr("Report.txt"), _FakeAttr("report.txt"), _FakeAttr("other.bin")]})
    assert [item[1] for item in got] == ["/top/Report.txt", "/top/other.bin"]
    assert clashes == [{"local_path": os.path.join("L", "report.txt"), "remote_path": "/top/report.txt", "other_remote": "/top/Report.txt", "is_dir": False}]
    assert any("same name on Windows as" in line for line in api.logs)


def test_iter_remote_folder_clash_skips_subtree():
    _api, sftp, got, clashes = _remote({"/top": [_FakeAttr("Docs", True), _FakeAttr("docs", True)], "/top/Docs": [_FakeAttr("a.bin")], "/top/docs": [_FakeAttr("b.bin")]})
    assert [item[1] for item in got] == ["/top/Docs/a.bin"]
    assert clashes[0]["remote_path"] == "/top/docs"
    assert "/top/docs" not in sftp.listed


def test_iter_remote_file_vs_folder_clash():
    _api, sftp, got, clashes = _remote({"/top": [_FakeAttr("docs"), _FakeAttr("Docs", True)], "/top/Docs": [_FakeAttr("never.bin")]})
    assert [item[1] for item in got] == ["/top/docs"]
    assert clashes[0]["is_dir"] is True
    assert "/top/Docs" not in sftp.listed


def test_iter_remote_unicode_forms_do_not_clash():
    _api, _sftp, got, clashes = _remote({"/top": [_FakeAttr("caf\u00e9.txt"), _FakeAttr("cafe\u0301.txt")]})
    assert len(got) == 2
    assert clashes == []


def _admit(q, local, remote, is_dir=False, root="L"):
    return q.admit("download", local, remote, os.path.basename(local), 0, "overwrite", is_dir,
                   _download_claims(local, remote, is_dir, root))


def test_admit_conflicting_source_becomes_failed_row():
    q = TransferQueue()
    _admit(q, "L/Report.txt", "/a/Report.txt")
    second, other = _admit(q, "L/report.txt", "/b/report.txt")
    row = next(item for item in q.snapshot() if item["id"] == second)
    assert other == "/a/Report.txt"
    assert row["state"] == FAILED and row["retryable"] is False
    assert "/a/Report.txt" in row["error"]
    assert q.counts()[FAILED] == 1


def test_admit_same_source_twice_is_allowed():
    q = TransferQueue()
    _admit(q, "L/Report.txt", "/a/Report.txt")
    _admit(q, "L/Report.txt", "/a/Report.txt")
    assert [item["state"] for item in q.snapshot()] == [WAITING, WAITING]


def test_admit_folder_prefix_clash():
    q = TransferQueue()
    _admit(q, "L/Docs/a.bin", "/r/Docs/a.bin")
    _id, other = _admit(q, "L/docs/b.bin", "/r/docs/b.bin")
    assert other == "/r/Docs"


def test_claims_release_on_completion():
    q = TransferQueue()
    item_id, _ = _admit(q, "L/Report.txt", "/a/Report.txt")
    assert q.claim().id == item_id
    q.mark_completed(item_id)
    _id, other = _admit(q, "L/report.txt", "/b/report.txt")
    assert other is None


def test_claims_release_on_waiting_cancel_cancel_all_and_fail_waiting():
    for release in (lambda q, item_id: q.cancel(item_id),
                    lambda q, _item_id: q.cancel_all(),
                    lambda q, _item_id: q.fail_waiting("offline")):
        q = TransferQueue()
        item_id, _ = _admit(q, "L/Report.txt", "/a/Report.txt")
        release(q, item_id)
        _id, other = _admit(q, "L/report.txt", "/b/report.txt")
        assert other is None


def test_active_cancel_keeps_claim_until_finished():
    q = TransferQueue()
    item_id, _ = _admit(q, "L/Report.txt", "/a/Report.txt")
    assert q.claim().state == ACTIVE
    q.cancel(item_id)
    _id, other = _admit(q, "L/report.txt", "/b/report.txt")
    assert other == "/a/Report.txt"
    q.mark_cancelled(item_id)
    _id, other = _admit(q, "L/report.txt", "/b/report.txt")
    assert other is None


def test_clash_rows_are_not_retryable():
    q = TransferQueue()
    q.add_clash("download", "L/report.txt", "/b/report.txt", "report.txt", "/a/Report.txt", False)
    item_id = q.snapshot()[0]["id"]
    assert q.requeue(item_id) is False
    assert q.retry_all_failed() == 0
    assert q.snapshot()[0]["state"] == FAILED


def test_retry_rechecks_claims():
    q = TransferQueue()
    item_id, _ = _admit(q, "L/Report.txt", "/a/Report.txt")
    q.claim()
    q.mark_failed(item_id, "network")
    holder_id, _ = _admit(q, "L/report.txt", "/b/report.txt")
    assert q.requeue(item_id) is False
    assert "/b/report.txt" in q.snapshot()[0]["error"]
    assert q.retry_all_failed() == 0
    q.claim()
    q.mark_completed(holder_id)
    assert q.requeue(item_id) is True
    _id, other = _admit(q, "L/report.txt", "/b/report.txt")
    assert other == "/a/Report.txt"


def test_retry_all_failed_requeues_once_claim_is_free():
    q = TransferQueue()
    item_id, _ = _admit(q, "L/Report.txt", "/a/Report.txt")
    q.claim()
    q.mark_failed(item_id, "network")
    holder_id, _ = _admit(q, "L/report.txt", "/b/report.txt")
    assert q.retry_all_failed() == 0
    q.claim()
    q.mark_completed(holder_id)
    assert q.retry_all_failed() == 1
    _id, other = _admit(q, "L/report.txt", "/b/report.txt")
    assert other == "/a/Report.txt"


def test_clear_finished_does_not_release_a_live_claim_twice():
    # Two queued copies of the same source share one claim (count 2). After
    # one finishes and is cleared, the other still holds the name.
    q = TransferQueue()
    first_id, _ = _admit(q, "L/Report.txt", "/a/Report.txt")
    _admit(q, "L/Report.txt", "/a/Report.txt")
    q.claim()
    q.mark_completed(first_id)
    q.clear_finished()
    _id, other = _admit(q, "L/report.txt", "/b/report.txt")
    assert other == "/a/Report.txt"


def test_snapshot_reports_retryable():
    q = TransferQueue()
    q.append("upload", "L/file", "/file", "file")
    assert q.snapshot()[0]["retryable"] is True


def test_concurrent_admits_admit_one_source_per_key():
    q = TransferQueue()
    threads = [threading.Thread(target=_admit, args=(q, "L/Report.txt", f"/{n}/report.txt")) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [item["state"] for item in q.snapshot()].count(WAITING) == 1
    assert [item["state"] for item in q.snapshot()].count(FAILED) == 7


def _wait_scan(api):
    deadline = time.time() + 15
    while time.time() < deadline:
        if not api._scan_active():
            return
        time.sleep(.02)
    raise AssertionError("scan did not finish")


def test_enqueue_across_requests_clash(sftp_env, wait_for_drain, monkeypatch):
    api, server_root, local_dir = sftp_env
    (server_root / "one").mkdir()
    (server_root / "two").mkdir()
    (server_root / "one" / "Report.txt").write_bytes(b"A")
    (server_root / "two" / "report.txt").write_bytes(b"B")
    api._queue.pause()
    monkeypatch.setattr(api, "_scan_wait_for_room", lambda _stop: None)
    api.enqueue([{"name": "Report.txt"}], "download", str(local_dir), "/one")
    _wait_scan(api)
    api.enqueue([{"name": "report.txt"}], "download", str(local_dir), "/two")
    _wait_scan(api)
    api._queue.resume()
    api._ensure_worker()
    wait_for_drain(api)
    rows = {row["name"]: row for row in api._queue.snapshot()}
    assert (local_dir / "Report.txt").read_bytes() == b"A"
    assert rows["report.txt"]["state"] == FAILED and rows["report.txt"]["retryable"] is False
    assert api.retry_item(rows["report.txt"]["id"])["ok"] is False


def test_enqueue_top_level_selection_clash(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (server_root / "Report.txt").write_bytes(b"A")
    api.enqueue([{"name": "Report.txt"}, {"name": "report.txt"}], "download", str(local_dir), "/")
    wait_for_drain(api)
    rows = {row["name"]: row for row in api._queue.snapshot()}
    assert rows["Report.txt"]["state"] == "completed"
    assert (local_dir / "Report.txt").read_bytes() == b"A"
    assert rows["report.txt"]["state"] == FAILED and rows["report.txt"]["retryable"] is False
    assert any("1 name clash" in line["msg"] for line in api.poll_queue()["console"])


def test_sync_stream_admits_through_claims(sftp_env, monkeypatch):
    api, _server_root, local_dir = sftp_env
    api._queue.pause()
    monkeypatch.setattr(api, "_scan_wait_for_room", lambda _stop: None)
    scan_id, stop = api._register_scan()
    transfers = [(str(local_dir / "Report.txt"), "/one/Report.txt", 1, False),
                 (str(local_dir / "report.txt"), "/two/report.txt", 1, False)]
    _stream_sync_transfers(api, transfers, "download", "overwrite", scan_id, stop, 1, str(local_dir))
    rows = api._queue.snapshot()
    assert rows[0]["state"] == WAITING
    assert rows[1]["state"] == FAILED and rows[1]["retryable"] is False
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("Sync: 1 item(s) queued, 1 name clash(es) not downloaded" in m for m in messages)
