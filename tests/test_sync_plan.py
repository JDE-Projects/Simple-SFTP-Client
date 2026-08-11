"""
Tests for sync_plan(), which computes what a folder sync would transfer
without transferring anything. Runs against the in-process SFTP server from
conftest.py.
"""
import os


def _put_local(local_dir, name, data, mtime=None):
    p = local_dir / name
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _put_remote(api, server_root, name, data, mtime=None):
    p = server_root / name
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _by_name(plan):
    return {p["name"]: p for p in plan}


def test_upload_plan_includes_local_only_file(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "new.txt", b"hello")

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    plan = _by_name(r["plan"])
    assert plan["new.txt"]["status"] == "local_only"
    assert plan["new.txt"]["local"]["size"] == 5
    assert isinstance(plan["new.txt"]["local"]["mtime"], int)
    assert plan["new.txt"]["remote"] is None


def test_download_plan_includes_remote_only_file(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_remote(api, server_root, "new.txt", b"hello")

    r = api.sync_plan(str(local_dir), "/", "download")
    assert r["ok"] is True
    plan = _by_name(r["plan"])
    assert plan["new.txt"]["status"] == "remote_only"
    assert plan["new.txt"]["local"] is None
    assert plan["new.txt"]["remote"]["size"] == 5


def test_upload_plan_includes_changed_file(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "same_name.txt", b"local bytes, longer content")
    _put_remote(api, server_root, "same_name.txt", b"remote bytes")

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    plan = _by_name(r["plan"])
    assert plan["same_name.txt"]["status"] in ("newer_local", "newer_remote")
    assert plan["same_name.txt"]["local"]["size"] == len(b"local bytes, longer content")
    assert plan["same_name.txt"]["remote"]["size"] == len(b"remote bytes")


def test_identical_file_not_in_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    data = b"identical bytes"
    _put_local(local_dir, "same.txt", data)
    _put_remote(api, server_root, "same.txt", data)

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    assert "same.txt" not in _by_name(r["plan"])

    r = api.sync_plan(str(local_dir), "/", "download")
    assert r["ok"] is True
    assert "same.txt" not in _by_name(r["plan"])


def test_destination_only_file_not_in_upload_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_remote(api, server_root, "remote_only.txt", b"hello")

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    assert "remote_only.txt" not in _by_name(r["plan"])


def test_source_only_file_not_in_download_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "local_only.txt", b"hello")

    r = api.sync_plan(str(local_dir), "/", "download")
    assert r["ok"] is True
    assert "local_only.txt" not in _by_name(r["plan"])


def test_all_identical_pair_yields_empty_plan(sftp_env):
    api, server_root, local_dir = sftp_env
    data = b"same everywhere"
    _put_local(local_dir, "a.txt", data)
    _put_remote(api, server_root, "a.txt", data)

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    assert r["plan"] == []

    r = api.sync_plan(str(local_dir), "/", "download")
    assert r["ok"] is True
    assert r["plan"] == []
