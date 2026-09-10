"""
Tests for carrying empty folders through the transfer/queue path (commit 1 of
the empty-dir-transfers work): _iter_local/_iter_remote, when asked to
include folders, emit a "make folder" marker only for a directory whose whole
subtree has no files, and the worker pool turns that marker into a real
folder on the destination. Compare/sync is untouched by this commit and is
not exercised here.

Runs against the same in-process paramiko SFTP server and sftp_env/
wait_for_drain fixtures as test_streaming_scan.py.
"""
import os

from transfer_queue import COMPLETED, FAILED


def test_upload_of_empty_selected_folder_creates_it_on_remote(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "empty").mkdir()

    result = api.enqueue([{"name": "empty", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    wait_for_drain(api)

    snap = api.queue.snapshot()
    assert len(snap) == 1
    assert snap[0]["is_dir"] is True
    assert snap[0]["state"] == COMPLETED
    assert (server_root / "empty").is_dir()


def test_download_of_empty_remote_folder_creates_it_locally(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (server_root / "empty").mkdir()

    result = api.enqueue([{"name": "empty", "is_dir": True}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    wait_for_drain(api)

    snap = api.queue.snapshot()
    assert len(snap) == 1
    assert snap[0]["is_dir"] is True
    assert snap[0]["state"] == COMPLETED
    assert (local_dir / "empty").is_dir()


def test_nested_empty_folders_are_created_both_ways(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env

    # upload side: a/b/c all empty
    (local_dir / "up_a" / "up_b" / "up_c").mkdir(parents=True)
    result = api.enqueue([{"name": "up_a", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_drain(api)
    assert (server_root / "up_a").is_dir()
    assert (server_root / "up_a" / "up_b").is_dir()
    assert (server_root / "up_a" / "up_b" / "up_c").is_dir()

    api.queue.clear_finished()

    # download side: a/b/c all empty
    (server_root / "dn_a" / "dn_b" / "dn_c").mkdir(parents=True)
    result = api.enqueue([{"name": "dn_a", "is_dir": True}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_drain(api)
    assert (local_dir / "dn_a").is_dir()
    assert (local_dir / "dn_a" / "dn_b").is_dir()
    assert (local_dir / "dn_a" / "dn_b" / "dn_c").is_dir()


def test_mixed_tree_transfers_files_and_creates_only_the_empty_folders(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    top = local_dir / "mixed"
    top.mkdir()
    (top / "has_file").mkdir()
    (top / "has_file" / "f.bin").write_bytes(os.urandom(64))
    (top / "empty_leaf").mkdir()
    (top / "has_file" / "empty_nested").mkdir()

    result = api.enqueue([{"name": "mixed", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_drain(api)

    snap = api.queue.snapshot()
    # exactly one file item and two folder items (empty_leaf, empty_nested);
    # has_file and mixed itself must NOT get their own folder items since
    # they contain a file somewhere in their subtree
    file_items = [e for e in snap if not e["is_dir"]]
    dir_items = [e for e in snap if e["is_dir"]]
    assert len(file_items) == 1
    assert len(dir_items) == 2
    assert all(e["state"] == COMPLETED for e in snap)
    assert (server_root / "mixed" / "has_file" / "f.bin").read_bytes() == \
        (top / "has_file" / "f.bin").read_bytes()
    assert (server_root / "mixed" / "empty_leaf").is_dir()
    assert (server_root / "mixed" / "has_file" / "empty_nested").is_dir()


def test_folder_already_existing_on_destination_is_success_not_failure(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "already").mkdir()
    (server_root / "already").mkdir()  # already there on the destination

    result = api.enqueue([{"name": "already", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_drain(api)

    snap = api.queue.snapshot()
    assert len(snap) == 1
    assert snap[0]["state"] == COMPLETED
    assert (server_root / "already").is_dir()


def test_file_where_folder_must_go_fails_the_item_without_overwriting(sftp_env, wait_for_drain):
    # File-vs-folder collision, doubling as the representative creation
    # failure for this commit: simulating a genuine server-side mkdir error
    # is impractical with this test harness, so the collision path is used
    # to prove a folder-creation failure surfaces as FAILED with a clear
    # message, not a silent drop or an overwrite.
    api, server_root, local_dir = sftp_env
    (local_dir / "collide").mkdir()
    (server_root / "collide").write_bytes(b"already a file")

    result = api.enqueue([{"name": "collide", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    wait_for_drain(api)

    snap = api.queue.snapshot()
    assert len(snap) == 1
    assert snap[0]["state"] == FAILED
    assert "already exists" in snap[0]["error"]
    # nothing overwritten: still a file, still the original bytes
    assert (server_root / "collide").is_file()
    assert (server_root / "collide").read_bytes() == b"already a file"


def test_cancel_mid_scan_stops_cleanly(sftp_env):
    api, _server_root, local_dir = sftp_env
    top = local_dir / "big"
    top.mkdir()
    for i in range(200):
        (top / f"empty_{i}").mkdir()

    result = api.enqueue([{"name": "big", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    cancel_result = api.cancel()
    assert cancel_result["ok"] is True

    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        if not api._scan_active():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("scan did not stop within 5s of cancel()")

    # the scan must have been cut short rather than queuing all 200 folders
    total_ever_queued = sum(api.queue.counts().values())
    assert total_ever_queued < 200
