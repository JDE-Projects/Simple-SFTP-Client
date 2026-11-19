"""Compare and sync report unsafe remote names and hold back Windows-name
clash groups so omitted remote entries cannot cause unsafe one-sided transfers."""

import os
import stat

import pytest

from app import constants
from app.api import Api
from app.errors import ScanIncomplete
from app.services.compare_sync import _stream_sync_transfers
from simple_sftp_client import APP_VERSION


class _FakeAttr:
    def __init__(self, filename, is_dir=False, size=0, mtime=0):
        self.filename = filename
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG
        self.st_size = size
        self.st_mtime = mtime


class _FakeSftp:
    def __init__(self, tree):
        self.tree = tree

    def listdir_iter(self, path):
        return iter(self.tree.get(path, []))

    def close(self):
        pass


class _FakeClient:
    def __init__(self, sftp):
        self.sftp = sftp

    def open_sftp(self):
        return self.sftp


def _api():
    return Api(APP_VERSION)


def _local(local_dir, name, data=b"x", mtime=10):
    path = local_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _sync(api, local_dir, tree, direction, changed_only=True):
    omissions = {"entries": [], "count": 0}
    plan, transfers, conflicts = api._compute_sync(
        _FakeSftp(tree), str(local_dir), "/", direction, changed_only,
        omissions=omissions)
    return plan, transfers, conflicts, omissions


def _names(plan):
    return {item["name"] for item in plan}


