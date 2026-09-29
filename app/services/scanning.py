"""Service functions for the scanning area."""

import os
import stat
import time
import threading
import traceback
import posixpath
from app import constants
from app.debug import debug
from app.errors import friendly_error
from app.paths import is_temp_part, safe_local_child


def _register_scan(api):
    """Start tracking a new background scan: a fresh stop Event and found
    count, keyed by its own id. Always a brand-new Event, never a shared
    or reused one, so a stop from an earlier scan can never reach a scan
    started after it."""
    with api._scan_lock:
        scan_id = api._scan_next_id
        api._scan_next_id += 1
        api._scans[scan_id] = {"stop": threading.Event(), "found": 0}
        return scan_id, api._scans[scan_id]["stop"]


def _deregister_scan(api, scan_id):
    with api._scan_lock:
        api._scans.pop(scan_id, None)


def _stop_all_scans(api):
    """Signal every currently-running scan to stop walking. Used by
    cancel-all and disconnect so a huge scan halts promptly instead of
    continuing to queue files nobody will drain."""
    with api._scan_lock:
        for s in api._scans.values():
            s["stop"].set()


def _scan_active(api):
    with api._scan_lock:
        return bool(api._scans)


def _bump_scan_found(api, scan_id, n):
    with api._scan_lock:
        entry = api._scans.get(scan_id)
        if entry is not None:
            entry["found"] += n


def _scan_found_total(api):
    """Sum of found-so-far across every active scan, for poll_queue()."""
    with api._scan_lock:
        return sum(s["found"] for s in api._scans.values())


def _scan_wait_for_room(api, stop_event):
    """Block the scanner thread (never the bridge thread) while the queue
    has too many WAITING items, or is paused, so a huge scan cannot flood
    memory faster than the worker pool can drain it. Returns as soon as
    there is room, the scan is stopped, or the connection drops."""
    while not stop_event.is_set() and api.connected:
        if api.queue.waiting() >= constants.SCAN_QUEUE_HIGH_WATER:
            time.sleep(0.05)
            continue
        if api.queue.is_paused():
            time.sleep(0.05)
            continue
        break


def _iter_local(api, lp, rp, is_dir, problems=None, include_dirs=False):
    """Stream (local_path, remote_path, size, mtime, is_dir) one file (or
    folder marker) at a time from a local file or folder, without
    creating any directories. A single file yields one tuple with
    is_dir False and returns True (this path contributed a file). A
    folder is walked depth-first, one subfolder's listing in memory at a
    time rather than the whole tree, and returns True if any file was
    found anywhere in its subtree, False otherwise.

    include_dirs, when True, also yields a folder marker tuple
    (path, path, 0, 0, True) for any directory whose entire subtree
    contains no files: a folder that does contain files anywhere below
    it already gets created as a side effect of sending those files, so
    it is never marked here. The default False keeps every existing
    caller (compare/sync) seeing only file tuples, unchanged. A marker is
    only ever yielded after that directory's own listing succeeds: a
    directory that could not be listed is not known to be empty.

    problems, if given, is a list that a listing failure or an unreadable
    file's metadata appends a short descriptor to (in addition to the usual
    log), so a caller that needs to know the walk was incomplete
    (compare/sync) can tell; the default of None keeps the ordinary
    transfer scan's current behavior (log and continue) unchanged."""
    if not is_dir:
        try:
            st = os.stat(lp)
            size, mtime = st.st_size, int(st.st_mtime)
        except OSError as e:
            size, mtime = 0, 0
            if problems is not None:
                problems.append(f"could not read {lp}: {e}")
        yield (lp, rp, size, mtime, False)
        return True
    had_file = False
    try:
        with os.scandir(lp) as it:
            for entry in it:
                if is_temp_part(entry.name):
                    continue
                rchild = posixpath.join(rp, entry.name)
                try:
                    child_is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    child_is_dir = False
                if child_is_dir:
                    had = yield from api._iter_local(
                        entry.path, rchild, True, problems=problems, include_dirs=include_dirs)
                    had_file = had_file or had
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        size, mtime = st.st_size, int(st.st_mtime)
                    except OSError as e:
                        size, mtime = 0, 0
                        if problems is not None:
                            problems.append(f"could not read {entry.path}: {e}")
                    yield (entry.path, rchild, size, mtime, False)
                    had_file = True
    except OSError as e:
        api._worker_log(f"could not list {lp}: {e}", "error")
        if problems is not None:
            problems.append(f"could not list {lp}: {e}")
        return False
    if include_dirs and not had_file:
        yield (lp, rp, 0, 0, True)
    return had_file


