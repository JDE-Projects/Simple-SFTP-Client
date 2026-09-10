"""
Load tests for the streaming scanner and worker pool at the scale the
BLUEPRINT claims for it: tens of thousands of files, bounded queue memory
regardless of batch size, and a scan that a cancel can actually cut short.

These never touch a real disk or network for the files being "transferred":
api._iter_local is replaced with a synthetic generator that yields
(local_path, remote_path, size, mtime, is_dir) tuples straight out of a Python range,
and api._one (the per-file byte-moving call) is stubbed to a no-op. That
isolates the thing actually being tested, the scanner/queue/worker-pool
bookkeeping (backpressure, batching, pruning), from the cost of moving real
bytes, which is what makes it possible to exercise 50,000 "files" in a few
seconds. The worker pool still opens a real SFTP session per worker against
the in-process server from conftest.py, since that path is cheap (a handful
of handshakes) and exercising it keeps this close to the real code path.

Bound numbers referenced throughout:
- SCAN_QUEUE_HIGH_WATER (3000): the scanner blocks once queue.waiting() would
  reach this, so it can transiently overshoot by up to one flush batch
  (capped at 64) plus a few items claimed as ACTIVE.
- RETAIN_FINISHED (200, in transfer_queue.py): once more than this many items
  have completed/skipped, the oldest ones collapse into counters instead of
  staying as objects in api.queue._items, which is what keeps a huge job's
  memory flat.
"""
import time

import pytest

from simple_sftp_client import SCAN_QUEUE_HIGH_WATER
from transfer_queue import RETAIN_FINISHED

FIXED_MTIME = 1_700_000_000


def _flat_path(i):
    return (f"/fake_local/f{i}.bin", f"/top/f{i}.bin")


def _deep_path_factory(depth):
    """Build a path function whose remote/local paths sit several hundred
    directory levels deep. The nested prefix is computed once (not per file)
    so timing a run of tens of thousands of files stays dominated by the
    queue machinery being tested, not by re-building a long string each time."""
    prefix = "/".join(f"d{lvl}" for lvl in range(depth))

    def _path(i):
        return (f"/fake_local/{prefix}/f{i}.bin", f"/top/{prefix}/f{i}.bin")

    return _path


def _drive_synthetic_upload(api, monkeypatch, n, path_fn):
    """Stub _one to a no-op and feed api._iter_local a synthetic generator of
    n (local_path, remote_path, size, mtime) tuples for an "upload" of a
    single top-level folder, then run the pipeline to completion.

    Returns (max_waiting, max_items): the highest values seen for
    api.queue.waiting() and len(api.queue._items) while the scan and the
    drain were both running. Sampling continues until the scan has stopped
    and nothing is left waiting or active, i.e. a true drain, not just an
    instant where the queue happens to look empty.
    """
    monkeypatch.setattr(api, "_one", lambda *a, **k: None)

    def fake_iter_local(lp, rp, is_dir, **kwargs):
        for i in range(n):
            plp, prp = path_fn(i)
            yield (plp, prp, 16, FIXED_MTIME, False)

    monkeypatch.setattr(api, "_iter_local", fake_iter_local)

    result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                          "/fake_local", "/", "overwrite")
    assert result["ok"] is True

    max_waiting = 0
    max_items = 0
    deadline = time.time() + 30
    while time.time() < deadline:
        max_waiting = max(max_waiting, api.queue.waiting())
        max_items = max(max_items, len(api.queue._items))
        if not api._scan_active() and api.queue.pending() == 0:
            break
        # A short sleep, not a tight spin: a busy-looping sampler starves the
        # scanner/worker threads of the GIL and makes the whole batch far
        # slower to drain, without meaningfully changing what peaks we see.
        time.sleep(0.005)
    else:
        pytest.fail("synthetic batch did not drain within 30s")

    return max_waiting, max_items


def test_flat_directory_scan_holds_backpressure_and_completes_all(sftp_env, wait_for_drain, monkeypatch):
    """Tens of thousands of files in one flat remote folder must never let
    the queue's WAITING count balloon past the high-water mark, and every
    single one must still reach COMPLETED."""
    api, _server_root, _local_dir = sftp_env
    n = 50_000

    max_waiting, max_items = _drive_synthetic_upload(api, monkeypatch, n, _flat_path)
    wait_for_drain(api)

    # waiting() can transiently overshoot the high-water mark by up to one
    # flush batch (capped at 64) plus a handful of items already claimed
    # ACTIVE; 200 is a generous slack for that, nowhere near n.
    assert max_waiting <= SCAN_QUEUE_HIGH_WATER + 200
    # counts() folds pruned items back in, so this is accurate even though
    # most of the 50,000 completed items no longer exist as objects.
    assert api.queue.counts()["completed"] == n
    # Bounded regardless of n: at most ~high-water items in flight plus at
    # most RETAIN_FINISHED finished ones kept as objects at any moment.
    assert max_items <= RETAIN_FINISHED + SCAN_QUEUE_HIGH_WATER + 300


