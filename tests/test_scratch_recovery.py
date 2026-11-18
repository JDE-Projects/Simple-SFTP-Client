"""Scratch ownership recovery and visibility tests."""

import json
import os

import paramiko
import pytest

from app import paths, scratch_journal
from simple_sftp_client import _report_recovery


def _entry(journal, scratch, final, identity=None):
    journal.register(str(scratch), str(final))
    if identity is not None:
        journal.set_identity(str(scratch), identity)


def _identity(path):
    result = os.stat(path)
    return result.st_dev, result.st_ino


def test_recover_deletes_owned_leftover(tmp_path):
    scratch = tmp_path / ".file.bin.deadbeef.sxtpart"
    final = tmp_path / "file.bin"
    scratch.write_bytes(b"partial")
    journal = scratch_journal.current
    _entry(journal, scratch, final, _identity(scratch))
    summary = journal.recover()
    assert summary["deleted"] == 1
    assert not scratch.exists() and not journal.owns(scratch)


def test_recover_retires_missing_file(tmp_path):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    journal = scratch_journal.current
    _entry(journal, scratch, final)
    assert journal.recover()["deleted"] == 0
    assert not journal.owns(scratch)


def test_recover_leaves_identity_mismatch(tmp_path):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    scratch.write_bytes(b"user data")
    journal = scratch_journal.current
    _entry(journal, scratch, final, (0, 0))
    summary = journal.recover()
    assert scratch.read_bytes() == b"user data"
    assert str(scratch) in summary["left_in_place"] and not journal.owns(scratch)


def test_recover_leaves_null_identity(tmp_path):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    scratch.write_bytes(b"user data")
    journal = scratch_journal.current
    _entry(journal, scratch, final)
    assert str(scratch) in journal.recover()["left_in_place"]
    assert scratch.exists() and not journal.owns(scratch)


def test_recover_skips_non_regular(tmp_path):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    scratch.mkdir()
    journal = scratch_journal.current
    _entry(journal, scratch, final, (0, 0))
    assert str(scratch) in journal.recover()["left_in_place"]
    assert scratch.is_dir() and not journal.owns(scratch)


def test_recover_keeps_unreachable_entry(tmp_path, monkeypatch):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    scratch.write_bytes(b"partial")
    journal = scratch_journal.current
    _entry(journal, scratch, final, _identity(scratch))
    real_lstat = scratch_journal.os.lstat
    monkeypatch.setattr(scratch_journal.os, "lstat",
                        lambda path: (_ for _ in ()).throw(PermissionError()) if str(path) == str(scratch) else real_lstat(path))
    assert str(scratch) in journal.recover()["unreachable"]
    assert journal.owns(scratch)


def test_recover_keeps_entry_when_delete_fails(tmp_path, monkeypatch):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    scratch.write_bytes(b"partial")
    journal = scratch_journal.current
    _entry(journal, scratch, final, _identity(scratch))
    monkeypatch.setattr(scratch_journal.os, "remove",
                        lambda path: (_ for _ in ()).throw(PermissionError()))
    assert journal.recover()["deleted"] == 0
    assert scratch.exists() and journal.owns(scratch)


def test_recover_ignores_invalid_entries(tmp_path):
    user_file = tmp_path / "user.bin"
    user_file.write_bytes(b"user")
    invalid = {"scratch": str(user_file), "final": str(tmp_path / "elsewhere"), "identity": None}
    with open(paths.SCRATCH_JOURNAL_FILE, "w", encoding="utf-8") as file:
        json.dump([invalid], file)
    journal = scratch_journal.current
    journal.close()
    assert journal.open()
    journal.recover()
    assert user_file.read_bytes() == b"user"
    assert invalid in json.loads(open(paths.SCRATCH_JOURNAL_FILE, encoding="utf-8").read())


def test_recover_after_hard_exit_state(tmp_path):
    scratch, final = tmp_path / ".file.bin.deadbeef.sxtpart", tmp_path / "file.bin"
    scratch.write_bytes(b"partial")
    first = scratch_journal.current
    _entry(first, scratch, final, _identity(scratch))
    first.close()
    second = scratch_journal.ScratchJournal()
    assert second.open()
    scratch_journal.current = second
    assert second.recover()["deleted"] == 1 and not scratch.exists()
    second.close()
    scratch_journal.current = first


def test_malformed_journal_recovers_nothing(tmp_path):
    scratch = tmp_path / ".file.bin.deadbeef.sxtpart"
    scratch.write_bytes(b"user")
    with open(paths.SCRATCH_JOURNAL_FILE, "w", encoding="utf-8") as file:
        file.write("not json")
    journal = scratch_journal.current
    journal.close()
    assert journal.open()
    assert journal.recover()["deleted"] == 0
    assert scratch.read_bytes() == b"user"