def _iter_remote(api, sftp, rp, lp, is_dir, root, problems=None, include_dirs=False):
    """Same as _iter_local but over an sftp session for a remote file or
    folder, again without creating any directories. sftp must be a
    session owned by the caller (the scanner opens its own, never
    self.sftp, which stays reserved for the file browser). root is the
    local folder the user selected for this download; it stays fixed
    across the whole recursive walk so every level, however deep, is
    checked against the same boundary rather than its immediate parent.

    Yields (local_path, remote_path, size, mtime, is_dir) tuples. A
    single file yields one file tuple and returns True (this path
    contributed a file); a folder returns True if any file was found
    anywhere in its subtree, False otherwise. include_dirs, when True,
    also yields a folder marker (lp, rp, 0, 0, True) for a directory
    whose whole subtree has no files, the same rule and default as
    _iter_local; a marker is only yielded once this directory's own
    listing has succeeded.

    problems, if given, is a list that a listing failure appends a short
    descriptor to (in addition to the usual log); see _iter_local for why
    the default is None. An unsafe remote name is skipped and logged only,
    never recorded as a problem (see the ValueError branch below).

    listdir_iter() pipelines read-ahead READDIR requests on the sftp
    session, so starting a second listdir_iter() on the same session
    (recursing into a subfolder) before the current one is fully drained
    hangs the session. So files are yielded the instant they are seen,
    but subfolders are only buffered as (rchild, lchild) name pairs here
    and recursed into after this level's loop finishes. That buffer holds
    only subfolder names, not the whole level, so the giant-flat-directory
    case (no subfolders) stays effectively unbuffered."""
    if not is_dir:
        size, mtime = api._rstat(sftp, rp)
        yield (lp, rp, size, mtime, False)
        return True
    had_file = False
    subdirs = []
    try:
        for a in sftp.listdir_iter(rp):
            if is_temp_part(a.filename):
                continue
            rchild = posixpath.join(rp, a.filename)
            try:
                lchild = safe_local_child(lp, a.filename, root)
            except ValueError as e:
                # An unsafe remote name is skipped and logged, not treated
                # as an incomplete-scan problem: it can never be represented
                # locally, so it would block compare/sync on a whole folder
                # over one untransferable name (e.g. a legitimate Linux name
                # that is illegal on Windows). This matches the transfer
                # scan's skip-and-report behavior.
                api._worker_log(f"skipped unsafe remote name {a.filename!r}: {e}", "error")
                continue
            if stat.S_ISDIR(a.st_mode):
                subdirs.append((rchild, lchild))
            else:
                yield (lchild, rchild, a.st_size, int(a.st_mtime or 0), False)
                had_file = True
    except Exception as e:
        api._worker_log(f"could not list {rp}: {friendly_error(e)}", "error")
        if problems is not None:
            problems.append(f"could not list {rp}: {friendly_error(e)}")
        return False
    for rchild, lchild in subdirs:
        had = yield from api._iter_remote(
            sftp, rchild, lchild, True, root, problems=problems, include_dirs=include_dirs)
        had_file = had_file or had
    if include_dirs and not had_file:
        yield (lp, rp, 0, 0, True)
    return had_file


def _scan_and_queue(api, roots, direction, on_conflict, scan_id, stop_event, local_root=None):
    """Runs entirely on its own daemon thread, never the pywebview bridge
    thread: walks roots (a list of (local_path, remote_path, is_dir)
    tuples) streaming file-by-file, and flushes small batches through
    _enqueue_files as it goes, so the existing worker-pool scaling logic
    is reused unchanged. Backpressure (_scan_wait_for_room) is what keeps
    the queue bounded during a huge scan; batches are also capped to
    SCAN_QUEUE_HIGH_WATER so a tiny high-water (as in a test) is actually
    observable, not just the production default. Always deregisters
    itself, and never lets an unexpected exception fail silently.

    local_root is the local folder the user selected for a download (the
    confinement boundary passed to _iter_remote); unused for uploads."""
    sftp = None
    found = 0
    batch = []
    try:
        if direction == "download":
            try:
                sftp = api.client.open_sftp()
            except Exception as e:
                api._worker_log(
                    f"scan: could not open a transfer session: {friendly_error(e)}", "error")
                return

        def flush():
            nonlocal batch, found
            if not batch:
                return
            api._scan_wait_for_room(stop_event)
            if stop_event.is_set() or not api.connected:
                return
            found += len(batch)
            api._bump_scan_found(scan_id, len(batch))
            api._enqueue_files(batch, direction, on_conflict)
            batch = []

        stopped_early = False
        for lp, rp, is_dir in roots:
            if stop_event.is_set() or not api.connected:
                stopped_early = True
                break
            gen = api._iter_local(lp, rp, is_dir, include_dirs=True) if direction == "upload" \
                else api._iter_remote(sftp, rp, lp, is_dir, local_root, include_dirs=True)
            for triple in gen:
                if stop_event.is_set() or not api.connected:
                    stopped_early = True
                    break
                batch.append(triple)
                batch_cap = min(64, constants.SCAN_QUEUE_HIGH_WATER) or 64
                if len(batch) >= batch_cap:
                    flush()
                    if stop_event.is_set() or not api.connected:
                        stopped_early = True
                        break
            if stopped_early:
                break
        flush()
        if stop_event.is_set():
            api._worker_log(f"Scan stopped ({found} file(s) queued before stopping)", "warn")
        elif not api.connected:
            api._worker_log(f"Scan halted: disconnected ({found} file(s) queued)", "warn")
        elif found:
            api._worker_log(f"Scan complete: {found} file(s) queued")
        else:
            api._worker_log("Scan complete: nothing to transfer")
    except Exception as e:
        api._worker_log(f"scan failed: {friendly_error(e)}", "error")
        debug.log("SCAN failed", traceback.format_exc())
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        api._deregister_scan(scan_id)
