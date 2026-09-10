"""
Tests for _compute_sync(), the pure recursive core behind sync_plan(), which
computes what a folder sync would transfer without transferring anything.
Runs against the in-process SFTP server from conftest.py. sync_plan() itself
is now async (see test_compare.py for its poll_queue() contract); these
tests call the pure core directly, over the sftp session the sftp_env
fixture already has open, since that core has no threading or bridge
concerns of its own.
"""
import os


def _put_local(local_dir, name, data, mtime=None):
    p = local_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _put_remote(api, server_root, name, data, mtime=None):
    p = server_root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _by_name(plan):
    return {p["name"]: p for p in plan}


def _sync(api, local_dir, direction, changed_only=True):
    plan, transfers, _conflicts = api._compute_sync(api.sftp, str(local_dir), "/", direction, changed_only)
    return plan, transfers


def test_upload_plan_includes_local_only_file(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "new.txt", b"hello")

    plan, transfers = _sync(api, local_dir, "upload")
    by_name = _by_name(plan)
    assert by_name["new.txt"]["status"] == "local_only"
    assert by_name["new.txt"]["local"]["size"] == 5
    assert isinstance(by_name["new.txt"]["local"]["mtime"], int)
    assert by_name["new.txt"]["remote"] is None
    assert transfers == [(str(local_dir / "new.txt"), "/new.txt", 5, False)]


def test_download_plan_includes_remote_only_file(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_remote(api, server_root, "new.txt", b"hello")

    plan, transfers = _sync(api, local_dir, "download")
    by_name = _by_name(plan)
    assert by_name["new.txt"]["status"] == "remote_only"
    assert by_name["new.txt"]["local"] is None
    assert by_name["new.txt"]["remote"]["size"] == 5
    assert transfers == [(str(local_dir / "new.txt"), "/new.txt", 5, False)]


def test_upload_plan_includes_changed_file(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "same_name.txt", b"local bytes, longer content")
    _put_remote(api, server_root, "same_name.txt", b"remote bytes")

    plan, _transfers = _sync(api, local_dir, "upload")
    by_name = _by_name(plan)
    assert by_name["same_name.txt"]["status"] in ("newer_local", "newer_remote")
    assert by_name["same_name.txt"]["local"]["size"] == len(b"local bytes, longer content")
    assert by_name["same_name.txt"]["remote"]["size"] == len(b"remote bytes")


def test_identical_file_not_in_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    data = b"identical bytes"
    _put_local(local_dir, "same.txt", data)
    _put_remote(api, server_root, "same.txt", data)

    plan, _t = _sync(api, local_dir, "upload")
    assert "same.txt" not in _by_name(plan)

    plan, _t = _sync(api, local_dir, "download")
    assert "same.txt" not in _by_name(plan)


def test_destination_only_file_not_in_upload_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_remote(api, server_root, "remote_only.txt", b"hello")

    plan, _t = _sync(api, local_dir, "upload")
    assert "remote_only.txt" not in _by_name(plan)


def test_source_only_file_not_in_download_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "local_only.txt", b"hello")

    plan, _t = _sync(api, local_dir, "download")
    assert "local_only.txt" not in _by_name(plan)


def test_all_identical_pair_yields_empty_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    data = b"same everywhere"
    _put_local(local_dir, "a.txt", data)
    _put_remote(api, server_root, "a.txt", data)

    plan, transfers = _sync(api, local_dir, "upload")
    assert plan == []
    assert transfers == []

    plan, transfers = _sync(api, local_dir, "download")
    assert plan == []
    assert transfers == []


# ───────────── recursive (nested) cases ─────────────

def test_nested_changed_file_appears_in_plan_with_its_rel_path(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "top/mid/changed.txt", b"local version, different length")
    _put_remote(api, server_root, "top/mid/changed.txt", b"remote version")

    plan, transfers = _sync(api, local_dir, "upload")
    by_name = _by_name(plan)
    assert "top/mid/changed.txt" in by_name
    assert by_name["top/mid/changed.txt"]["local"]["size"] == len(b"local version, different length")
    assert transfers == [
        (str(local_dir / "top" / "mid" / "changed.txt"), "/top/mid/changed.txt",
         len(b"local version, different length"), False),
    ]


def test_nested_identical_file_omitted_from_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    data = b"identical nested bytes"
    _put_local(local_dir, "top/mid/deep/same.txt", data)
    _put_remote(api, server_root, "top/mid/deep/same.txt", data)

    plan, transfers = _sync(api, local_dir, "upload")
    assert "top/mid/deep/same.txt" not in _by_name(plan)
    assert transfers == []

    plan, transfers = _sync(api, local_dir, "download")
    assert "top/mid/deep/same.txt" not in _by_name(plan)
    assert transfers == []


def test_nested_only_file_classifies_correctly_on_each_side(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "top/local_only.txt", b"only local")
    _put_remote(api, server_root, "top/remote_only.txt", b"only remote")

    upload_plan, upload_transfers = _sync(api, local_dir, "upload")
    up_by_name = _by_name(upload_plan)
    assert up_by_name["top/local_only.txt"]["status"] == "local_only"
    assert "top/remote_only.txt" not in up_by_name
    assert upload_transfers == [
        (str(local_dir / "top" / "local_only.txt"), "/top/local_only.txt", len(b"only local"), False),
    ]

    download_plan, download_transfers = _sync(api, local_dir, "download")
    down_by_name = _by_name(download_plan)
    assert down_by_name["top/remote_only.txt"]["status"] == "remote_only"
    assert "top/local_only.txt" not in down_by_name
    assert download_transfers == [
        (str(local_dir / "top" / "remote_only.txt"), "/top/remote_only.txt", len(b"only remote"), False),
    ]
