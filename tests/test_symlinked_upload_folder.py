"""Regression coverage for excluding local links from upload-side work."""

import builtins
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from app.api import Api
from app.paths import is_local_link
from app.services import watching
from app.services import transfer_io
from simple_sftp_client import APP_VERSION


def _junction(link, target):
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True)


def _symlink(link, target, target_is_directory=False):
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except OSError as e:
        pytest.skip(f"symlink creation unavailable: {e}")


def _console(api):
    with api._console_lock:
        return [line["msg"] for line in api._console_buffer]


def test_is_local_link_tags():
    def st(tag, mode=stat.S_IFREG):
        return SimpleNamespace(st_reparse_tag=tag, st_mode=mode)

    assert is_local_link(st(0xA000000C))
    assert is_local_link(st(0xA0000003))
    assert not is_local_link(st(0x9000001A))
    assert not is_local_link(st(0x80000013))
    assert not is_local_link(st(0))


def test_scan_skips_file_symlink(tmp_path):
    api = Api(APP_VERSION)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    link = root / "file-link"
    _symlink(link, outside)

    excluded = []
    found = list(api._iter_local(str(root), "/", True, excluded=excluded))

    assert [path for path, *_ in found] == [str(root / "a.txt")]
    assert excluded == [str(link)]
    assert any(f"skipped {link}: it is a symlink" in message for message in _console(api))


def test_scan_skips_dir_symlink(tmp_path):
    api = Api(APP_VERSION)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "hidden.txt").write_text("hidden")
    _symlink(root / "dir-link", outside, target_is_directory=True)

    assert list(api._iter_local(str(root), "/", True)) == []


def test_scan_skips_junction(tmp_path):
    api = Api(APP_VERSION)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "hidden.txt").write_text("hidden")
    link = root / "junction"
    _junction(link, outside)

    excluded = []
    assert list(api._iter_local(str(root), "/", True, excluded=excluded)) == []
    assert excluded == [str(link)]


def test_scan_junction_to_ancestor_terminates(tmp_path):
    api = Api(APP_VERSION)
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.txt").write_text("real")
    _junction(root / "again", root)

    found = list(api._iter_local(str(root), "/", True))

    assert [path for path, *_ in found] == [str(root / "real.txt")]


def test_scan_root_link_skipped(tmp_path):
    api = Api(APP_VERSION)
    target = tmp_path / "target"
    target.mkdir()
    (target / "real.txt").write_text("real")
    link = tmp_path / "junction"
    _junction(link, target)

    excluded = []
    assert list(api._iter_local(str(link), "/", True, excluded=excluded)) == []
    assert excluded == [str(link)]


def test_scan_summary_counts_links(sftp_env, wait_for_drain):
    api, _server_root, local_dir = sftp_env
    root = local_dir / "root"
    target = local_dir / "target"
    root.mkdir()
    target.mkdir()
    (target / "hidden.txt").write_text("hidden")
    _junction(root / "junction", target)

    assert api.enqueue([{"name": "root", "is_dir": True}], "upload",
                       str(local_dir), "/")["ok"]
    wait_for_drain(api)

    assert any("1 link(s) skipped" in message for message in _console(api))


def test_upload_paths_all_links_errors(sftp_env):
    api, _server_root, local_dir = sftp_env
    target = local_dir / "target"
    target.mkdir()
    link = local_dir / "junction"
    _junction(link, target)

    result = api.upload_paths([str(link)], "/")

    assert result["ok"] is False
    assert "Links are not uploaded" in result["error"]


def test_compare_excludes_local_link_both_ways(sftp_env):
    api, server_root, local_dir = sftp_env
    target = local_dir / "target"
    target.mkdir()
    _junction(local_dir / "j", target)
    (server_root / "j").mkdir()
    (server_root / "j" / "x.txt").write_text("remote")
    (server_root / "other.txt").write_text("remote")

    result = api._compute_compare(api._sftp, str(local_dir), "/")

    assert "j" not in result["files"]
    assert "j/x.txt" not in result["files"]
    assert result["excluded_links"] == ["j"]
    assert result["files"]["other.txt"] == "remote_only"


def test_sync_down_never_targets_link(sftp_env):
    api, server_root, local_dir = sftp_env
    target = local_dir / "target"
    target.mkdir()
    _junction(local_dir / "j", target)
    (server_root / "j").mkdir()
    (server_root / "j" / "x.txt").write_text("remote")

    _plan, transfers, _conflicts = api._compute_sync(
        api._sftp, str(local_dir), "/", "download")

    assert not any("j" in remote_path.split("/") for _local, remote_path, *_ in transfers)


