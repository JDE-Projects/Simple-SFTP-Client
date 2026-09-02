"""
Tests for the streaming background scanner (Stage 2a): enqueue()/upload_paths()
start a daemon thread that streams files into the queue as they are found,
instead of walking the whole tree up front. Runs against the same in-process
paramiko SFTP server and sftp_env/wait_for_drain fixtures as
test_queue_integration.py.
"""
import os
import time

import simple_sftp_client
from transfer_queue import COMPLETED


def test_nested_remote_folder_downloads_every_file_with_correct_bytes(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    # a few levels of nesting, with files at every level, not just the leaves
    (server_root / "top").mkdir()
    (server_root / "top" / "a.bin").write_bytes(os.urandom(1000))
    (server_root / "top" / "mid").mkdir()
    (server_root / "top" / "mid" / "b.bin").write_bytes(os.urandom(2000))
    (server_root / "top" / "mid" / "deep").mkdir()
    (server_root / "top" / "mid" / "deep" / "c.bin").write_bytes(os.urandom(3000))

    result = api.enqueue([{"name": "top", "is_dir": True}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["scanning"] is True

    wait_for_drain(api)

    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    assert states["a.bin"] == COMPLETED
    assert states["b.bin"] == COMPLETED
    assert states["c.bin"] == COMPLETED
    assert (local_dir / "top" / "a.bin").read_bytes() == \
        (server_root / "top" / "a.bin").read_bytes()
    assert (local_dir / "top" / "mid" / "b.bin").read_bytes() == \
        (server_root / "top" / "mid" / "b.bin").read_bytes()
    assert (local_dir / "top" / "mid" / "deep" / "c.bin").read_bytes() == \
        (server_root / "top" / "mid" / "deep" / "c.bin").read_bytes()
    # every file actually arrived, not just empty directory shells
    assert (local_dir / "top" / "mid" / "deep" / "c.bin").stat().st_size == 3000


def test_nested_local_folder_uploads_creating_remote_dirs_lazily(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "top").mkdir()
    (local_dir / "top" / "a.bin").write_bytes(os.urandom(500))
    (local_dir / "top" / "mid").mkdir()
    (local_dir / "top" / "mid" / "b.bin").write_bytes(os.urandom(700))
    (local_dir / "top" / "mid" / "deep").mkdir()
    (local_dir / "top" / "mid" / "deep" / "c.bin").write_bytes(os.urandom(900))

    # nothing exists on the remote side yet: no pre-created folder shells
    assert not (server_root / "top").exists()

    result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["scanning"] is True

    wait_for_drain(api)

    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    assert states["a.bin"] == COMPLETED
    assert states["b.bin"] == COMPLETED
    assert states["c.bin"] == COMPLETED
    assert (server_root / "top" / "a.bin").read_bytes() == \
        (local_dir / "top" / "a.bin").read_bytes()
    assert (server_root / "top" / "mid" / "b.bin").read_bytes() == \
        (local_dir / "top" / "mid" / "b.bin").read_bytes()
    assert (server_root / "top" / "mid" / "deep" / "c.bin").read_bytes() == \
        (local_dir / "top" / "mid" / "deep" / "c.bin").read_bytes()


def test_poll_queue_reports_scanning_while_running_then_false_once_drained(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "top").mkdir()
    for i in range(20):
        (local_dir / "top" / f"f{i}.bin").write_bytes(os.urandom(256))

    result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    # scanning should show True at some point before the batch drains (a tiny
    # window given 20 small files, so poll promptly and tolerate a miss by
    # also accepting the scan_found count moving)
    saw_scanning = False
    deadline = time.time() + 5
    while time.time() < deadline:
        status = api.poll_queue()
        if status["scanning"]:
            saw_scanning = True
            break
        if not api._scan_active() and api.queue.pending() == 0 and len(api.queue.snapshot()) == 20:
            break
        time.sleep(0.005)
    assert saw_scanning, "poll_queue never reported scanning=True while the scan ran"

    wait_for_drain(api)

    status = api.poll_queue()
    assert status["scanning"] is False
    assert status["counts"]["completed"] == 20


def test_backpressure_holds_queue_waiting_at_the_high_water_mark(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    # a tiny high-water so the cap is observable without creating thousands
    # of real files
    monkeypatch.setattr(simple_sftp_client, "SCAN_QUEUE_HIGH_WATER", 5)
    # nothing drains the queue at all, so any growth past the high-water can
    # only be explained by backpressure not holding
    monkeypatch.setattr(api, "_ensure_worker", lambda: None)

    (local_dir / "top").mkdir()
    names = [f"f{i}.bin" for i in range(40)]
    for name in names:
        (local_dir / "top" / name).write_bytes(os.urandom(16))

    try:
        result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                              str(local_dir), "/", "overwrite")
        assert result["ok"] is True

        # watch waiting() for a bit while the scan tries to run ahead; with
        # nothing draining, it must stop right at the high-water mark instead
        # of queuing all 40 files
        max_seen = 0
        deadline = time.time() + 2
        while time.time() < deadline:
            max_seen = max(max_seen, api.queue.waiting())
            time.sleep(0.01)

        assert max_seen <= 5, f"queue.waiting() reached {max_seen}, expected it held at the high-water mark"
        assert max_seen > 0, "expected at least the first batch to have been queued"
    finally:
        # stop the scan so it does not keep spinning after the test ends
        api._stop_all_scans()
        deadline = time.time() + 15
        while time.time() < deadline:
            if not api._scan_active():
                break
            time.sleep(0.02)
