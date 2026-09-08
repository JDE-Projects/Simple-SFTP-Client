"""
Tests for Option B timestamp preservation: a transfer stamps the destination
with the source's modification time, so a later size+mtime compare reads an
unchanged file as "same" instead of a false edit.

Runs against the in-process SFTP server from conftest.py (sftp_env and the
set-time-unsupported variant, sftp_env_no_set_time).
"""
import os

import simple_sftp_client
from transfer_queue import COMPLETED

from simple_sftp_client import MTIME_TOL


def _enqueue_one(api, direction, local_dir, remote_dir, name, on_conflict, wait_for_queue_count):
    before = len(api.queue.snapshot())
    result = api.enqueue([{"name": name, "is_dir": False}], direction,
                          str(local_dir), remote_dir, on_conflict)
    assert result["ok"] is True
    wait_for_queue_count(api, before + 1)
    return api.queue.snapshot()[-1]["id"]


def test_download_preserves_remote_mtime(sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    data = os.urandom(4096)
    remote_path = server_root / name
    remote_path.write_bytes(data)
    known_mtime = 1_700_000_000
    os.utime(remote_path, (known_mtime, known_mtime))

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    local_mtime = int((local_dir / name).stat().st_mtime)
    assert abs(local_mtime - known_mtime) <= MTIME_TOL


def test_upload_preserves_local_mtime(sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    data = os.urandom(4096)
    local_path = local_dir / name
    local_path.write_bytes(data)
    known_mtime = 1_700_000_000
    os.utime(local_path, (known_mtime, known_mtime))

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    remote_mtime = int((server_root / name).stat().st_mtime)
    assert abs(remote_mtime - known_mtime) <= MTIME_TOL


def test_upload_completes_when_server_refuses_to_set_time(
        sftp_env_no_set_time, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env_no_set_time
    name = "up.bin"
    data = os.urandom(64 * 1024 + 17)
    (local_dir / name).write_bytes(data)

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    entry = state_of(api, item_id)
    assert entry["state"] == COMPLETED
    assert (server_root / name).read_bytes() == data


def test_download_completes_when_local_stamp_fails(
        sftp_env, monkeypatch, wait_for_queue_count, wait_for_drain, state_of):
    """The download-side mirror of the upload fallback above: if stamping the
    freshly downloaded local file's modification time fails (a locked file,
    a filesystem that rejects utime), the transfer must still complete, since
    the bytes are already safely in place. Falls back to size-only equality
    for this file on a later compare, and that fallback must be logged."""
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    data = os.urandom(4096)
    remote_path = server_root / name
    remote_path.write_bytes(data)

    target_local_path = os.path.abspath(str(local_dir / name))
    real_utime = os.utime

    def _raise_only_for_target(path, *args, **kwargs):
        if os.path.abspath(path) == target_local_path:
            raise OSError("simulated: cannot set local file time")
        return real_utime(path, *args, **kwargs)

    # Patch narrowly on the module simple_sftp_client actually uses (its own
    # `import os`), and only for the one path being downloaded, so nothing
    # else in the test (fixture teardown, etc.) is affected.
    monkeypatch.setattr(simple_sftp_client.os, "utime", _raise_only_for_target)

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    entry = state_of(api, item_id)
    assert entry["state"] == COMPLETED
    assert (local_dir / name).read_bytes() == data
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("could not set modification time" in m for m in messages)