def test_deep_tree_scan_holds_backpressure_and_completes_all(sftp_env, wait_for_drain, monkeypatch):
    """Same bounds as the flat case, but the remote/local paths are several
    hundred directory levels deep, showing the bound holds for a deep shape
    too, not just a wide flat one."""
    api, _server_root, _local_dir = sftp_env
    n = 50_000
    deep_path = _deep_path_factory(depth=300)  # a few hundred distinct nested levels

    max_waiting, max_items = _drive_synthetic_upload(api, monkeypatch, n, deep_path)
    wait_for_drain(api)

    assert max_waiting <= SCAN_QUEUE_HIGH_WATER + 200
    assert api.queue.counts()["completed"] == n
    assert max_items <= RETAIN_FINISHED + SCAN_QUEUE_HIGH_WATER + 300


def test_bounded_memory_does_not_grow_with_batch_size(sftp_env, wait_for_drain, monkeypatch):
    """The core BLUEPRINT claim: the live item count is bounded by
    RETAIN_FINISHED plus the high-water mark, not by how many files the job
    has. Run the same bounded pipeline at two very different sizes and check
    the peak object count barely moves between them; len(api.queue._items)
    is a robust, deterministic proxy for memory here; a wall-memory
    assertion would be flaky across machines and Python builds.

    The small run has to be bigger than SCAN_QUEUE_HIGH_WATER itself, or its
    peak would just trivially equal its own file count (nothing bounds
    below the high-water mark), and the comparison would show nothing.
    """
    api, _server_root, _local_dir = sftp_env
    n_small = SCAN_QUEUE_HIGH_WATER + 1000  # comfortably past the cap, not just "small"
    n_large = 50_000

    _, small_peak = _drive_synthetic_upload(api, monkeypatch, n_small, _flat_path)
    wait_for_drain(api)
    assert api.queue.counts()["completed"] == n_small
    # Clean slate before the second run: otherwise its peak would be
    # inflated by finished items still sitting in _items from the first run.
    api.queue.clear_finished()

    _, large_peak = _drive_synthetic_upload(api, monkeypatch, n_large, _flat_path)
    wait_for_drain(api)
    assert api.queue.counts()["completed"] == n_large

    # A few hundred items of slack, nowhere near the ~46,000-item gap there
    # would be if the item count scaled with n.
    assert large_peak <= small_peak + 300


def test_runtime_scales_about_linearly_not_worse(sftp_env, monkeypatch):
    """With _one stubbed out, drain time reflects the cost of moving items
    through the queue machinery (scan batching, worker claims, pruning), not
    the cost of any real byte transfer. A 5x-larger batch should cost at
    most a generous multiple of what perfect linear scaling would predict:
    loose enough to never flake on a slow or busy machine, tight enough to
    still catch something quadratic (e.g. an unbounded per-item scan over
    self._items, which RETAIN_FINISHED pruning exists to prevent).
    """
    api, _server_root, _local_dir = sftp_env
    n_small = 5_000
    n_large = 25_000  # 5x

    start = time.time()
    _drive_synthetic_upload(api, monkeypatch, n_small, _flat_path)
    small_elapsed = time.time() - start
    api.queue.clear_finished()

    start = time.time()
    _drive_synthetic_upload(api, monkeypatch, n_large, _flat_path)
    large_elapsed = time.time() - start

    expected_linear = small_elapsed * (n_large / n_small)
    # 3x above perfect linear, with an absolute floor so a tiny, near-zero
    # small_elapsed (fixed thread/session overhead dwarfing 5,000 no-op
    # items) can never make the bound flake tighter than it should be.
    assert large_elapsed <= max(3 * expected_linear, 0.5)


def test_cancel_all_stops_a_large_scan_before_it_finishes(sftp_env, monkeypatch):
    """A cancel mid-scan must stop the background scanner quickly, and must
    not let it queue the rest of a huge batch first. The synthetic generator
    sleeps briefly every couple thousand yields, mirroring the pace of a real
    filesystem/SFTP walk, so cancel() reliably lands while the scan is still
    running rather than after it has already finished."""
    api, _server_root, _local_dir = sftp_env
    n = 50_000
    monkeypatch.setattr(api, "_one", lambda *a, **k: None)

    def slow_flat_gen(lp, rp, is_dir, **kwargs):
        for i in range(n):
            if i and i % 2000 == 0:
                time.sleep(0.01)
            yield (f"/fake_local/f{i}.bin", f"/top/f{i}.bin", 16, FIXED_MTIME, False)

    monkeypatch.setattr(api, "_iter_local", slow_flat_gen)

    try:
        result = api.enqueue([{"name": "top", "is_dir": True}], "upload",
                              "/fake_local", "/", "overwrite")
        assert result["ok"] is True

        cancel_result = api.cancel()
        assert cancel_result["ok"] is True

        deadline = time.time() + 5
        while time.time() < deadline:
            if not api._scan_active():
                break
            time.sleep(0.01)
        else:
            pytest.fail("scan did not stop within 5s of cancel()")

        # Everything that made it onto the queue before the scan stopped,
        # in whatever state (waiting/active/cancelled/completed): well under
        # n is what proves the scan actually stopped early instead of
        # queuing the whole batch and only noticing cancel afterward.
        counts = api.queue.counts()
        total_ever_queued = sum(counts.values())
        assert total_ever_queued < n
    finally:
        # Stop the scan so it does not keep spinning after the test ends,
        # same teardown pattern as the backpressure test.
        api._stop_all_scans()
        deadline = time.time() + 15
        while time.time() < deadline:
            if not api._scan_active():
                break
            time.sleep(0.02)
