"""
Tests for the write-to-temp-then-atomic-swap transfer pattern: a cancel, a
dropped connection, or an exhausted retry must never touch the real
destination file, and must never leave a .sxtpart scratch file behind except
while a transfer is actually in flight.

Runs against the in-process SFTP server from conftest.py (sftp_env and the
posix-rename-unsupported variant, sftp_env_no_posix_rename).
"""
import os
import time

from transfer_queue import COMPLETED, CANCELLED, FAILED, WAITING

from simple_sftp_client import is_temp_part


def _local_temp_files(folder):
    return [n for n in os.listdir(folder) if is_temp_part(n)]


def _remote_temp_files(server_root):
    return [n for n in os.listdir(server_root) if is_temp_part(n)]


def _wait_until_active(api, item_id, state_of, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state_of(api, item_id)["state"] != WAITING:
            return
        time.sleep(0.02)


def _enqueue_one(api, direction, local_dir, remote_dir, name, on_conflict, wait_for_queue_count):
    before = len(api.queue.snapshot())
    result = api.enqueue([{"name": name, "is_dir": False}], direction,
                          str(local_dir), remote_dir, on_conflict)
    assert result["ok"] is True
    wait_for_queue_count(api, before + 1)
    return api.queue.snapshot()[-1]["id"]


# ───────────── download: cancel keeps the original ─────────────

def test_download_cancel_leaves_existing_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    original = b"O" * 4096
    (local_dir / name).write_bytes(original)
    # big enough that the byte loop is still running when cancel_item lands
    (server_root / name).write_bytes(os.urandom(6 * 1024 * 1024))

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    _wait_until_active(api, item_id, state_of)
    assert api.cancel_item(item_id) == {"ok": True}

    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == CANCELLED
    assert (local_dir / name).read_bytes() == original
    assert _local_temp_files(local_dir) == []


# ───────────── download: exhausted retries keep the original ─────────────

def test_download_retry_exhausted_leaves_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    original = b"O" * 4096
    (local_dir / name).write_bytes(original)
    (server_root / name).write_bytes(os.urandom(64 * 1024))

    # simulate a connection drop partway through every attempt: the progress
    # callback runs mid-stream, so raising there interrupts the byte loop
    # exactly like a real dropped connection would.
    def always_fail(*args, **kwargs):
        raise OSError("simulated dropped connection")
    api._progress = always_fail

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == FAILED
    assert (local_dir / name).read_bytes() == original
    assert _local_temp_files(local_dir) == []


# ───────────── download: success cleans up ─────────────

def test_download_success_writes_correct_bytes_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    data = os.urandom(96 * 1024 + 5)
    (server_root / name).write_bytes(data)

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (local_dir / name).read_bytes() == data
    assert _local_temp_files(local_dir) == []


# ───────────── upload: cancel keeps the original ─────────────

def test_upload_cancel_leaves_existing_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    original = b"O" * 4096
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(os.urandom(6 * 1024 * 1024))

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    _wait_until_active(api, item_id, state_of)
    assert api.cancel_item(item_id) == {"ok": True}

    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == CANCELLED
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── upload: exhausted retries keep the original ─────────────

def test_upload_retry_exhausted_leaves_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    original = b"O" * 4096
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(os.urandom(64 * 1024))

    def always_fail(*args, **kwargs):
        raise OSError("simulated dropped connection")
    api._progress = always_fail

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == FAILED
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── upload: success cleans up ─────────────

def test_upload_success_writes_correct_bytes_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    data = os.urandom(64 * 1024 + 37)
    (local_dir / name).write_bytes(data)

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == data
    assert _remote_temp_files(server_root) == []


# ───────────── upload: refuse-and-keep when the server cannot rename ─────────────

def test_upload_refuses_and_keeps_original_when_posix_rename_unsupported(
        sftp_env_no_posix_rename, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env_no_posix_rename
    name = "up.bin"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(b"new local content that must never land")

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    entry = state_of(api, item_id)
    assert entry["state"] == FAILED
    assert "server does not support safe atomic replace" in entry["error"]
    assert "file not written to protect the existing copy" in entry["error"]
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── listing hides scratch files ─────────────

def test_list_local_hides_temp_part_files(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "real.bin").write_bytes(b"data")
    (local_dir / ".real.bin.deadbeef.sxtpart").write_bytes(b"partial")

    result = api.list_local(str(local_dir))
    assert result["ok"] is True
    names = [e["name"] for e in result["entries"]]
    assert "real.bin" in names
    assert not any(is_temp_part(n) for n in names)


def test_list_remote_hides_temp_part_files(sftp_env):
    api, server_root, local_dir = sftp_env
    (server_root / "real.bin").write_bytes(b"data")
    (server_root / ".real.bin.deadbeef.sxtpart").write_bytes(b"partial")

    result = api.list_remote("/")
    assert result["ok"] is True
    names = [e["name"] for e in result["entries"]]
    assert "real.bin" in names
    assert not any(is_temp_part(n) for n in names)


def test_delete_remote_folder_removes_leftover_scratch_file(sftp_env):
    # A folder holding a leftover scratch file must still delete cleanly:
    # the delete removes the scratch too, so the final rmdir does not fail on
    # a non-empty directory. Hiding scratch files is for listings, never for
    # deletion.
    api, server_root, local_dir = sftp_env
    folder = server_root / "sub"
    folder.mkdir()
    (folder / "real.bin").write_bytes(b"data")
    (folder / ".real.bin.deadbeef.sxtpart").write_bytes(b"partial")

    result = api.delete("remote", "/", [{"name": "sub", "is_dir": True}])
    assert result["ok"] is True
    assert result["errors"] == []
    assert not folder.exists()


def test_compare_hides_temp_part_files_on_both_sides(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "same.bin").write_bytes(b"AAAA")
    (server_root / "same.bin").write_bytes(b"AAAA")
    (local_dir / ".same.bin.deadbeef.sxtpart").write_bytes(b"scratch")
    (server_root / ".other.bin.cafebabe.sxtpart").write_bytes(b"scratch")

    result = api.compare(str(local_dir), "/")
    assert result["ok"] is True
    assert not any(is_temp_part(n) for n in result["result"])
    assert result["result"]["same.bin"] == "same"
