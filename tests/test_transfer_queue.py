"""
Tests for transfer_queue.py: the thread-safe in-memory FIFO transfer queue.

Pure logic, no network or paramiko involved, so these run fast and offline.
"""
import threading

import pytest

from transfer_queue import (
    TransferQueue,
    WAITING,
    ACTIVE,
    COMPLETED,
    FAILED,
    CANCELLED,
)


@pytest.fixture
def q():
    return TransferQueue()


def test_claim_returns_items_in_append_order(q):
    ids = [q.append("upload", f"/local/{i}", f"/remote/{i}", f"file{i}") for i in range(5)]

    claimed_ids = []
    for _ in range(5):
        item = q.claim()
        claimed_ids.append(item.id)

    assert claimed_ids == ids


def test_append_after_claims_still_enqueues_at_tail(q):
    id1 = q.append("upload", "/l1", "/r1", "file1")
    id2 = q.append("upload", "/l2", "/r2", "file2")

    first = q.claim()
    assert first.id == id1
    q.mark_completed(first.id)

    id3 = q.append("upload", "/l3", "/r3", "file3")

    second = q.claim()
    third = q.claim()
    assert second.id == id2
    assert third.id == id3
    assert q.claim() is None


def test_mark_completed_shows_in_snapshot_and_counts(q):
    item_id = q.append("upload", "/l", "/r", "file")
    claimed = q.claim()
    assert claimed.id == item_id

    assert q.mark_completed(item_id) is True

    snap = q.snapshot()
    assert snap == [{"id": item_id, "direction": "upload", "name": "file",
                      "state": COMPLETED, "error": ""}]
    assert q.counts()[COMPLETED] == 1
    assert q.pending() == 0


def test_mark_failed_stores_error(q):
    item_id = q.append("download", "/l", "/r", "file")
    q.claim()

    assert q.mark_failed(item_id, "connection reset") is True

    snap = q.snapshot()
    assert snap[0]["state"] == FAILED
    assert snap[0]["error"] == "connection reset"
    assert q.counts()[FAILED] == 1


def test_cancel_waiting_item_goes_straight_to_cancelled(q):
    item_id = q.append("upload", "/l", "/r", "file")

    assert q.cancel(item_id) is True

    snap = q.snapshot()
    assert snap[0]["state"] == CANCELLED
    # never handed out by claim()
    assert q.claim() is None


def test_cancel_active_item_flags_it_and_worker_can_still_finish(q):
    item_id = q.append("upload", "/l", "/r", "file")
    item = q.claim()
    assert item.id == item_id

    assert q.cancel(item_id) is True
    assert item.cancel_requested is True

    # claim() must not hand it out again
    assert q.claim() is None
    # item stays ACTIVE until the worker observes the flag and finishes it
    assert q.snapshot()[0]["state"] == ACTIVE

    assert q.mark_completed(item_id) is True
    assert q.snapshot()[0]["state"] == COMPLETED


def test_invalid_transitions_return_false_and_do_not_change_state(q):
    # unknown id
    assert q.mark_completed(999) is False
    assert q.mark_failed(999, "err") is False
    assert q.mark_skipped(999) is False
    assert q.cancel(999) is False

    # WAITING item: terminal-marking methods require ACTIVE
    item_id = q.append("upload", "/l", "/r", "file")
    assert q.mark_completed(item_id) is False
    assert q.snapshot()[0]["state"] == WAITING

    # terminal item: cancel and terminal methods are no-ops
    q.claim()
    q.mark_completed(item_id)
    assert q.cancel(item_id) is False
    assert q.mark_failed(item_id, "err") is False
    snap = q.snapshot()[0]
    assert snap["state"] == COMPLETED
    assert snap["error"] == ""


def test_mark_cancelled_moves_active_item_to_cancelled(q):
    item_id = q.append("upload", "/l", "/r", "file")
    item = q.claim()
    assert item.id == item_id

    assert q.mark_cancelled(item_id) is True

    snap = q.snapshot()
    assert snap[0]["state"] == CANCELLED
    assert q.counts()[CANCELLED] == 1
    assert q.counts()[ACTIVE] == 0


def test_mark_cancelled_false_on_waiting_terminal_or_unknown(q):
    # unknown id
    assert q.mark_cancelled(999) is False

    # waiting item: not ACTIVE yet
    waiting_id = q.append("upload", "/l", "/r", "file")
    assert q.mark_cancelled(waiting_id) is False
    assert q.snapshot()[0]["state"] == WAITING

    # terminal item: already completed
    done_id = q.append("upload", "/l2", "/r2", "file2")
    first = q.claim()
    q.mark_completed(first.id)
    second = q.claim()
    q.mark_completed(second.id)
    assert q.mark_cancelled(done_id) is False
    snap = {s["id"]: s["state"] for s in q.snapshot()}
    assert snap[done_id] == COMPLETED


def test_append_with_on_conflict_stores_it_on_claimed_item(q):
    item_id = q.append("upload", "/l", "/r", "file", on_conflict="skip")

    item = q.claim()

    assert item.id == item_id
    assert item.on_conflict == "skip"


def test_claim_is_thread_safe_no_duplicates_no_drops(q):
    n = 50
    ids = [q.append("upload", f"/l{i}", f"/r{i}", f"file{i}") for i in range(n)]

    claimed = []
    claimed_lock = threading.Lock()

    def worker():
        while True:
            item = q.claim()
            if item is None:
                return
            with claimed_lock:
                claimed.append(item.id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == n
    assert q.counts()[ACTIVE] == n
