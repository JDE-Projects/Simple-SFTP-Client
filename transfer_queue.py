"""
Thread-safe in-memory FIFO transfer queue.

Pure logic, no paramiko or network dependency. A single lock guards every
state change so multiple workers can claim items concurrently without ever
grabbing the same one; claim() is what makes the worker pool in
simple_sftp_client.py (two workers by default) safe.
"""
import threading
from dataclasses import dataclass

# ───────────── states ─────────────
WAITING = "waiting"
ACTIVE = "active"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
SKIPPED = "skipped"

ALL_STATES = (WAITING, ACTIVE, COMPLETED, FAILED, CANCELLED, SKIPPED)
TERMINAL_STATES = (COMPLETED, FAILED, CANCELLED, SKIPPED)


@dataclass
class TransferItem:
    id: int
    direction: str  # "upload" or "download"
    local_path: str
    remote_path: str
    name: str
    size: int = 0
    state: str = WAITING
    error: str = ""
    cancel_requested: bool = False
    on_conflict: str = "overwrite"


class TransferQueue:
    """FIFO queue of TransferItem, guarded by a single lock for atomic claims."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []  # FIFO order, append order preserved
        self._next_id = 1
        self._paused = False

    def pause(self):
        """Stop claim() from handing out new items. Items already ACTIVE are
        left alone and run to completion."""
        with self._lock:
            self._paused = True

    def resume(self):
        """Let claim() hand out WAITING items again."""
        with self._lock:
            self._paused = False

    def is_paused(self):
        with self._lock:
            return self._paused

    def append(self, direction, local_path, remote_path, name, size=0, on_conflict="overwrite"):
        with self._lock:
            item = TransferItem(
                id=self._next_id,
                direction=direction,
                local_path=local_path,
                remote_path=remote_path,
                name=name,
                size=size,
                on_conflict=on_conflict,
            )
            self._next_id += 1
            self._items.append(item)
            return item.id

    def claim(self):
        """Atomically find the oldest WAITING item, mark it ACTIVE, and return it."""
        with self._lock:
            # Workers retire on a None claim, so this is what makes the pool
            # wind down while paused instead of picking up more WAITING work.
            if self._paused:
                return None
            for item in self._items:
                if item.state == WAITING:
                    item.state = ACTIVE
                    return item
            return None

    def _finish(self, item_id, new_state, error=""):
        """Move an ACTIVE item to a terminal state. No-op (returns False) otherwise."""
        with self._lock:
            item = self._find(item_id)
            if item is None or item.state != ACTIVE:
                return False
            item.state = new_state
            if error:
                item.error = error
            return True

    def mark_completed(self, item_id):
        return self._finish(item_id, COMPLETED)

    def mark_failed(self, item_id, error):
        return self._finish(item_id, FAILED, error=error)

    def mark_skipped(self, item_id):
        return self._finish(item_id, SKIPPED)

    def mark_cancelled(self, item_id):
        """Move an ACTIVE item to CANCELLED (terminal). False if not ACTIVE or unknown.
        This is how the worker finalizes an active transfer that was cancelled
        mid-flight; cancel() alone only flags an active item, it does not move it."""
        return self._finish(item_id, CANCELLED)

    def cancel(self, item_id):
        """WAITING items cancel immediately. ACTIVE items are flagged for the
        worker to observe and finish via a terminal method. Returns False if
        the item is unknown or already terminal."""
        with self._lock:
            item = self._find(item_id)
            if item is None:
                return False
            if item.state == WAITING:
                item.state = CANCELLED
                return True
            if item.state == ACTIVE:
                item.cancel_requested = True
                return True
            return False

    def requeue(self, item_id):
        """How a failed or user-cancelled item gets put back in line for the
        worker pool: reset it to WAITING with a clean error and cancel flag.
        False (no change) for any other state or an unknown id."""
        with self._lock:
            item = self._find(item_id)
            if item is None or item.state not in (FAILED, CANCELLED):
                return False
            item.state = WAITING
            item.error = ""
            item.cancel_requested = False
            return True

    def cancel_all(self):
        with self._lock:
            for item in self._items:
                if item.state == WAITING:
                    item.state = CANCELLED
                elif item.state == ACTIVE:
                    item.cancel_requested = True

    def fail_waiting(self, error):
        """Move every still-WAITING item to FAILED with the given reason, and
        return how many were failed. Used when the worker pool cannot open any
        transfer session: without this, queued items would sit as WAITING
        forever with no worker left to drain them, showing as pending with no
        visible failure. ACTIVE and terminal items are left untouched."""
        with self._lock:
            failed = 0
            for item in self._items:
                if item.state == WAITING:
                    item.state = FAILED
                    item.error = error
                    failed += 1
            return failed

    def snapshot(self):
        """Plain dicts (id, direction, name, state, error) in FIFO order, copies only."""
        with self._lock:
            return [
                {
                    "id": item.id,
                    "direction": item.direction,
                    "name": item.name,
                    "state": item.state,
                    "error": item.error,
                }
                for item in self._items
            ]

    def counts(self):
        with self._lock:
            result = {state: 0 for state in ALL_STATES}
            for item in self._items:
                result[item.state] += 1
            return result

    def pending(self):
        with self._lock:
            return sum(1 for item in self._items if item.state in (WAITING, ACTIVE))

    def waiting(self):
        """Count of items still WAITING to be claimed (not counting ACTIVE).
        Used by the worker pool to decide whether to start another worker or
        let one retire; pending() includes ACTIVE items too, so it is not the
        right check there (an active item on another worker would wrongly
        keep an idle worker's decision looking like there is more to do)."""
        with self._lock:
            return sum(1 for item in self._items if item.state == WAITING)

    def snapshot_and_pending(self):
        """Same data as snapshot() and pending(), read under one lock grab so
        the two numbers can never disagree by one item mid-transfer, the way
        two separate lock grabs could."""
        with self._lock:
            items = [
                {
                    "id": item.id,
                    "direction": item.direction,
                    "name": item.name,
                    "state": item.state,
                    "error": item.error,
                }
                for item in self._items
            ]
            pending = sum(1 for item in self._items if item.state in (WAITING, ACTIVE))
            return items, pending

    def clear_finished(self):
        """Remove items in a terminal state, keep waiting/active items. Returns
        the number of items removed."""
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.state not in TERMINAL_STATES]
            return before - len(self._items)

    def _find(self, item_id):
        """Caller must hold self._lock."""
        for item in self._items:
            if item.id == item_id:
                return item
        return None