def test_user_file_matching_pattern_is_visible_and_kept(sftp_env):
    api, _server_root, local_dir = sftp_env
    user_file = local_dir / ".foo.deadbeef.sxtpart"
    user_file.write_bytes(b"user")
    assert user_file.name in [entry["name"] for entry in api.list_local(str(local_dir))["entries"]]
    assert any(path == str(user_file) for path, *_ in api._iter_local(str(user_file), "/x", False))
    assert str(user_file) in [path for path, *_ in api._iter_local(str(local_dir), "/x", True)]
    assert user_file.exists()


def test_active_download_scratch_is_hidden_locally(sftp_env):
    api, _server_root, local_dir = sftp_env
    scratch, final = local_dir / ".file.bin.deadbeef.sxtpart", local_dir / "file.bin"
    scratch.write_bytes(b"partial")
    scratch_journal.current.register(str(scratch), str(final))
    assert scratch.name not in [entry["name"] for entry in api.list_local(str(local_dir))["entries"]]
    assert list(api._iter_local(str(scratch), "/x", False)) == []
    assert str(scratch) not in [path for path, *_ in api._iter_local(str(local_dir), "/x", True)]


def test_remote_user_file_matching_pattern_is_visible(sftp_env):
    api, server_root, local_dir = sftp_env
    (server_root / ".foo.deadbeef.sxtpart").write_bytes(b"user")
    assert ".foo.deadbeef.sxtpart" in [entry["name"] for entry in api.list_remote("/")["entries"]]
    assert list(api._iter_remote(api._sftp, "/.foo.deadbeef.sxtpart", str(local_dir / "x"), False, str(local_dir)))
    assert api.calc_remote_size("/", ".") ["files"] == 1


def test_active_upload_scratch_is_hidden_remotely(sftp_env):
    api, server_root, local_dir = sftp_env
    scratch = "/.foo.deadbeef.sxtpart"
    (server_root / scratch[1:]).write_bytes(b"partial")
    api._add_active_remote_scratch(scratch)
    assert scratch[1:] not in [entry["name"] for entry in api.list_remote("/")["entries"]]
    assert list(api._iter_remote(api._sftp, scratch, str(local_dir / "x"), False, str(local_dir))) == []
    assert api.calc_remote_size("/", ".")["files"] == 0


def test_directly_selected_active_scratch_is_skipped(sftp_env):
    api, server_root, local_dir = sftp_env
    local = local_dir / ".local.deadbeef.sxtpart"
    local.write_bytes(b"partial")
    scratch_journal.current.register(str(local), str(local_dir / "local"))
    remote = "/.remote.deadbeef.sxtpart"
    (server_root / remote[1:]).write_bytes(b"partial")
    api._add_active_remote_scratch(remote)
    assert list(api._iter_local(str(local), "/x", False)) == []
    assert list(api._iter_remote(api._sftp, remote, str(local_dir / "x"), False, str(local_dir))) == []


def test_upload_tracks_active_scratch_then_clears(sftp_env, monkeypatch):
    api, _server_root, local_dir = sftp_env
    source = local_dir / "upload.bin"
    source.write_bytes(b"data")
    seen = []
    real_open = paramiko.SFTPClient.open

    def observe_open(client, path, mode="r", *args, **kwargs):
        if mode == "w":
            seen.append(api._is_active_remote_scratch(path))
        return real_open(client, path, mode, *args, **kwargs)

    monkeypatch.setattr(paramiko.SFTPClient, "open", observe_open)
    assert api._put_file(api._sftp, str(source), "/upload.bin", lambda *_: None, lambda: False)
    assert seen == [True] and not api._active_remote_scratch
    with pytest.raises(RuntimeError, match="stop"):
        api._put_file(api._sftp, str(source), "/failed.bin",
                      lambda *_: (_ for _ in ()).throw(RuntimeError("stop")), lambda: False)
    assert not api._active_remote_scratch


def test_main_reports_recovery_summary():
    calls = []
    class Api:
        def _vlog(self, *args):
            calls.append(args)
    _report_recovery(Api(), {"deleted": 0, "left_in_place": [], "unreachable": []})
    assert calls == []
    _report_recovery(Api(), {"deleted": 2, "left_in_place": ["left"], "unreachable": ["gone"]})
    assert calls[0][1] == "ok" and "2" in calls[0][0]
    assert calls[1][1] == calls[2][1] == "warn"
    assert "left" in calls[1][0] and "gone" in calls[2][0]
