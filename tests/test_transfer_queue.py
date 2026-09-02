"""
Tests for transfer_queue.py: the thread-safe in-memory FIFO transfer queue.

Pure logic, no network or paramiko involved, so these run fast and offline.
"""
import threading

import pytest

import transfer_queue
from transfer_queue import (
    TransferQueue,
    WAITING,
    ACTIVE,
    COMPLETED,
    FAILED,
    CANCELLED,
    SKIPPED,
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


def test_mark_skipped_shows_in_snapshot_and_counts(q):
    item_id = q.append("upload", "/l", "/r", "file")
    q.claim()

    assert q.mark_skipped(item_id) is True

    assert q.snapshot()[0]["state"] == SKIPPED
    assert q.counts()[SKIPPED] == 1


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


def test_clear_finished_removes_terminal_items_only(q):
    active_id = q.append("upload", "/l2", "/r2", "file2")
    done_id = q.append("upload", "/l3", "/r3", "file3")
    failed_id = q.append("upload", "/l4", "/r4", "file4")

    active_item = q.claim()
    assert active_item.id == active_id
    done_item = q.claim()
    assert done_item.id == done_id
    q.mark_completed(done_id)
    failed_item = q.claim()
    assert failed_item.id == failed_id
    q.mark_failed(failed_id, "boom")

    # appended after the others were claimed, so it stays WAITING
    waiting_id = q.append("upload", "/l1", "/r1", "file1")

    removed = q.clear_finished()

    assert removed == 2
    remaining_ids = {s["id"] for s in q.snapshot()}
    assert remaining_ids == {waiting_id, active_id}
    # the completed tally is reset to zero by clear_finished() for a clean
    # summary on the next batch.
    assert q.counts()[COMPLETED] == 0


def test_clear_finished_returns_zero_when_nothing_to_remove(q):
    q.append("upload", "/l1", "/r1", "file1")
    q.claim()

    assert q.clear_finished() == 0
    assert len(q.snapshot()) == 1


def test_fail_waiting_fails_only_waiting_items(q):
    # Build a mix of states. claim() is FIFO, so claim the first two and
    # finalize them (one ACTIVE, one COMPLETED), then append fresh WAITING
    # items at the tail that were never claimed.
    active_id = q.append("upload", "/l1", "/r1", "file1")
    done_id = q.append("upload", "/l2", "/r2", "file2")

    assert q.claim().id == active_id          # file1 -> ACTIVE, left as-is
    assert q.claim().id == done_id
    q.mark_completed(done_id)                  # file2 -> COMPLETED

    wait1 = q.append("upload", "/l3", "/r3", "file3")   # WAITING
    wait2 = q.append("upload", "/l4", "/r4", "file4")   # WAITING

    failed = q.fail_waiting("no session")

    assert failed == 2
    states = {s["id"]: s for s in q.snapshot()}
    assert states[wait1]["state"] == FAILED and states[wait1]["error"] == "no session"
    assert states[wait2]["state"] == FAILED and states[wait2]["error"] == "no session"
    assert states[active_id]["state"] == ACTIVE       # active untouched
    assert states[done_id]["state"] == COMPLETED      # terminal untouched


def test_fail_waiting_returns_zero_when_nothing_waiting(q):
    q.append("upload", "/l", "/r", "file")
    q.claim()  # now ACTIVE, nothing WAITING

    assert q.fail_waiting("no session") == 0
    assert q.snapshot()[0]["state"] == ACTIVE


def test_requeue_failed_item_resets_to_waiting_and_clears_error(q):
    item_id = q.append("upload", "/l", "/r", "file")
    q.claim()
    q.mark_failed(item_id, "connection reset")

    assert q.requeue(item_id) is True

    snap = q.snapshot()[0]
    assert snap["state"] == WAITING
    assert snap["error"] == ""


def test_requeue_cancelled_item_resets_to_waiting(q):
    item_id = q.append("upload", "/l", "/r", "file")
    q.cancel(item_id)  # waiting item cancels straight to CANCELLED

    assert q.requeue(item_id) is True

    snap = q.snapshot()[0]
    assert snap["state"] == WAITING
    assert snap["error"] == ""


def test_requeue_does_nothing_for_waiting_active_completed_or_unknown(q):
    active_id = q.append("upload", "/l2", "/r2", "file2")
    item = q.claim()
    assert item.id == active_id
    assert q.requeue(active_id) is False
    states = {s["id"]: s["state"] for s in q.snapshot()}
    assert states[active_id] == ACTIVE

    completed_id = q.append("upload", "/l3", "/r3", "file3")
    completed_item = q.claim()
    assert completed_item.id == completed_id
    q.mark_completed(completed_id)
    assert q.requeue(completed_id) is False
    states = {s["id"]: s["state"] for s in q.snapshot()}
    assert states[completed_id] == COMPLETED

    waiting_id = q.append("upload", "/l1", "/r1", "file1")
    assert q.requeue(waiting_id) is False
    assert {s["id"]: s["state"] for s in q.snapshot()}[waiting_id] == WAITING

    assert q.requeue(999) is False


def test_pause_stops_claim_but_leaves_waiting_items_waiting(q):
    ids = [q.append("upload", f"/l{i}", f"/r{i}", f"file{i}") for i in range(3)]

    q.pause()

    assert q.claim() is None
    states = {s["id"]: s["state"] for s in q.snapshot()}
    for item_id in ids:
        assert states[item_id] == WAITING


def test_resume_lets_claim_hand_out_waiting_items_in_fifo_order(q):
    ids = [q.append("upload", f"/l{i}", f"/r{i}", f"file{i}") for i in range(3)]

    q.pause()
    assert q.claim() is None
    q.resume()

    claimed_ids = [q.claim().id for _ in range(3)]
    assert claimed_ids == ids
    assert q.claim() is None


def test_is_paused_reflects_pause_and_resume(q):
    assert q.is_paused() is False

    q.pause()
    assert q.is_paused() is True

    q.resume()
    assert q.is_paused() is False


def test_pause_does_not_disturb_an_already_active_item(q):
    item_id = q.append("upload", "/l", "/r", "file")
    item = q.claim()
    assert item.id == item_id

    q.pause()

    assert q.snapshot()[0]["state"] == ACTIVE
    assert q.mark_completed(item_id) is True
    assert q.snapshot()[0]["state"] == COMPLETED


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


def test_failed_and_cancelled_items_stay_visible_in_snapshot(q):
    failed_id = q.append("upload", "/l1", "/r1", "file1")
    cancelled_id = q.append("upload", "/l2", "/r2", "file2")

    q.claim()
    q.mark_failed(failed_id, "boom")
    q.claim()
    q.mark_cancelled(cancelled_id)

    states = {s["id"]: s["state"] for s in q.snapshot()}
    assert states[failed_id] == FAILED
    assert states[cancelled_id] == CANCELLED


def test_retry_all_failed_requeues_only_failed_items(q):
    failed_id1 = q.append("upload", "/l1", "/r1", "file1")
    failed_id2 = q.append("upload", "/l2", "/r2", "file2")
    cancelled_id = q.append("upload", "/l3", "/r3", "file3")
    completed_id = q.append("upload", "/l4", "/r4", "file4")

    q.claim()
    q.mark_failed(failed_id1, "boom")
    q.claim()
    q.mark_failed(failed_id2, "bang")
    q.cancel(cancelled_id)  # WAITING -> CANCELLED directly
    q.claim()
    q.mark_completed(completed_id)

    waiting_id = q.append("upload", "/l5", "/r5", "file5")

    requeued = q.retry_all_failed()

    assert requeued == 2
    states = {s["id"]: s for s in q.snapshot()}
    assert states[failed_id1]["state"] == WAITING
    assert states[failed_id1]["error"] == ""
    assert states[failed_id2]["state"] == WAITING
    assert states[failed_id2]["error"] == ""
    # cancelled item is left alone, not swept up by the bulk retry
    assert states[cancelled_id]["state"] == CANCELLED
    # untouched waiting item stays waiting
    assert states[waiting_id]["state"] == WAITING
    # completed item is untouched either way
    assert states[completed_id]["state"] == COMPLETED
    assert q.counts()[COMPLETED] == 1


def test_completed_items_beyond_the_cap_age_out_of_snapshot(q, monkeypatch):
    # Patch a small cap so the test runs fast instead of creating 200+ real
    # transfers. transfer_queue._finish() reads the module-level constant by
    # name at call time, so patching the module attribute is enough.
    monkeypatch.setattr(transfer_queue, "RETAIN_FINISHED", 3)

    ids = [q.append("upload", f"/l{i}", f"/r{i}", f"file{i}") for i in range(5)]
    for item_id in ids:
        q.claim()
        q.mark_completed(item_id)

    # only the newest RETAIN_FINISHED (3) completed items are still objects
    snap_ids = [s["id"] for s in q.snapshot()]
    assert snap_ids == ids[-3:]
    # the oldest two aged out of snapshot() entirely
    assert ids[0] not in snap_ids
    assert ids[1] not in snap_ids
    # counts() still reports the full completed total, live plus pruned
    assert q.counts()[COMPLETED] == 5


def test_retry_all_failed_returns_zero_when_nothing_failed(q):
    q.append("upload", "/l1", "/r1", "file1")

    assert q.retry_all_failed() == 0


def test_clear_finished_zeroes_pruned_tallies_for_a_clean_summary(q):
    id1 = q.append("upload", "/l1", "/r1", "file1")
    id2 = q.append("upload", "/l2", "/r2", "file2")
    failed_id = q.append("upload", "/l3", "/r3", "file3")

    q.claim()
    q.mark_completed(id1)
    q.claim()
    q.mark_completed(id2)
    q.claim()
    q.mark_failed(failed_id, "boom")

    assert q.counts()[COMPLETED] == 2

    removed = q.clear_finished()

    assert removed == 3  # both completed objects plus the failed one
    assert q.counts()[COMPLETED] == 0
    assert q.snapshot() == []


def test_counts_totals_stay_correct_across_a_mix_of_outcomes(q):
    complete_id = q.append("upload", "/l1", "/r1", "file1")
    skip_id = q.append("upload", "/l2", "/r2", "file2")
    fail_id = q.append("upload", "/l3", "/r3", "file3")
    cancel_id = q.append("upload", "/l4", "/r4", "file4")
    waiting_id = q.append("upload", "/l5", "/r5", "file5")

    q.claim()
    q.mark_completed(complete_id)
    q.claim()
    q.mark_skipped(skip_id)
    q.claim()
    q.mark_failed(fail_id, "boom")
    q.claim()
    q.mark_cancelled(cancel_id)

    counts = q.counts()
    assert counts[COMPLETED] == 1
    assert counts[SKIPPED] == 1
    assert counts[FAILED] == 1
    assert counts[CANCELLED] == 1
    assert counts[WAITING] == 1
    assert counts[ACTIVE] == 0
    assert sum(counts.values()) == 5
    assert waiting_id in {s["id"] for s in q.snapshot()}
