"""Confinement coverage for file-browser operations."""

import os
import stat

import paramiko
import pytest

from app.paths import remote_leaf_problem


def _attr(name, is_dir=False, size=0):
    attr = paramiko.SFTPAttributes()
    attr.filename = name
    attr.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG
    attr.st_size = size
    attr.st_mtime = 0
    return attr


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "/etc/x", "../x", "a\x00b"])
def test_remote_leaf_problem_rejects_unsafe_names(name):
    assert remote_leaf_problem(name) is not None


@pytest.mark.parametrize("name", ["a.txt", "a\\b", "a:b", "café.txt"])
def test_remote_leaf_problem_allows_remote_names(name):
    assert remote_leaf_problem(name) is None


@pytest.mark.parametrize("name", [r"..\x", r"C:\x", r"\\srv\share\x", "a/b", "CON", "a."])
def test_local_make_dir_refuses_unsafe_names(sftp_env, tmp_path, name):
    api, _server_root, local_dir = sftp_env
    before = sorted(path.name for path in tmp_path.iterdir())

    result = api.make_dir("local", str(local_dir), name)

    assert result["ok"] is False
    assert "can't be used" in result["error"]
    assert sorted(path.name for path in tmp_path.iterdir()) == before


@pytest.mark.parametrize("old,new", [("source.txt", r"..\x"), (r"..\source.txt", "new.txt")])
def test_local_rename_refuses_unsafe_names(sftp_env, old, new):
    api, _server_root, local_dir = sftp_env
    source = local_dir / "source.txt"
    source.write_text("unchanged", encoding="utf-8")

    result = api.rename("local", str(local_dir), old, new)

    assert result["ok"] is False
    assert source.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("name", ["..", r"..\sibling"])
def test_local_delete_refuses_unsafe_name_without_touching_sibling(sftp_env, name):
    api, _server_root, local_dir = sftp_env
    sibling = local_dir.parent / "sibling"
    sibling.write_text("keep", encoding="utf-8")
    normal = local_dir / "normal.txt"
    normal.write_text("remove", encoding="utf-8")

    result = api.delete("local", str(local_dir), [
        {"name": name, "is_dir": False}, {"name": "normal.txt", "is_dir": False},
    ])

    assert any(name in error and "can't be used" in error for error in result["errors"])
    assert sibling.exists()
    assert not normal.exists()


def test_open_local_refuses_unsafe_name_without_starting(sftp_env, monkeypatch):
    api, _server_root, local_dir = sftp_env
    calls = []
    monkeypatch.setattr(os, "startfile", lambda path: calls.append(path), raising=False)

    result = api.open_local(str(local_dir), r"..\x")

    assert result["ok"] is False
    assert calls == []


@pytest.mark.parametrize("operation,args", [
    ("make_dir", ("remote", "/", "../x")),
    ("rename", ("remote", "/", "../x", "new")),
    ("delete", ("remote", "/", [{"name": "a/b", "is_dir": False}])),
])
def test_remote_browser_operations_refuse_unsafe_names(sftp_env, monkeypatch, operation, args):
    api, _server_root, _local_dir = sftp_env
    calls = []
    for method in ("mkdir", "rename", "remove", "rmdir"):
        monkeypatch.setattr(api._sftp, method, lambda *paths, _m=method: calls.append((_m, paths)))

    result = getattr(api, operation)(*args)

    assert result["ok"] is False or result["errors"]
    assert calls == []


def test_remote_backslash_and_colon_names_reach_the_server(sftp_env, monkeypatch):
    # The in-process server stores files in a Windows folder, which cannot
    # hold these names, so record the calls instead of performing them.
    api, _server_root, _local_dir = sftp_env
    calls = []
    monkeypatch.setattr(api._sftp, "mkdir", lambda path: calls.append(("mkdir", path)))
    monkeypatch.setattr(api._sftp, "rename", lambda old, new: calls.append(("rename", old, new)))
    assert api.make_dir("remote", "/", "a:b") == {"ok": True}
    assert api.make_dir("remote", "/", "a\\b") == {"ok": True}
    assert api.rename("remote", "/", "a:b", "b\\c") == {"ok": True}
    assert calls == [("mkdir", "/a:b"), ("mkdir", "/a\\b"), ("rename", "/a:b", "/b\\c")]