def test_compare_and_sync_exclude_case_paired_local_link(sftp_env):
    api, server_root, local_dir = sftp_env
    target = local_dir / "target"
    target.mkdir()
    _junction(local_dir / "Link", target)
    (server_root / "link").mkdir()
    (server_root / "link" / "f.txt").write_text("remote")

    result = api._compute_compare(api._sftp, str(local_dir), "/")
    _plan, transfers, _conflicts = api._compute_sync(
        api._sftp, str(local_dir), "/", "download")

    assert "link/f.txt" not in result["files"]
    assert not any(remote_path.endswith("/link/f.txt") for _local, remote_path, *_ in transfers)


def test_compare_root_link_allowed(sftp_env):
    api, _server_root, local_dir = sftp_env
    target = local_dir / "target"
    target.mkdir()
    (target / "a.txt").write_text("local")
    link = local_dir / "root-link"
    _junction(link, target)

    result = api._compute_compare(api._sftp, str(link), "/")

    assert result["files"]["a.txt"] == "local_only"


def test_watch_snapshot_skips_links(tmp_path):
    api = Api(APP_VERSION)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "real.txt").write_text("real")
    target_file = outside / "outside.txt"
    target_file.write_text("outside")
    _symlink(root / "file-link", target_file)
    _junction(root / "junction", outside)

    excluded = set()
    first = watching.snapshot(api, str(root), excluded)
    second = watching.snapshot(api, str(root), excluded)

    assert list(first) == [str(root / "real.txt")]
    assert second == first
    skipped = [message for message in _console(api) if "links are not uploaded" in message]
    assert len(skipped) == 2


def test_put_file_refuses_link(sftp_env):
    api, server_root, local_dir = sftp_env
    target = local_dir / "target.txt"
    target.write_text("target")
    link = local_dir / "link.txt"
    _symlink(link, target)

    with pytest.raises(IOError, match="is a link"):
        api._put_file(api._sftp, str(link), "/upload.txt", lambda *_: None, lambda: False)

    assert not (server_root / "upload.txt").exists()


def test_put_file_identity_mismatch(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    source = local_dir / "source.txt"
    source.write_text("source")
    real_fstat = transfer_io.os.fstat

    def mismatched(fd):
        result = real_fstat(fd)
        return SimpleNamespace(st_ino=result.st_ino + 1, st_dev=result.st_dev,
                               st_size=result.st_size, st_mtime=result.st_mtime)

    monkeypatch.setattr(transfer_io.os, "fstat", mismatched)

    with pytest.raises(IOError, match="was replaced"):
        api._put_file(api._sftp, str(source), "/upload.txt", lambda *_: None, lambda: False)

    assert not (server_root / "upload.txt").exists()


def test_put_file_uses_open_handle_size(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    source = local_dir / "source.txt"
    source.write_bytes(b"source")
    real_open = builtins.open

    def growing_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if os.fspath(path) == str(source) and mode == "rb":
            with real_open(source, "ab") as writer:
                writer.write(b"-grown")
        return handle

    monkeypatch.setattr(transfer_io, "open", growing_open, raising=False)
    monkeypatch.setattr(transfer_io.os.path, "getsize",
                        lambda _path: pytest.fail("path size must not be used after open"))

    assert api._put_file(api._sftp, str(source), "/upload.txt", lambda *_: None, lambda: False)

    assert (server_root / "upload.txt").read_bytes() == b"source-grown"


def test_onedrive_style_tag_included(tmp_path, monkeypatch):
    api = Api(APP_VERSION)
    root = tmp_path / "root"
    root.mkdir()
    cloud = root / "cloud.txt"
    cloud.write_text("cloud")
    real_lstat = os.lstat

    def tagged(path):
        result = real_lstat(path)
        if os.fspath(path) == str(cloud):
            return SimpleNamespace(st_mode=result.st_mode, st_size=result.st_size,
                                   st_mtime=result.st_mtime, st_reparse_tag=0x9000001A)
        return result

    monkeypatch.setattr("app.services.scanning.os.lstat", tagged)
    found = list(api._iter_local(str(cloud), "/cloud.txt", False))

    assert found[0][0] == str(cloud)
