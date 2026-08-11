"""
Tests for Phase 4: auto-tuned worker concurrency. worker_target() picks the
pool size for a batch once, up front, from its file sizes; _target_workers on
the Api resets back to the default once the pool empties.
"""
import os
import time

from simple_sftp_client import WORKER_COUNT, WORKER_COUNT_MAX, worker_target
from transfer_queue import ACTIVE, COMPLETED, WAITING


def test_few_small_files_stays_at_default():
    assert worker_target([100, 100, 100]) == WORKER_COUNT


def test_exactly_eight_small_files_scales_up():
    assert worker_target([100] * 8) == WORKER_COUNT_MAX


def test_seven_small_files_stays_at_default():
    assert worker_target([100] * 7) == WORKER_COUNT


def test_twenty_large_files_stays_at_default():
    assert worker_target([5 * 1024 * 1024] * 20) == WORKER_COUNT


def test_mixed_batch_with_enough_small_files_scales_up():
    sizes = [500 * 1024 * 1024] + [100] * 8
    assert worker_target(sizes) == WORKER_COUNT_MAX


def test_unknown_sizes_are_never_counted_as_small():
    assert worker_target([-1] * 10) == WORKER_COUNT


def test_unknown_sizes_mixed_with_enough_real_small_files_scales_up():
    sizes = [-1] * 8 + [100] * 8
    assert worker_target(sizes) == WORKER_COUNT_MAX


def test_target_workers_resets_to_default_once_the_pool_empties(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    names = ["a.bin", "b.bin", "c.bin"]
    for name in names:
        (local_dir / name).write_bytes(os.urandom(1024))

    result = api.enqueue([{"name": n, "is_dir": False} for n in names], "upload",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True

    wait_for_drain(api)

    assert api._target_workers == WORKER_COUNT


def test_paused_scaled_batch_keeps_its_target_then_resets_on_drain(
        sftp_env, wait_for_drain, state_of):
    # A batch of enough small files scales the pool up to WORKER_COUNT_MAX. Two
    # big files ride along to keep workers busy so a pause can land while smalls
    # are still WAITING. Through the pause-hold the raised target must survive, so
    # a resume drains the rest at the scaled count, not the default; a full drain
    # then resets it.
    api, server_root, local_dir = sftp_env
    bigs = ["big1.bin", "big2.bin"]
    for name in bigs:
        (local_dir / name).write_bytes(os.urandom(4 * 1024 * 1024))
    smalls = [f"small{i}.bin" for i in range(8)]
    for name in smalls:
        (local_dir / name).write_bytes(os.urandom(16))

    # bigs first so the FIFO hands them to the first two workers
    jobs = [{"name": n, "is_dir": False} for n in bigs + smalls]
    assert api.enqueue(jobs, "upload", str(local_dir), "/", "overwrite")["ok"] is True
    # eight small files in the batch -> the pool scales to the max
    assert api._target_workers == WORKER_COUNT_MAX

    snap = api.queue.snapshot()
    big_ids = [next(e["id"] for e in snap if e["name"] == n) for n in bigs]

    # wait until both bigs are active, then pause while smalls sit behind them
    deadline = time.time() + 15
    while time.time() < deadline:
        if all(state_of(api, i)["state"] == ACTIVE for i in big_ids):
            break
        time.sleep(0.02)
    assert api.pause_queue() == {"ok": True}

    # bigs finish, the pool winds down; some smalls are still WAITING because a
    # paused claim() never hands one out
    deadline = time.time() + 15
    while time.time() < deadline:
        bigs_done = all(state_of(api, i)["state"] == COMPLETED for i in big_ids)
        if bigs_done and not any(w.is_alive() for w in api._workers):
            break
        time.sleep(0.02)

    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    assert any(states[n] == WAITING for n in smalls)
    # the pause-hold left the target alone: a resume will use the scaled count
    assert api._target_workers == WORKER_COUNT_MAX

    assert api.resume_queue() == {"ok": True}
    wait_for_drain(api)
    states = {e["name"]: e["state"] for e in api.queue.snapshot()}
    for name in smalls:
        assert states[name] == COMPLETED
    # a genuine drain resets the target back to the default
    assert api._target_workers == WORKER_COUNT
