"""
Integration tests for the transfer queue backend (transfer_queue.py wired
into simple_sftp_client.Api) against a real, in-process paramiko SFTP server.

The server, the connected Api, and the small polling helpers all come from
fixtures in conftest.py (sftp_env, wait_for_drain, state_of). Nothing is
installed or left running.
"""
import os
import time

from transfer_queue import COMPLETED, CANCELLED, WAITING


def test_upload_byte_integrity(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    data = os.urandom(64 * 1024 + 37)  # not a round chunk multiple, on purpose
    src = local_dir / "up.bin"
    src.write_bytes(data)

    result = api.enqueue([{"name": "up.bin", "is_dir": False}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    item_id = api.queue.snapshot()[0]["id"]

    wait_for_drain(api)

    entry = state_of(api, item_id)
    assert entry["state"] == COMPLETED
    served_copy = server_root / "up.bin"
    assert served_copy.read_bytes() == data


def test_download_byte_integrity(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    data = os.urandom(96 * 1024 + 5)
    (server_root / "down.bin").write_bytes(data)

    result = api.enqueue([{"name": "down.bin", "is_dir": False}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    item_id = api.queue.snapshot()[0]["id"]

    wait_for_drain(api)

    entry = state_of(api, item_id)
    assert entry["state"] == COMPLETED
    local_copy = local_dir / "down.bin"
    assert local_copy.read_bytes() == data


def test_multiple_files_drain_in_order_and_all_complete(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    names = [f"file{i}.bin" for i in range(4)]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(2048))

    jobs = [{"name": name, "is_dir": False} for name in names]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["queued"] == len(names)

    wait_for_drain(api)

    snap = api.queue.snapshot()
    assert len(snap) == len(names)
    assert all(entry["state"] == COMPLETED for entry in snap)
    assert api.queue.pending() == 0


def test_cancel_waiting_item_behind_a_slower_active_one(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    # a few MB, so the first item's byte loop keeps the worker busy long
    # enough for the second (waiting) item to be cancelled deterministically
    big = local_dir / "big.bin"
    big.write_bytes(os.urandom(4 * 1024 * 1024))
    small = local_dir / "small.bin"
    small.write_bytes(os.urandom(16))

    jobs = [{"name": "big.bin", "is_dir": False}, {"name": "small.bin", "is_dir": False}]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    snap = api.queue.snapshot()
    big_id = next(e["id"] for e in snap if e["name"] == "big.bin")
    small_id = next(e["id"] for e in snap if e["name"] == "small.bin")

    # wait until the big item is claimed (active) before cancelling the one
    # still waiting behind it
    deadline = time.time() + 15
    while time.time() < deadline:
        entry = state_of(api, big_id)
        if entry["state"] != WAITING:
            break
        time.sleep(0.02)

    assert api.cancel_item(small_id) == {"ok": True}

    wait_for_drain(api)

    assert state_of(api, small_id)["state"] == CANCELLED
    assert state_of(api, big_id)["state"] == COMPLETED
    served_small = server_root / "small.bin"
    assert not served_small.exists()


def test_enqueue_locked_out_while_legacy_transfer_active(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "file.bin").write_bytes(os.urandom(16))

    api._legacy_active.set()
    try:
        result = api.enqueue([{"name": "file.bin", "is_dir": False}], "upload",
                              str(local_dir), "/", "overwrite")
    finally:
        api._legacy_active.clear()

    assert result["ok"] is False
    assert "sync" in result["error"] or "watch" in result["error"]
    assert api.queue.pending() == 0


def test_upload_paths_locked_out_while_queue_pending(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    # a few MB, so the item is reliably still pending when upload_paths is
    # called a moment later, instead of racing the worker to drain first
    queued_file = local_dir / "queued.bin"
    queued_file.write_bytes(os.urandom(4 * 1024 * 1024))

    result = api.enqueue([{"name": "queued.bin", "is_dir": False}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    dropped_file = local_dir / "dropped.bin"
    dropped_file.write_bytes(os.urandom(16))

    result = api.upload_paths([str(dropped_file)], "/", "overwrite")

    assert result["ok"] is False
    assert "queue" in result["error"].lower()

    wait_for_drain(api)