def test_list_remote_drops_unsafe_server_names(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    lines = []
    monkeypatch.setattr("app.services.browsing._iter_remote_dir", lambda api, path: (entry for entry in [
        _attr("good.txt"), _attr(".."), _attr("../x"), _attr("a/b"),
    ]))
    monkeypatch.setattr(api, "_vlog", lambda msg, level="info", **kwargs: lines.append((msg, level, kwargs)))

    result = api.list_remote("/")

    assert [entry["name"] for entry in result["entries"]] == ["good.txt"]
    warnings = [line for line in lines if line[1] == "warn"]
    assert len(warnings) == 1
    assert "3 unsafe" in warnings[0][0]
    assert warnings[0][2]["items"] == 3


def test_remote_delete_stops_on_unsafe_nested_name(sftp_env, monkeypatch):
    api, server_root, _local_dir = sftp_env
    folder = server_root / "folder"
    folder.mkdir()
    (folder / "safe.txt").write_text("delete first", encoding="utf-8")
    calls = []
    real_listdir = api._sftp.listdir_attr
    real_remove, real_rmdir = api._sftp.remove, api._sftp.rmdir

    def listed(path):
        if path == "/folder":
            return [_attr("safe.txt"), _attr("../victim")]
        return real_listdir(path)

    monkeypatch.setattr(api._sftp, "listdir_attr", listed)
    monkeypatch.setattr(api._sftp, "remove", lambda path: (calls.append(path), real_remove(path))[1])
    monkeypatch.setattr(api._sftp, "rmdir", lambda path: (calls.append(path), real_rmdir(path))[1])

    result = api.delete("remote", "/", [{"name": "folder", "is_dir": True}])

    assert "stopped" in result["errors"][0]
    assert "may already be deleted" in result["errors"][0]
    assert all(path.startswith("/folder/") for path in calls)


def test_remote_size_skips_unsafe_nested_name(sftp_env, monkeypatch):
    api, server_root, _local_dir = sftp_env
    folder = server_root / "folder"
    folder.mkdir()
    (folder / "good.txt").write_bytes(b"good")
    lines = []
    real_listdir = api._sftp.listdir_attr

    def listed(path):
        attrs = real_listdir(path)
        return attrs + ([_attr("../x", size=999)] if path == "/folder" else [])

    monkeypatch.setattr(api._sftp, "listdir_attr", listed)
    monkeypatch.setattr(api, "_vlog", lambda msg, level="info", **kwargs: lines.append((msg, level)))

    result = api.calc_remote_size("/", "folder")

    assert result["ok"] is True
    assert result["complete"] is False
    assert result["problem_count"] == 1
    assert result["bytes"] == 4
    assert sum("unsafe remote name" in msg and level == "warn" for msg, level in lines) == 1


@pytest.mark.parametrize("name", ["../x", ".", "..", "", "a/b", "a\x00b"])
def test_remote_size_refuses_unsafe_top_level_name_without_listing(sftp_env, monkeypatch, name):
    api, _server_root, _local_dir = sftp_env
    calls = []
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: calls.append(path))

    result = api.calc_remote_size("/", name)

    assert result["ok"] is False
    assert calls == []


def test_upload_enqueue_skips_unsafe_names_and_keeps_normal_job(sftp_env, wait_for_drain, monkeypatch):
    api, server_root, local_dir = sftp_env
    (local_dir / "good.txt").write_text("good", encoding="utf-8")
    lines = []
    real_log = api._worker_log
    monkeypatch.setattr(api, "_worker_log", lambda *args, **kwargs: (lines.append(args), real_log(*args, **kwargs))[1])

    result = api.enqueue([
        {"name": r"..\x", "is_dir": False}, {"name": "../x", "is_dir": False},
        {"name": "good.txt", "is_dir": False},
    ], "upload", str(local_dir), "/")
    wait_for_drain(api)

    assert result == {"ok": True, "scanning": True}
    assert (server_root / "good.txt").read_text(encoding="utf-8") == "good"
    assert len([line for line in lines if "unsafe upload name" in line[0]]) == 2
    assert all(item["name"] == "good.txt" for item in api._queue.snapshot())
