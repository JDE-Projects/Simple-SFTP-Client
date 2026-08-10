"""
Tests for Phase 4: bounded parallel transfers (WORKER_COUNT workers draining
the queue concurrently, each over its own SFTP session).

Built on the same in-process paramiko SFTP server and sftp_env/wait_for_drain/
state_of fixtures as test_queue_integration.py.
"""
import os
import threading
import time

from transfer_queue import CANCELLED, COMPLETED, WAITING


def test_two_files_both_complete_with_matching_bytes(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    up_data = os.urandom(48 * 1024 + 7)
    (local_dir / "up.bin").write_bytes(up_data)
    down_data = os.urandom(52 * 1024 + 3)
    (server_root / "down.bin").write_bytes(down_data)

    up_result = api.enqueue([{"name": "up.bin", "is_dir": False}], "upload",
                             str(local_dir), "/", "overwrite")
    down_result = api.enqueue([{"name": "down.bin", "is_dir": False}], "download",
                               str(local_dir), "/", "overwrite")
    assert up_result["ok"] is True and down_result["ok"] is True

    wait_for_drain(api)

    snap = {e["name"]: e for e in api.queue.snapshot()}
    assert snap["up.bin"]["state"] == COMPLETED
    assert snap["down.bin"]["state"] == COMPLETED
    assert (server_root / "up.bin").read_bytes() == up_data
    assert (local_dir / "down.bin").read_bytes() == down_data


def test_five_files_never_more_than_two_active_at_once(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    names = [f"f{i}.bin" for i in range(5)]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(256 * 1024))

    # Wrap _one to track how many are running concurrently. A short sleep
    # widens the window so two overlapping calls are reliably observed.
    lock = threading.Lock()
    tracker = {"current": 0, "max": 0}
    real_one = api._one

    def wrapped_one(*args, **kwargs):
        with lock:
            tracker["current"] += 1
            tracker["max"] = max(tracker["max"], tracker["current"])
        try:
            time.sleep(0.05)
            return real_one(*args, **kwargs)
        finally:
            with lock:
                tracker["current"] -= 1

    api._one = wrapped_one

    jobs = [{"name": n, "is_dir": False} for n in names]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    wait_for_drain(api)

    assert tracker["max"] == 2  # exactly WORKER_COUNT, not less, never more
    snap = api.queue.snapshot()
    assert len(snap) == 5
    assert all(e["state"] == COMPLETED for e in snap)


def test_each_worker_uses_its_own_session_never_the_shared_one(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    names = ["s1.bin", "s2.bin"]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(1024 * 1024))

    sessions = []
    real_open_sftp = api.client.open_sftp

    def tracking_open_sftp(*args, **kwargs):
        s = real_open_sftp(*args, **kwargs)
        sessions.append(s)
        return s

    api.client.open_sftp = tracking_open_sftp

    result = api.enqueue([{"name": n, "is_dir": False} for n in names], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    wait_for_drain(api)

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    for s in sessions:
        assert s is not api.sftp


def test_cancel_one_active_item_leaves_the_other_to_complete(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    # large enough that the byte loop is still running when cancel_item lands
    victim = local_dir / "victim.bin"
    victim.write_bytes(os.urandom(6 * 1024 * 1024))
    survivor = local_dir / "survivor.bin"
    survivor.write_bytes(os.urandom(6 * 1024 * 1024))

    jobs = [{"name": "victim.bin", "is_dir": False}, {"name": "survivor.bin", "is_dir": False}]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    snap = api.queue.snapshot()
    victim_id = next(e["id"] for e in snap if e["name"] == "victim.bin")
    survivor_id = next(e["id"] for e in snap if e["name"] == "survivor.bin")

    # wait until both are active (both workers picked one up) before cancelling
    deadline = time.time() + 15
    while time.time() < deadline:
        if state_of(api, victim_id)["state"] != WAITING and state_of(api, survivor_id)["state"] != WAITING:
            break
        time.sleep(0.02)

    assert api.cancel_item(victim_id) == {"ok": True}

    wait_for_drain(api)

    assert state_of(api, victim_id)["state"] == CANCELLED
    assert state_of(api, survivor_id)["state"] == COMPLETED


def test_cancel_all_stops_both_active_items(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    names = ["a.bin", "b.bin"]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(6 * 1024 * 1024))

    jobs = [{"name": n, "is_dir": False} for n in names]
    result = api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    snap = api.queue.snapshot()
    ids = {e["name"]: e["id"] for e in snap}

    deadline = time.time() + 15
    while time.time() < deadline:
        if all(state_of(api, i)["state"] != WAITING for i in ids.values()):
            break
        time.sleep(0.02)

    assert api.cancel() == {"ok": True}

    wait_for_drain(api)

    for _name, item_id in ids.items():
        assert state_of(api, item_id)["state"] == CANCELLED


def test_mixed_upload_and_download_run_concurrently(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    up_data = os.urandom(2 * 1024 * 1024)
    (local_dir / "mix_up.bin").write_bytes(up_data)
    down_data = os.urandom(2 * 1024 * 1024)
    (server_root / "mix_down.bin").write_bytes(down_data)

    up_result = api.enqueue([{"name": "mix_up.bin", "is_dir": False}], "upload",
                             str(local_dir), "/", "overwrite")
    down_result = api.enqueue([{"name": "mix_down.bin", "is_dir": False}], "download",
                               str(local_dir), "/", "overwrite")
    assert up_result["ok"] is True and down_result["ok"] is True

    wait_for_drain(api)

    snap = {e["name"]: e for e in api.queue.snapshot()}
    assert snap["mix_up.bin"]["state"] == COMPLETED
    assert snap["mix_down.bin"]["state"] == COMPLETED
    assert (server_root / "mix_up.bin").read_bytes() == up_data
    assert (local_dir / "mix_down.bin").read_bytes() == down_data


def test_fully_sent_transfer_completes_even_if_cancel_arrives_right_at_the_end(sftp_env):
    api, server_root, local_dir = sftp_env
    data = os.urandom(4096)  # small: a single 32768-byte chunk covers it all
    (local_dir / "onelast.bin").write_bytes(data)

    # A cancel_check that only starts returning True after the transfer has
    # already been asked for its next chunk once (i.e. after the sole chunk
    # of data was read and would have been written). This reproduces a cancel
    # landing in the exact instant the file finishes.
    calls = {"n": 0}

    def cancel_check():
        calls["n"] += 1
        return calls["n"] > 1

    res = api._one("upload", str(local_dir / "onelast.bin"), "/onelast.bin", "onelast.bin",
                    0, 1, "overwrite", api.sftp, cancel_check=cancel_check)

    assert res == "ok"  # not "cancelled": every byte was already sent
    assert (server_root / "onelast.bin").read_bytes() == data


def test_snapshot_and_pending_agree_with_themselves(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    names = ["c1.bin", "c2.bin", "c3.bin"]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(2048))

    result = api.enqueue([{"name": n, "is_dir": False} for n in names], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    # Read the combined method and the two separate ones back to back; on a
    # single lock grab they must describe the same moment.
    items, pending = api.queue.snapshot_and_pending()
    assert pending == sum(1 for it in items if it["state"] in ("waiting", "active"))

    wait_for_drain(api)

    items, pending = api.queue.snapshot_and_pending()
    assert pending == 0
    assert all(it["state"] == COMPLETED for it in items)
