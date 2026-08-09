"""
Thread-safe in-memory FIFO transfer queue.

Phase 1: pure logic, no paramiko or network dependency. A single lock guards
every state change so multiple workers can claim items concurrently without
ever grabbing the same one. Concurrency is 1 today, but claim() is written
to be safe for a future worker pool.
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


class TransferQueue:
    """FIFO queue of TransferItem, guarded by a single lock for atomic claims."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []  # FIFO order, append order preserved
        self._next_id = 1

    def append(self, direction, local_path, remote_path, name, size=0):
        with self._lock:
            item = TransferItem(
                id=self._next_id,
                direction=direction,
                local_path=local_path,
                remote_path=remote_path,
                name=name,
                size=size,
            )
            self._next_id += 1
            self._items.append(item)
            return item.id

    def claim(self):
        """Atomically find the oldest WAITING item, mark it ACTIVE, and return it."""
        with self._lock:
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

    def cancel_all(self):
        with self._lock:
            for item in self._items:
                if item.state == WAITING:
                    item.state = CANCELLED
                elif item.state == ACTIVE:
                    item.cancel_requested = True

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

    def _find(self, item_id):
        """Caller must hold self._lock."""
        for item in self._items:
            if item.id == item_id:
                return item
        return None