def test_compare_reports_unsafe_name(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    data = api._compute_compare(_FakeSftp({
        "/": [_FakeAttr("good.bin", size=5), _FakeAttr("a:b.bin", size=5)],
    }), str(local_dir), "/")

    assert "good.bin" in data["files"]
    assert data["not_compared_count"] == 1
    assert data["not_compared"] == [
        {"name": "a:b.bin", "reason": "unsafe name", "is_dir": False},
    ]


def test_sync_down_reports_unsafe_name_with_empty_plan(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "good.bin", b"good", 10)
    api._client = _FakeClient(_FakeSftp({
        "/": [_FakeAttr("good.bin", size=4, mtime=10), _FakeAttr("a:b.bin", size=1)],
    }))
    cid, stop = api._start_compare("sync")

    api._run_compare(cid, str(local_dir), "/", stop, direction="download")

    with api._compare_lock:
        result = api._compares[cid]["result"]
    assert result["count"] == 0
    assert result["not_compared_count"] == 1
    assert result["not_compared"] == [
        {"name": "a:b.bin", "reason": "unsafe name", "is_dir": False},
    ]


def test_file_clash_held_back_sync_up(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "report.txt")

    plan, transfers, _conflicts, omissions = _sync(api, local_dir, {
        "/": [_FakeAttr("Report.txt"), _FakeAttr("report.txt")],
    }, "upload")

    assert plan == [] and transfers == []
    assert omissions["count"] == 3
    assert {item["name"] for item in omissions["entries"]} == {"Report.txt", "report.txt"}
    assert all(item["reason"] == "name clash" for item in omissions["entries"])


def test_file_clash_held_back_sync_down(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    original = _local(local_dir, "report.txt", b"local")

    plan, transfers, _conflicts, omissions = _sync(api, local_dir, {
        "/": [_FakeAttr("Report.txt", size=6), _FakeAttr("report.txt", size=7)],
    }, "download")

    assert plan == [] and transfers == [] and omissions["count"] == 3
    assert original.read_bytes() == b"local"


def test_file_clash_held_back_both_listing_orders(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "report.txt")

    plan, transfers, _conflicts, omissions = _sync(api, local_dir, {
        "/": [_FakeAttr("report.txt"), _FakeAttr("Report.txt")],
    }, "upload")

    assert plan == [] and transfers == [] and omissions["count"] == 3
    assert {item["name"] for item in omissions["entries"]} == {"Report.txt", "report.txt"}


def test_folder_clash_holds_back_descendants_sync_up(tmp_path):
    for number, order in enumerate((("Docs", "docs"), ("docs", "Docs"))):
        api = _api()
        local_dir = tmp_path / f"local_{number}"
        local_dir.mkdir(parents=True)
        _local(local_dir, "docs/sub/f.txt")
        tree = {
            "/": [_FakeAttr(order[0], True), _FakeAttr(order[1], True)],
            "/Docs": [_FakeAttr("a.txt")], "/docs": [_FakeAttr("b.txt")],
        }

        plan, transfers, _conflicts, omissions = _sync(api, local_dir, tree, "upload")

        assert plan == [] and transfers == [] and omissions["count"] == 3
        assert {item["name"] for item in omissions["entries"]} == {"Docs", "docs"}


def test_folder_clash_holds_back_descendants_sync_down(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "docs/sub/f.txt", b"local")
    tree = {
        "/": [_FakeAttr("Docs", True), _FakeAttr("docs", True)],
        "/Docs": [_FakeAttr("a.txt")], "/docs": [_FakeAttr("b.txt")],
    }

    plan, transfers, _conflicts, omissions = _sync(api, local_dir, tree, "download")

    assert plan == [] and transfers == [] and omissions["count"] == 3
    assert (local_dir / "docs" / "sub" / "f.txt").read_bytes() == b"local"


def test_mixed_file_folder_clash_held_back(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "data/x.txt")
    tree = {"/": [_FakeAttr("data"), _FakeAttr("DATA", True)],
            "/DATA": [_FakeAttr("child.txt")]}

    for direction in ("upload", "download"):
        plan, transfers, _conflicts, omissions = _sync(api, local_dir, tree, direction)
        assert plan == [] and transfers == [] and omissions["count"] == 3


def test_clash_does_not_hold_back_unrelated_names(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "report.txt")
    _local(local_dir, "Reports2.txt")
    tree = {"/": [_FakeAttr("Report.txt"), _FakeAttr("report.txt"),
                  _FakeAttr("other.txt")]}

    plan, transfers, _conflicts, _omissions = _sync(
        api, local_dir, tree, "download", changed_only=False)

    assert {"other.txt", "Reports2.txt"} <= _names(plan)
    assert {transfer[1] for transfer in transfers} == {"/other.txt"}


def test_compare_hides_clash_group(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "report.txt")

    data = api._compute_compare(_FakeSftp({
        "/": [_FakeAttr("Report.txt"), _FakeAttr("report.txt")],
    }), str(local_dir), "/")

    assert data["files"] == {} and data["folders"] == {}
    assert data["not_compared_count"] == 3


def test_not_compared_list_capped_at_200(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    attrs = [_FakeAttr(f"a:{i}.bin") for i in range(250)]

    data = api._compute_compare(_FakeSftp({"/": attrs}), str(local_dir), "/")

    assert len(data["not_compared"]) == 200
    assert data["not_compared_count"] == 250


def test_clash_key_limit_refuses(tmp_path, monkeypatch):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    monkeypatch.setattr(constants, "COMPARE_SYNC_ENTRY_LIMIT", 2)
    tree = {"/": [_FakeAttr("A"), _FakeAttr("a"),
                  _FakeAttr("B"), _FakeAttr("b"), _FakeAttr("C"), _FakeAttr("c")]}

    with pytest.raises(ScanIncomplete, match="Too many files to compare"):
        api._compute_compare(_FakeSftp(tree), str(local_dir), "/")


def test_stream_sync_final_line_reports_not_compared(tmp_path, monkeypatch):
    api = _api()
    api._connected = True
    logged = []
    real_worker_log = api._worker_log

    def capture_log(*args, **kwargs):
        logged.append(kwargs)
        return real_worker_log(*args, **kwargs)

    monkeypatch.setattr(api, "_worker_log", capture_log)
    scan_id, stop = api._register_scan()
    _stream_sync_transfers(api, [], "upload", "overwrite", scan_id, stop, 1,
                           not_compared_count=3)
    with api._console_lock:
        empty_line = api._console_buffer[-1]
    assert "3 remote entries not compared" in empty_line["msg"]
    assert empty_line["level"] == "warn" and logged[-1]["outcome"] == "incomplete"

    monkeypatch.setattr(api, "_enqueue_files", lambda batch, *_args, **_kwargs: (len(batch), 0))
    scan_id, stop = api._register_scan()
    _stream_sync_transfers(api, [(str(tmp_path / "x"), "/x", 1, False)], "upload",
                           "overwrite", scan_id, stop, 2, not_compared_count=3)
    with api._console_lock:
        queued_line = api._console_buffer[-1]
    assert "3 remote entries not compared" in queued_line["msg"]
    assert queued_line["level"] == "warn" and logged[-1]["outcome"] == "incomplete"


def test_nested_clash_lists_entries_at_clash_depth(tmp_path):
    # A clash inside a subfolder names the clashing entries themselves
    # (sub/report.txt), never the parent folder, and leaves the rest of
    # that folder in the comparison.
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "sub/report.txt")
    _local(local_dir, "sub/keep.txt")

    plan, transfers, _conflicts, omissions = _sync(api, local_dir, {
        "/": [_FakeAttr("sub", is_dir=True)],
        "/sub": [_FakeAttr("Report.txt"), _FakeAttr("report.txt")],
    }, "upload")

    assert _names(plan) == {"sub/keep.txt"}
    assert all("report" not in t[0].lower() for t in transfers)
    assert omissions["count"] == 3
    assert {item["name"] for item in omissions["entries"]} == {"sub/Report.txt", "sub/report.txt"}
    assert all(item["is_dir"] is False for item in omissions["entries"])


def test_nested_folder_clash_marks_local_top_as_folder(tmp_path):
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    _local(local_dir, "sub/docs/deep/f.txt")

    plan, transfers, _conflicts, omissions = _sync(api, local_dir, {
        "/": [_FakeAttr("sub", is_dir=True)],
        "/sub": [_FakeAttr("Docs", is_dir=True), _FakeAttr("docs", is_dir=True)],
        "/sub/Docs": [_FakeAttr("a.txt")],
        "/sub/docs": [_FakeAttr("b.txt")],
    }, "upload")

    assert plan == [] and transfers == []
    local_entries = [e for e in omissions["entries"] if e["name"] == "sub/docs"]
    assert local_entries and all(e["is_dir"] for e in local_entries)
    kept = [e for e in omissions["entries"] if e["name"] == "sub/Docs"]
    assert kept == [{"name": "sub/Docs", "reason": "name clash", "is_dir": True}]


def test_too_many_unsafe_names_refuses_during_walk(tmp_path, monkeypatch):
    # Endless unusable names are refused with the size-limit message during
    # the walk, not reported as an unreadable folder.
    api = _api()
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    monkeypatch.setattr(constants, "COMPARE_SYNC_ENTRY_LIMIT", 5)

    with pytest.raises(ScanIncomplete, match="Too many files to compare"):
        api._compute_compare(_FakeSftp({
            "/": [_FakeAttr(f"bad{i}:x") for i in range(6)],
        }), str(local_dir), "/")
