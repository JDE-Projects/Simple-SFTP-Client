"""
Thread-safe in-memory FIFO transfer queue.

Pure logic, no paramiko or network dependency. A single lock guards every
state change so multiple workers can claim items concurrently without ever
grabbing the same one; claim() is what makes the worker pool in
app/services/transfers.py (2 workers by default, up to 5 for a batch of many
small files) safe.
"""
import posixpath
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

# Most-recent completed/skipped items kept as objects in self._items; once a
# batch has more than this many finished successfully, the oldest ones
# collapse into the self._pruned counters instead of staying in memory. This
# keeps a normal small transfer fully inspectable while still bounding memory
# for a huge job.
RETAIN_FINISHED = 200


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
    is_dir: bool = False
    retryable: bool = True
    claims: tuple = ()
    report_id: int | None = None


def _item_dict(item):
    """Plain dict for one item. scan_report appears only on a scan summary row."""
    d = {
        "id": item.id,
        "direction": item.direction,
        "name": item.name,
        "state": item.state,
        "error": item.error,
        "is_dir": item.is_dir,
        "retryable": item.retryable,
    }
    if item.report_id is not None:
        d["scan_report"] = item.report_id
    return d


class TransferQueue:
    """FIFO queue of TransferItem, guarded by a single lock for atomic claims."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []  # FIFO order, append order preserved
        self._by_id = {}  # item.id -> item, kept in sync with self._items for O(1) _find
        self._next_id = 1
        self._paused = False
        # Once more than RETAIN_FINISHED items have completed/skipped, the
        # oldest ones are dropped from self._items (see _finish) so a huge
        # finished batch doesn't keep every item object in memory and get
        # copied on every UI poll. These tallies keep counts() accurate for
        # the ones that aged out and no longer exist as objects.
        self._pruned = {COMPLETED: 0, SKIPPED: 0}
        # Running count of live (not yet pruned) COMPLETED+SKIPPED items in
        # self._items, kept in sync in _finish and clear_finished so _finish
        # never has to re-scan self._items to know whether it is over
        # RETAIN_FINISHED.
        self._finished_live = 0
        self._claims = {}  # destination key -> (kind, source, holder count)

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

    def append(self, direction, local_path, remote_path, name, size=0, on_conflict="overwrite",
               is_dir=False):
        with self._lock:
            item = TransferItem(
                id=self._next_id,
                direction=direction,
                local_path=local_path,
                remote_path=remote_path,
                name=name,
                size=size,
                on_conflict=on_conflict,
                is_dir=is_dir,
            )
            self._next_id += 1
            self._items.append(item)
            self._by_id[item.id] = item
            return item.id

    @staticmethod
    def _clash_error(other_remote):
        # The key fact comes first so it stays readable in a narrow queue
        # row; the full remote paths are in the console line.
        return (f"Same name on Windows as {posixpath.basename(other_remote)}. "
                "Not downloaded: download it alone into a different folder.")

    def _conflicting_source(self, claims):
        for key, kind, source in claims:
            held = self._claims.get(key)
            if held is not None and (held[0] != kind or held[1] != source):
                return held[1]
        return None

    def _take_claims(self, claims):
        for key, kind, source in claims:
            held = self._claims.get(key)
            if held is None:
                self._claims[key] = (kind, source, 1)
            else:
                self._claims[key] = (kind, source, held[2] + 1)

    def _release_claims(self, item):
        for key, _kind, _source in item.claims:
            held = self._claims.get(key)
            if held is None:
                continue
            if held[2] == 1:
                del self._claims[key]
            else:
                self._claims[key] = (held[0], held[1], held[2] - 1)

    def admit(self, direction, local_path, remote_path, name, size, on_conflict, is_dir, claims):
        """Atomically admit a download only when all destination claims are free."""
        claims = tuple(claims)
        with self._lock:
            other_remote = self._conflicting_source(claims)
            item = TransferItem(
                id=self._next_id, direction=direction, local_path=local_path,
                remote_path=remote_path, name=name, size=size,
                state=FAILED if other_remote else WAITING,
                error=self._clash_error(other_remote) if other_remote else "",
                on_conflict=on_conflict, is_dir=is_dir,
                retryable=other_remote is None, claims=claims if other_remote is None else (),
            )
            self._next_id += 1
            self._items.append(item)
            self._by_id[item.id] = item
            if other_remote is None:
                self._take_claims(claims)
            return item.id, other_remote

    def add_clash(self, direction, local_path, remote_path, name, other_remote, is_dir):
        """Record a scan-discovered Windows destination clash without a claim."""
        with self._lock:
            item = TransferItem(
                id=self._next_id, direction=direction, local_path=local_path,
                remote_path=remote_path, name=name, state=FAILED,
                error=self._clash_error(other_remote), is_dir=is_dir, retryable=False,
            )
            self._next_id += 1
            self._items.append(item)
            self._by_id[item.id] = item
            return item.id

    def add_scan_incomplete(self, direction, name, error, report_id):
        """Record an unretryable summary row for an incomplete folder scan."""
        with self._lock:
            item = TransferItem(
                id=self._next_id, direction=direction, local_path="", remote_path="",
                name=name, state=FAILED, error=error, retryable=False, report_id=report_id,
            )
            self._next_id += 1
            self._items.append(item)
            self._by_id[item.id] = item
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
        """Move an ACTIVE item to a terminal state. No-op (returns False) otherwise.
        COMPLETED and SKIPPED items stay visible in self._items up to
        RETAIN_FINISHED of them; beyond that cap the oldest ones collapse
        into the self._pruned counter instead of staying in memory forever,
        which is what keeps a million-file job bounded. FAILED and CANCELLED
        are never pruned, so they remain visible and retryable."""
        with self._lock:
            item = self._find(item_id)
            if item is None or item.state != ACTIVE:
                return False
            item.state = new_state
            if error:
                item.error = error
            self._release_claims(item)
            if new_state in (COMPLETED, SKIPPED):
                # Only one item can cross into COMPLETED/SKIPPED per call, so
                # the count can be at most one over the cap: drop the single
                # oldest finished item (FIFO, scan from the front) if so.
                self._finished_live += 1
                if self._finished_live > RETAIN_FINISHED:
                    for i in self._items:
                        if i.state in (COMPLETED, SKIPPED):
                            self._pruned[i.state] += 1
                            self._items.remove(i)
                            del self._by_id[i.id]
                            self._finished_live -= 1
                            break
            return True

    def mark_completed(self, item_id):
        return self._finish(item_id, COMPLETED)

    def mark_failed(self, item_id, error, retryable=True):
        finished = self._finish(item_id, FAILED, error=error)
        if finished and not retryable:
            with self._lock:
                self._find(item_id).retryable = False
        return finished

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
                self._release_claims(item)
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
            if item is None or not item.retryable or item.state not in (FAILED, CANCELLED):
                return False
            other_remote = self._conflicting_source(item.claims)
            if other_remote is not None:
                item.error = self._clash_error(other_remote)
                return False
            item.state = WAITING
            item.error = ""
            item.cancel_requested = False
            self._take_claims(item.claims)
            return True

    def retry_all_failed(self):
        """Put every FAILED item back to WAITING, clearing its error and
        cancel flag, same as requeue() does for a single item. CANCELLED
        items are left alone: those were intentional and keep the existing
        per-item requeue(). Returns how many items were requeued."""
        with self._lock:
            requeued = 0
            for item in self._items:
                if item.state == FAILED and item.retryable:
                    other_remote = self._conflicting_source(item.claims)
                    if other_remote is not None:
                        item.error = self._clash_error(other_remote)
                        continue
                    item.state = WAITING
                    item.error = ""
                    item.cancel_requested = False
                    self._take_claims(item.claims)
                    requeued += 1
            return requeued

    def cancel_all(self):
        with self._lock:
            for item in self._items:
                if item.state == WAITING:
                    item.state = CANCELLED
                    self._release_claims(item)
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
                    self._release_claims(item)
                    failed += 1
            return failed

    def snapshot(self):
        """Plain dicts (id, direction, name, state, error, is_dir, retryable, and
        scan_report on a scan summary row) in FIFO order."""
        with self._lock:
            return [
                _item_dict(item)
                for item in self._items
            ]

    def retryable_failed(self):
        """How many FAILED items Retry can still put back in line. Kept out
        of counts(), whose values are one total per state."""
        with self._lock:
            return sum(1 for item in self._items if item.state == FAILED and item.retryable)

    def counts(self):
        with self._lock:
            result = {state: 0 for state in ALL_STATES}
            for item in self._items:
                result[item.state] += 1
            # Live COMPLETED/SKIPPED items are already counted from
            # self._items above. self._pruned only covers the ones that
            # aged out past RETAIN_FINISHED and no longer exist as objects,
            # so adding it in here cannot double count.
            result[COMPLETED] += self._pruned[COMPLETED]
            result[SKIPPED] += self._pruned[SKIPPED]
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
                _item_dict(item)
                for item in self._items
            ]
            pending = sum(1 for item in self._items if item.state in (WAITING, ACTIVE))
            return items, pending

    def clear_finished(self):
        """Remove items in a terminal state, keep waiting/active items. Also
        resets the pruned tallies to zero so the next batch's summary starts
        clean. Returns the number of item objects removed (the tally reset
        is not counted)."""
        with self._lock:
            before = len(self._items)
            kept = []
            for item in self._items:
                if item.state in TERMINAL_STATES:
                    del self._by_id[item.id]
                else:
                    kept.append(item)
            self._items = kept
            self._pruned = {COMPLETED: 0, SKIPPED: 0}
            self._finished_live = 0
            return before - len(self._items)

    def _find(self, item_id):
        """Caller must hold self._lock."""
        return self._by_id.get(item_id)
