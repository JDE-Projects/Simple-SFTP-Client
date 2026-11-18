"""Tests that a source changing during transfer is never published."""

import os
import re

import paramiko
import pytest

from app.services import transfer_io
from app.transfer_queue import COMPLETED, SKIPPED


def _local_scratch_files(folder):
    return [name for name in os.listdir(folder)
            if re.fullmatch(r"\..+\.[0-9a-f]{8}\.sxtpart", name)]


def _remote_scratch_files(folder):
    return [name for name in os.listdir(folder)
            if re.fullmatch(r"\..+\.[0-9a-f]{8}\.sxtpart", name)]


def _rewrite_same_size(path, data):
    before = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


def _enqueue_one(api, direction, local_dir, name, on_conflict, wait_for_queue_count):
    before = len(api._queue.snapshot())
    result = api.enqueue([{"name": name, "is_dir": False}], direction,
                         str(local_dir), "/", on_conflict)
    assert result["ok"] is True
    wait_for_queue_count(api, before + 1)
    return api._queue.snapshot()[-1]["id"]


def test_upload_same_size_rewrite_mid_transfer_is_refused(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    source = local_dir / "upload.bin"
    source.write_bytes(b"A" * (64 * 1024))
    (server_root / source.name).write_bytes(b"old remote copy")
    renamed = []
    real_rename = api._sftp.posix_rename

    def record_rename(*args):
        renamed.append(args)
        return real_rename(*args)

    changed = False

    def mutate_after_first_chunk(*_args):
        nonlocal changed
        if not changed:
            changed = True
            _rewrite_same_size(source, b"B" * (64 * 1024))

    monkeypatch.setattr(api._sftp, "posix_rename", record_rename)
    with pytest.raises(IOError, match="changed"):
        transfer_io._put_file(api, api._sftp, str(source), f"/{source.name}",
                              mutate_after_first_chunk, lambda: False)

    assert renamed == []
    assert (server_root / source.name).read_bytes() == b"old remote copy"
    assert _remote_scratch_files(server_root) == []


def test_upload_source_replaced_by_rename_is_refused(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    source = local_dir / "upload.bin"
    source.write_bytes(b"A" * (64 * 1024))
    replacement = local_dir / "replacement.bin"
    replacement.write_bytes(b"B" * (64 * 1024))
    (server_root / source.name).write_bytes(b"old remote copy")
    changed = False
    real_stat = transfer_io.os.stat
    replacement_stat = replacement.stat()

    def replace_after_first_chunk(*_args):
        nonlocal changed
        if not changed:
            changed = True

    def replaced_path_stat(path, *args, **kwargs):
        if changed and os.fspath(path) == str(source):
            return replacement_stat
        return real_stat(path, *args, **kwargs)

    # Windows prevents os.replace while the source remains open, which is the
    # condition the transfer deliberately maintains through publication. Model
    # the replaced path at the stat boundary used by the final safety check.
    monkeypatch.setattr(transfer_io.os, "stat", replaced_path_stat)
    with pytest.raises(IOError, match="changed"):
        transfer_io._put_file(api, api._sftp, str(source), f"/{source.name}",
                              replace_after_first_chunk, lambda: False)

    assert (server_root / source.name).read_bytes() == b"old remote copy"


def test_upload_source_deleted_before_publish_is_refused(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    source = local_dir / "upload.bin"
    source.write_bytes(b"A" * (64 * 1024))
    (server_root / source.name).write_bytes(b"old remote copy")
    changed = False
    real_stat = transfer_io.os.stat

    def remove_after_first_chunk(*_args):
        nonlocal changed
        if not changed:
            changed = True

    def missing_path_stat(path, *args, **kwargs):
        if changed and os.fspath(path) == str(source):
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    # Windows also prevents deletion or rename-away while this open source
    # handle exists, so model the missing path at the final stat boundary.
    monkeypatch.setattr(transfer_io.os, "stat", missing_path_stat)
    with pytest.raises(IOError, match="changed"):
        transfer_io._put_file(api, api._sftp, str(source), f"/{source.name}",
                              remove_after_first_chunk, lambda: False)

    assert (server_root / source.name).read_bytes() == b"old remote copy"


def test_upload_unchanged_source_publishes(sftp_env):
    api, server_root, local_dir = sftp_env
    source = local_dir / "upload.bin"
    source.write_bytes(b"source bytes")
    mtime = 1_700_000_000
    os.utime(source, (mtime, mtime))

    assert transfer_io._put_file(api, api._sftp, str(source), f"/{source.name}",
                                 lambda *_: None, lambda: False)

    assert (server_root / source.name).read_bytes() == b"source bytes"
    assert int((server_root / source.name).stat().st_mtime) == mtime


def test_download_same_size_rewrite_mid_transfer_is_refused(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    name = "download.bin"
    (server_root / name).write_bytes(b"remote bytes")
    destination = local_dir / name
    destination.write_bytes(b"old local copy")
    real_stat = api._sftp.stat
    calls = 0

    def changed_final_stat(path):
        nonlocal calls
        calls += 1
        result = real_stat(path)
        if calls == 2:
            result.st_mtime += 1
        return result

    monkeypatch.setattr(api._sftp, "stat", changed_final_stat)
    with pytest.raises(IOError, match="changed"):
        transfer_io._get_file(api, api._sftp, f"/{name}", str(destination),
                              lambda *_: None, lambda: False)

    assert destination.read_bytes() == b"old local copy"
    assert _local_scratch_files(local_dir) == []


def test_download_size_change_mid_transfer_is_refused(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    name = "download.bin"
    (server_root / name).write_bytes(b"remote bytes")
    destination = local_dir / name
    destination.write_bytes(b"old local copy")
    real_stat = api._sftp.stat
    calls = 0

    def changed_final_stat(path):
        nonlocal calls
        calls += 1
        result = real_stat(path)
        if calls == 2:
            result.st_size += 1
        return result

    monkeypatch.setattr(api._sftp, "stat", changed_final_stat)
    with pytest.raises(IOError, match="changed"):
        transfer_io._get_file(api, api._sftp, f"/{name}", str(destination),
                              lambda *_: None, lambda: False)

    assert destination.read_bytes() == b"old local copy"


def test_download_no_server_mtime_publishes_size_only(sftp_env_no_times):
    api, server_root, local_dir = sftp_env_no_times
    name = "download.bin"
    (server_root / name).write_bytes(b"remote bytes")

    assert transfer_io._get_file(api, api._sftp, f"/{name}", str(local_dir / name),
                                 lambda *_: None, lambda: False)

    with api._console_lock:
        warnings = [line["msg"] for line in api._console_buffer
                    if "no modification time" in line["msg"]]
    assert len(warnings) == 1
    assert api._mtime_fallback_matches(str(local_dir / name), f"/{name}", len(b"remote bytes"))


def test_download_pre_read_mtime_missing_records_fallback(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    name = "download.bin"
    data = b"remote bytes"
    (server_root / name).write_bytes(data)
    real_stat = api._sftp.stat
    calls = 0

    def missing_first_mtime(path):
        nonlocal calls
        calls += 1
        result = real_stat(path)
        if calls == 1:
            result.st_mtime = None
        return result

    monkeypatch.setattr(api._sftp, "stat", missing_first_mtime)
    assert transfer_io._get_file(api, api._sftp, f"/{name}", str(local_dir / name),
                                 lambda *_: None, lambda: False)

    assert api._mtime_fallback_matches(str(local_dir / name), f"/{name}", len(data))


def test_detection_clears_mtime_fallback(sftp_env):
    api, server_root, local_dir = sftp_env
    upload = local_dir / "upload.bin"
    upload.write_bytes(b"A" * (64 * 1024))
    (server_root / upload.name).write_bytes(b"old remote copy")
    api._record_mtime_fallback(str(upload), f"/{upload.name}", upload.stat().st_size)
    changed = False

    def mutate_upload(*_args):
        nonlocal changed
        if not changed:
            changed = True
            _rewrite_same_size(upload, b"B" * (64 * 1024))

    with pytest.raises(IOError, match="changed"):
        transfer_io._put_file(api, api._sftp, str(upload), f"/{upload.name}",
                              mutate_upload, lambda: False)
    assert api._mtime_fallback == {}

    name = "download.bin"
    (server_root / name).write_bytes(b"remote bytes")
    api._record_mtime_fallback(str(local_dir / name), f"/{name}", len(b"remote bytes"))
    real_stat = api._sftp.stat
    calls = 0

    def mutate_download_stat(path):
        nonlocal calls
        calls += 1
        result = real_stat(path)
        if calls == 2:
            result.st_size += 1
        return result

    api._sftp.stat = mutate_download_stat
    with pytest.raises(IOError, match="changed"):
        transfer_io._get_file(api, api._sftp, f"/{name}", str(local_dir / name),
                              lambda *_: None, lambda: False)
    assert api._mtime_fallback == {}


def test_queue_retry_after_mutation_does_not_skip(
        sftp_env, monkeypatch, wait_for_drain, wait_for_queue_count, state_of):
    api, server_root, local_dir = sftp_env
    name = "queue.bin"
    source = local_dir / name
    source.write_bytes(b"A" * (64 * 1024))
    (server_root / name).write_bytes(b"old remote copy")
    api._record_mtime_fallback(str(source), f"/{name}", source.stat().st_size)
    real_matches = api._mtime_fallback_matches
    checked = False

    def force_first_transfer(*args):
        nonlocal checked
        if not checked:
            checked = True
            return False
        return real_matches(*args)

    changed = False
    real_progress = api._progress

    def mutate_during_first_attempt(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            _rewrite_same_size(source, b"B" * (64 * 1024))
        return real_progress(*args, **kwargs)

    monkeypatch.setattr(api, "_mtime_fallback_matches", force_first_transfer)
    monkeypatch.setattr(api, "_progress", mutate_during_first_attempt)
    item_id = _enqueue_one(api, "upload", local_dir, name, "skip", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert state_of(api, item_id)["state"] != SKIPPED
    assert (server_root / name).read_bytes() == b"B" * (64 * 1024)


def test_watch_upload_mutation_logs_error_and_stays_pending(sftp_env, monkeypatch, wait_until):
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.3
    name = "watch.bin"
    source = local_dir / name
    (server_root / name).write_bytes(b"old remote copy")
    real_write = paramiko.SFTPFile.write
    changed = False

    def mutate_after_write(sftp_file, data):
        nonlocal changed
        result = real_write(sftp_file, data)
        if not changed:
            changed = True
            _rewrite_same_size(source, b"B" * (64 * 1024))
        return result

    monkeypatch.setattr(paramiko.SFTPFile, "write", mutate_after_write)
    assert api.start_watch(str(local_dir), "/")["ok"] is True
    source.write_bytes(b"A" * (64 * 1024))
    lines = []

    def watch_failed():
        lines.extend(api.poll_queue()["console"])
        return any(line["msg"].startswith(f"Watch error: {name} - ") for line in lines)

    try:
        wait_until(watch_failed, timeout=6)
    finally:
        api.stop_watch()

    assert (server_root / name).read_bytes() == b"old remote copy"
    assert not any(line["msg"].startswith("Watch: uploaded ") for line in lines)
