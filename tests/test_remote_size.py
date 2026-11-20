"""Coverage for incomplete remote folder-size calculations."""

import stat

import paramiko

from app.services import connections


def _attr(name, is_dir=False, size=0, mode=True):
    attr = paramiko.SFTPAttributes()
    attr.filename = name
    attr.st_mode = (stat.S_IFDIR if is_dir else stat.S_IFREG) if mode else None
    attr.st_size = size
    attr.st_mtime = 0
    return attr


def _logs(api, monkeypatch):
    lines = []
    monkeypatch.setattr(api, "_vlog",
                        lambda msg, level="info", **kwargs: lines.append((msg, level, kwargs)))
    return lines


def test_remote_size_complete_folder(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: {
        "/folder": [_attr("one.txt", size=2), _attr("nested", is_dir=True)],
        "/folder/nested": [_attr("two.txt", size=3)],
    }[path])

    result = api.calc_remote_size("/", "folder")

    assert result == {"ok": True, "bytes": 5, "human": "5 B", "files": 2,
                      "complete": True, "problem_count": 0, "problems": [], "more": 0}


def test_remote_size_empty_folder(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: [])

    result = api.calc_remote_size("/", "empty")

    assert result["ok"] is True
    assert result["complete"] is True
    assert result["bytes"] == 0
    assert result["files"] == 0


def test_remote_size_counts_zero_byte_file(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: [_attr("empty.txt", size=0)])

    result = api.calc_remote_size("/", "folder")

    assert result["complete"] is True
    assert result["bytes"] == 0
    assert result["files"] == 1


def test_remote_size_records_denied_subfolder(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: (
        [_attr("readable.txt", size=4), _attr("denied", is_dir=True)]
        if path == "/folder" else (_ for _ in ()).throw(PermissionError("denied"))))
    monkeypatch.setattr(connections, "_connection_dead", lambda api, sftp: False)

    result = api.calc_remote_size("/", "folder")

    assert result["ok"] is True
    assert result["complete"] is False
    assert result["bytes"] == 4
    assert result["problems"] == [{"path": "/folder/denied", "reason": "permission denied (no access to read it)"}]


def test_remote_size_records_missing_mode(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    calls = []

    def listed(path):
        calls.append(path)
        return [_attr("unknown", mode=False)]

    monkeypatch.setattr(api._sftp, "listdir_attr", listed)

    result = api.calc_remote_size("/", "folder")

    assert result["problem_count"] == 1
    assert result["files"] == 0
    assert result["bytes"] == 0
    assert calls == ["/folder"]


def test_remote_size_records_missing_file_size(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: [_attr("unknown.txt", size=None)])

    result = api.calc_remote_size("/", "folder")

    assert result["problem_count"] == 1
    assert result["files"] == 0
    assert result["bytes"] == 0


def test_remote_size_records_unsafe_nested_name(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: [_attr("../unsafe", size=9)])

    result = api.calc_remote_size("/", "folder")

    assert result["complete"] is False
    assert result["problem_count"] == 1
    assert result["problems"][0]["path"] == "/folder/'../unsafe'"


def test_remote_size_unreadable_top_folder_returns_error(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr",
                        lambda path: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(connections, "_connection_dead", lambda api, sftp: False)

    result = api.calc_remote_size("/", "folder")

    assert result["ok"] is False
    assert "/folder" in result["error"]
    assert "bytes" not in result


def test_remote_size_stops_when_connection_is_dead(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    calls = []

    def listed(path):
        calls.append(path)
        if path == "/folder":
            return [_attr("lost", is_dir=True), _attr("later", is_dir=True)]
        raise OSError("connection gone")

    monkeypatch.setattr(api._sftp, "listdir_attr", listed)
    monkeypatch.setattr(connections, "_connection_dead", lambda api, sftp: True)

    result = api.calc_remote_size("/", "folder")

    assert result["connection_lost"] is True
    assert calls == ["/folder", "/folder/lost"]


def test_remote_size_treats_ssh_exception_as_connection_loss(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: (
        [_attr("lost", is_dir=True)] if path == "/folder"
        else (_ for _ in ()).throw(paramiko.SSHException("channel closed"))))
    monkeypatch.setattr(connections, "_connection_dead", lambda api, sftp: False)

    result = api.calc_remote_size("/", "folder")

    assert result["ok"] is False
    assert result["connection_lost"] is True


def test_remote_size_consults_connection_state_for_permission_error(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    checked = []
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: (
        [_attr("denied", is_dir=True)] if path == "/folder"
        else (_ for _ in ()).throw(PermissionError("denied"))))
    monkeypatch.setattr(connections, "_connection_dead",
                        lambda api, sftp: checked.append((api, sftp)) or False)

    result = api.calc_remote_size("/", "folder")

    assert result["complete"] is False
    assert result["problem_count"] == 1
    assert "connection_lost" not in result
    assert checked == [(api, api._sftp)]


def test_remote_size_cancels_mid_walk(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    calls = 0

    def listed(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [_attr("nested", is_dir=True)]
        api._cancel.set()
        return []

    monkeypatch.setattr(api._sftp, "listdir_attr", listed)

    result = api.calc_remote_size("/", "folder")

    assert result == {"ok": False, "cancelled": True, "error": "Size calculation cancelled."}
    assert "bytes" not in result


def test_remote_size_cancels_after_final_listing(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env

    def listed(path):
        api._cancel.set()
        return []

    monkeypatch.setattr(api._sftp, "listdir_attr", listed)

    result = api.calc_remote_size("/", "folder")

    assert result["cancelled"] is True
    assert "bytes" not in result


def test_remote_size_caps_problem_details(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    monkeypatch.setattr(api._sftp, "listdir_attr",
                        lambda path: [_attr(f"missing-{i}.txt", size=None) for i in range(10_005)])

    result = api.calc_remote_size("/", "folder")

    assert len(result["problems"]) == 10_000
    assert result["problem_count"] == 10_005
    assert result["more"] == 5


def test_remote_size_incomplete_logging(sftp_env, monkeypatch):
    api, _server_root, _local_dir = sftp_env
    lines = _logs(api, monkeypatch)
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: [_attr("missing.txt", size=None)])

    incomplete = api.calc_remote_size("/", "incomplete")
    monkeypatch.setattr(api._sftp, "listdir_attr", lambda path: [])
    complete = api.calc_remote_size("/", "complete")

    incomplete_lines = [line for line in lines if line[2].get("event") == "remote size incomplete"]
    assert len(incomplete_lines) == 1
    assert incomplete_lines[0][1] == "warn"
    assert incomplete_lines[0][2]["items"] == incomplete["problem_count"]
    assert complete["complete"] is True
