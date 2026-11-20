"""Bounds for local and remote browser directory listings."""

import stat

import pytest

from app.services import browsing


@pytest.fixture(autouse=True)
def small_browse_limits(monkeypatch):
    monkeypatch.setattr(browsing, "BROWSE_ENTRY_LIMIT", 5)
    monkeypatch.setattr(browsing, "BROWSE_EXAMINE_LIMIT", 10)


def _files(folder, count):
    for index in range(count):
        (folder / f"entry-{index}.txt").write_text(str(index), encoding="utf-8")


def _attr(name):
    attr = type("Attr", (), {})()
    attr.filename = name
    attr.st_mode = stat.S_IFREG
    attr.st_size = 0
    attr.st_mtime = 0
    return attr


def test_local_under_limit(sftp_env):
    api, _server_root, local_dir = sftp_env
    _files(local_dir, 3)

    result = api.list_local(str(local_dir))

    assert len(result["entries"]) == 3
    assert result["truncated"] is False
    assert result["limit"] == 5


def test_local_exactly_at_limit(sftp_env):
    api, _server_root, local_dir = sftp_env
    _files(local_dir, 5)

    result = api.list_local(str(local_dir))

    assert len(result["entries"]) == 5
    assert result["truncated"] is False


def test_local_over_limit(sftp_env):
    api, _server_root, local_dir = sftp_env
    _files(local_dir, 8)

    result = api.list_local(str(local_dir))

    assert len(result["entries"]) == 5
    assert result["truncated"] is True
    assert result["limit"] == 5


def test_local_skipped_entries_do_not_count(sftp_env, monkeypatch):
    api, _server_root, local_dir = sftp_env
    _files(local_dir, 7)
    monkeypatch.setattr(browsing, "is_active_local", lambda path: path.endswith(("entry-0.txt", "entry-1.txt")))

    result = api.list_local(str(local_dir))

    assert len(result["entries"]) == 5
    assert result["truncated"] is False


def test_local_examine_budget(sftp_env, monkeypatch):
    api, _server_root, local_dir = sftp_env
    _files(local_dir, 12)
    monkeypatch.setattr(browsing, "is_active_local", lambda path: True)

    result = api.list_local(str(local_dir))

    assert result["entries"] == []
    assert result["truncated"] is True


@pytest.mark.parametrize(("count", "truncated"), [(3, False), (5, False), (8, True)])
def test_remote_under_at_over_limit(sftp_env, count, truncated):
    api, server_root, _local_dir = sftp_env
    _files(server_root, count)

    result = api.list_remote("/")

    assert len(result["entries"]) == min(count, 5)
    assert result["truncated"] is truncated
    assert result["limit"] == 5


def test_remote_unsafe_names_do_not_count(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    attrs = [_attr("../unsafe-1"), _attr("../unsafe-2"), _attr("../unsafe-3"), _attr("../unsafe-4")]
    attrs.extend(_attr(f"entry-{index}.txt") for index in range(5))
    monkeypatch.setattr(browsing, "_iter_remote_dir", lambda api, path: (attr for attr in attrs))

    result = api.list_remote("/")

    assert len(result["entries"]) == 5
    assert result["truncated"] is False


def test_remote_early_stop_closes_handle_and_channel_still_works(sftp_env, monkeypatch):
    api, server_root, _local_dir = sftp_env
    _files(server_root, 8)
    other = server_root / "other"
    other.mkdir()
    _files(other, 1)
    requests = []
    real_request = api._sftp._request

    def request(command, *args):
        requests.append(command)
        return real_request(command, *args)

    monkeypatch.setattr(api._sftp, "_request", request)

    first = api.list_remote("/")
    first_requests = list(requests)
    second = api.list_remote("/other")
    ping = api.ping()

    assert len(first["entries"]) == 5
    assert first["truncated"] is True
    assert first_requests.count(browsing.CMD_OPENDIR) == 1
    assert first_requests[-1] == browsing.CMD_CLOSE
    assert [entry["name"] for entry in second["entries"]] == ["entry-0.txt"]
    assert ping["ok"] is True


def test_remote_error_mid_listing_still_closes(sftp_env, monkeypatch):
    api, server_root, _local_dir = sftp_env
    _files(server_root, 3)
    requests = []
    real_request = api._sftp._request
    readdir_count = 0

    def request(command, *args):
        nonlocal readdir_count
        requests.append(command)
        if command == browsing.CMD_READDIR:
            readdir_count += 1
            if readdir_count == 2:
                raise OSError("listing failed")
        return real_request(command, *args)

    monkeypatch.setattr(api._sftp, "_request", request)

    result = api.list_remote("/")

    assert result["ok"] is False
    assert browsing.CMD_CLOSE in requests
