"""
Tests that on_conflict="overwrite" actually resends a file, even when the copy
on the other side is the same size. Size alone does not prove the contents
match, so overwrite must not skip. on_conflict="skip" keeps skipping.

Also guards against splicing: when the destination is smaller than the source,
the transfer must rewrite the whole file, not keep the old head and append the
new tail. That splice used to pass silently and even matched the source size.

Runs against the in-process SFTP server from conftest.py.
"""
import os
import time

from transfer_queue import COMPLETED, SKIPPED


def _download_one(api, local_dir, name):
    before = len(api.queue.snapshot())
    result = api.enqueue([{"name": name, "is_dir": False}], "download",
                         str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    _wait_for_new_item(api, before)
    return api.queue.snapshot()[-1]["id"]


def _upload_one(api, local_dir, name, on_conflict):
    before = len(api.queue.snapshot())
    result = api.enqueue([{"name": name, "is_dir": False}], "upload",
                          str(local_dir), "/", on_conflict)
    assert result["ok"] is True
    _wait_for_new_item(api, before)
    return api.queue.snapshot()[-1]["id"]


def _wait_for_new_item(api, before, timeout=15):
    """Scanning is async now: poll until a new item lands on the queue rather
    than assuming it is already there the instant enqueue() returns."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(api.queue.snapshot()) > before:
            return
        time.sleep(0.02)
    raise AssertionError(f"no new queue item appeared within {timeout}s")


def test_overwrite_resends_same_size_changed_file(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "same_size.bin"
    original = b"A" * 4096
    changed = b"B" * 4096  # identical size, different bytes

    src = local_dir / name
    src.write_bytes(original)
    first_id = _upload_one(api, local_dir, name, "overwrite")
    wait_for_drain(api)
    assert state_of(api, first_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == original

    # change the local bytes without changing the size, then overwrite again
    src.write_bytes(changed)
    second_id = _upload_one(api, local_dir, name, "overwrite")
    wait_for_drain(api)

    assert state_of(api, second_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == changed


def test_skip_leaves_same_size_file_untouched(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "same_size.bin"
    original = b"A" * 4096
    changed = b"B" * 4096

    src = local_dir / name
    src.write_bytes(original)
    first_id = _upload_one(api, local_dir, name, "skip")
    wait_for_drain(api)
    assert state_of(api, first_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == original

    # same size and same modification time as what the first upload stamped
    # on the remote, but different bytes: skip matches on size and time, not
    # content, so it still leaves the remote copy alone.
    stamped_mtime = src.stat().st_mtime
    src.write_bytes(changed)
    os.utime(src, (stamped_mtime, stamped_mtime))
    second_id = _upload_one(api, local_dir, name, "skip")
    wait_for_drain(api)

    assert state_of(api, second_id)["state"] == SKIPPED
    assert (server_root / name).read_bytes() == original


def test_overwrite_rewrites_larger_local_over_smaller_remote(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "changed.bin"
    # remote is a shorter, older version; local is longer and different
    (server_root / name).write_bytes(b"old short remote")
    src = local_dir / name
    src.write_bytes(b"new local content that is clearly longer than the remote copy")

    item_id = _upload_one(api, local_dir, name, "overwrite")
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    # the whole local file is on the server, not the old head + new tail
    assert (server_root / name).read_bytes() == src.read_bytes()


def test_overwrite_rewrites_larger_remote_over_smaller_local(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "changed.bin"
    # local is a shorter, older version; remote is longer and different
    dst = local_dir / name
    dst.write_bytes(b"old short local")
    remote_bytes = b"new remote content that is clearly longer than the local copy"
    (server_root / name).write_bytes(remote_bytes)

    item_id = _download_one(api, local_dir, name)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert dst.read_bytes() == remote_bytes


def test_skip_reruns_folder_download_skipping_matching_files(sftp_env, wait_for_drain):
    """A folder download that is re-run with on_conflict="skip" leaves an
    already-matching local file alone (same size and modification time) and
    re-sends a file whose remote copy has since changed size."""
    api, server_root, local_dir = sftp_env
    (server_root / "top").mkdir()
    (server_root / "top" / "same.bin").write_bytes(b"S" * 4096)
    (server_root / "top" / "changed.bin").write_bytes(b"C" * 4096)

    result = api.enqueue([{"name": "top", "is_dir": True}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_drain(api)

    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    assert states["same.bin"] == COMPLETED
    assert states["changed.bin"] == COMPLETED

    # same.bin is left untouched on both sides; changed.bin's remote copy
    # shrinks, so a partial/older-looking local copy has something real to
    # catch up on
    (server_root / "top" / "changed.bin").write_bytes(b"c" * 10)

    before = len(api.queue.snapshot())
    result = api.enqueue([{"name": "top", "is_dir": True}], "download",
                          str(local_dir), "/", "skip")
    assert result["ok"] is True
    wait_for_drain(api)

    new_states = {e["name"]: e["state"] for e in api.queue.snapshot()[before:]}
    assert new_states["same.bin"] == SKIPPED
    assert new_states["changed.bin"] == COMPLETED
    assert (local_dir / "top" / "same.bin").read_bytes() == b"S" * 4096
    assert (local_dir / "top" / "changed.bin").read_bytes() == b"c" * 10
