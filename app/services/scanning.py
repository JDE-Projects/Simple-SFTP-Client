"""Service functions for the scanning area."""

import os
import stat
import time
import threading
import posixpath
from app import constants
from app.debug import log_event
from app.errors import friendly_error
from app.paths import _local_link_kind, is_local_link, safe_local_child, windows_destination_key
from app.scratch_journal import is_active_local


class _ScanStopped(Exception):
    """Private signal used to stop a problem-heavy scan promptly."""


class ScanProblems:
    """Bounded problem list for an ordinary transfer scan."""

    _MAX_PROBLEMS = 10_000
    suppress_console = True

    def __init__(self, api, scan_id, initial=()):
        self._api = api
        self._scan_id = scan_id
        self._items = []
        self._count = 0
        for message in initial:
            self._add(message)

    def _add(self, message):
        self._count += 1
        if len(self._items) < self._MAX_PROBLEMS:
            self._items.append(message)
        with self._api._scan_lock:
            entry = self._api._scans.get(self._scan_id)
            if entry is not None:
                entry["not_read"] += 1
                return entry["stop"].is_set()
        return False

    def append(self, message):
        if self._add(message):
            raise _ScanStopped()

    def add(self, message):
        """Record a finalization problem without converting it into a stop."""
        self._add(message)

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        return self._items[index]

    @property
    def items(self):
        return list(self._items)

    @property
    def more(self):
        return self._count - len(self._items)


def _read_error(e, remote=False):
    """Error text for a location the scan could not read. The shared
    friendly_error wording for a permission failure gives write advice,
    which is wrong for a folder or file that could not be read."""
    if isinstance(e, PermissionError):
        return "permission denied (no access to read it)"
    return friendly_error(e) if remote else str(e)


def _record_problem(api, problems, message, event, error):
    """Log a walker failure, suppressing only ordinary-scan console noise."""
    if isinstance(problems, ScanProblems):
        log_event(event, error=error)
    else:
        api._worker_log(message, "error", event, error=error)
    if problems is not None:
        problems.append(message)


def _register_scan(api):
    """Start tracking a new background scan: a fresh stop Event and found
    count, keyed by its own id. Always a brand-new Event, never a shared
    or reused one, so a stop from an earlier scan can never reach a scan
    started after it."""
    with api._scan_lock:
        scan_id = api._scan_next_id
        api._scan_next_id += 1
        api._scans[scan_id] = {"stop": threading.Event(), "found": 0, "not_read": 0,
                               "stop_reason": None}
        return scan_id, api._scans[scan_id]["stop"]


def _deregister_scan(api, scan_id):
    with api._scan_lock:
        api._scans.pop(scan_id, None)


def _stop_all_scans(api, reason=None):
    """Signal every currently-running scan to stop walking. Used by
    cancel-all and disconnect so a huge scan halts promptly instead of
    continuing to queue files nobody will drain."""
    with api._scan_lock:
        for s in api._scans.values():
            if s["stop_reason"] is None:
                s["stop_reason"] = reason
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


def _scan_not_read_total(api):
    with api._scan_lock:
        return sum(s["not_read"] for s in api._scans.values())


def _scan_stop_reason(api, scan_id):
    with api._scan_lock:
        entry = api._scans.get(scan_id)
        return entry["stop_reason"] if entry is not None else None


def _store_scan_report(api, scan_id, direction, problems):
    with api._scan_lock:
        api._scan_reports[scan_id] = {
            "direction": direction,
            "problems": problems.items,
            "more": problems.more,
        }
        while len(api._scan_reports) > 10:
            api._scan_reports.popitem(last=False)
    return scan_id


def get_scan_problems(api, report_id):
    with api._scan_lock:
        report = api._scan_reports.get(report_id)
        if report is None:
            return {"ok": False, "error": "This list is no longer available."}
        return {"ok": True, "direction": report["direction"],
                "problems": list(report["problems"]), "more": report["more"]}


def _scan_wait_for_room(api, stop_event):
    """Block the scanner thread (never the bridge thread) while the queue
    has too many WAITING items, or is paused, so a huge scan cannot flood
    memory faster than the worker pool can drain it. Returns as soon as
    there is room, the scan is stopped, or the connection drops."""
    while not stop_event.is_set() and api._connected:
        if api._queue.waiting() >= constants.SCAN_QUEUE_HIGH_WATER:
            time.sleep(0.05)
            continue
        if api._queue.is_paused():
            time.sleep(0.05)
            continue
        break


def _iter_local(api, lp, rp, is_dir, problems=None, include_dirs=False, excluded=None,
                allow_root_link=False):
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

    problems, if given, collects a short descriptor for each listing
    failure or unreadable entry, so the caller knows the walk was
    incomplete. Compare/Sync pass a plain list and each problem is also a
    console line. The transfer scan passes a ScanProblems collector: the
    problem goes to the debug log only, and the scan reports one summary
    at the end. An entry inside a folder whose metadata cannot be read is
    skipped; a single file picked by name is still yielded, so its
    transfer fails on its own retryable row."""
    def report_detection_error(path, error):
        message = f"could not read {path}: {_read_error(error)}"
        _record_problem(api, problems, message, "link skipped", error)

    def skip_link(path, st):
        if excluded is not None:
            excluded.append(path)
        api._worker_log(
            f"skipped {path}: it is a {_local_link_kind(st)}; links are not uploaded",
            "warn", "link skipped")

    if not allow_root_link:
        try:
            root_st = os.lstat(lp)
        except OSError as e:
            if is_dir:
                message = f"could not list {lp}: {_read_error(e)}"
                _record_problem(api, problems, message, "list failed", e)
            else:
                report_detection_error(lp, e)
            return False
        if is_local_link(root_st):
            skip_link(lp, root_st)
            return False
    if not is_dir:
        if is_active_local(lp):
            api._worker_log(f"skipped {lp}: a download is still writing this scratch file", "info")
            return False
        try:
            st = os.lstat(lp)
            size, mtime = st.st_size, int(st.st_mtime)
        except OSError as e:
            # A file picked by name is still queued, so the transfer fails on
            # its own retryable row. Compare/Sync still treat it as a problem.
            size, mtime = 0, 0
            if problems is not None and not isinstance(problems, ScanProblems):
                problems.append(f"could not read {lp}: {_read_error(e)}")
        yield (lp, rp, size, mtime, False)
        return True
    had_file = False
    try:
        with os.scandir(lp) as it:
            for entry in it:
                if is_active_local(entry.path):
                    continue
                rchild = posixpath.join(rp, entry.name)
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    report_detection_error(entry.path, e)
                    continue
                if is_local_link(st):
                    skip_link(entry.path, st)
                    continue
                try:
                    child_is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as e:
                    report_detection_error(entry.path, e)
                    continue
                if child_is_dir:
                    had = yield from api._iter_local(
                        entry.path, rchild, True, problems=problems, include_dirs=include_dirs,
                        excluded=excluded)
                    had_file = had_file or had
                else:
                    try:
                        size, mtime = st.st_size, int(st.st_mtime)
                    except OSError as e:
                        _record_problem(api, problems, f"could not read {entry.path}: {_read_error(e)}",
                                        "link skipped", e)
                        continue
                    yield (entry.path, rchild, size, mtime, False)
                    had_file = True
    except OSError as e:
        _record_problem(api, problems, f"could not list {lp}: {_read_error(e)}", "list failed", e)
        return False
    if include_dirs and not had_file:
        yield (lp, rp, 0, 0, True)
    return had_file


def _iter_remote(api, sftp, rp, lp, is_dir, root, problems=None, include_dirs=False, clashes=None):
    """Same as _iter_local but over an sftp session for a remote file or
    folder, again without creating any directories. sftp must be a
    session owned by the caller (the scanner opens its own, never
    self._sftp, which stays reserved for the file browser). root is the
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

    problems, if given, collects a short descriptor for each listing
    failure; see _iter_local for how each caller uses it. An unsafe remote name is skipped and logged only,
    never recorded as a problem (see the ValueError branch below). clashes,
    if given, collects sibling names that collide as Windows destinations;
    without it those clashes are logged but otherwise skipped.

    listdir_iter() pipelines read-ahead READDIR requests on the sftp
    session, so starting a second listdir_iter() on the same session
    (recursing into a subfolder) before the current one is fully drained
    hangs the session. So files are yielded the instant they are seen,
    but subfolders are only buffered as (rchild, lchild) name pairs here
    and recursed into after this level's loop finishes. That buffer holds
    only subfolder names, not the whole level, so the giant-flat-directory
    case (no subfolders) stays effectively unbuffered."""
    if not is_dir:
        if api._is_active_remote_scratch(rp):
            api._worker_log(f"skipped {rp}: an upload is still writing this scratch file", "info")
            return False
        # A file picked by name is always queued, even when its details
        # cannot be read: the transfer then fails on its own retryable row.
        size, mtime = api._rstat(sftp, rp)
        yield (lp, rp, size, mtime, False)
        return True
    had_file = False
    subdirs = []
    siblings = {}
    try:
        for a in sftp.listdir_iter(rp):
            rchild = posixpath.join(rp, a.filename)
            if api._is_active_remote_scratch(rchild):
                continue
            try:
                lchild = safe_local_child(lp, a.filename, root)
            except ValueError as e:
                # An unsafe remote name is skipped and logged, not treated
                # as an incomplete-scan problem: it can never be represented
                # locally, so it would block compare/sync on a whole folder
                # over one untransferable name (e.g. a legitimate Linux name
                # that is illegal on Windows). This matches the transfer
                # scan's skip-and-report behavior.
                api._worker_log(f"skipped unsafe remote name {a.filename!r}: {e}", "error", "unsafe remote name", error=e)
                continue
            key = windows_destination_key(lchild)
            other_remote = siblings.get(key)
            if other_remote is not None:
                api._worker_log(
                    f"skipped {rchild}: same name on Windows as {other_remote}. "
                    "To get it, download it on its own into a different local folder.",
                    "error", "name clash")
                if clashes is not None:
                    clashes.append({"local_path": lchild, "remote_path": rchild,
                                    "other_remote": other_remote,
                                    "is_dir": stat.S_ISDIR(a.st_mode)})
                continue
            siblings[key] = rchild
            if stat.S_ISDIR(a.st_mode):
                subdirs.append((rchild, lchild))
            else:
                yield (lchild, rchild, a.st_size, int(a.st_mtime or 0), False)
                had_file = True
    except Exception as e:
        _record_problem(api, problems, f"could not list {rp}: {_read_error(e, remote=True)}",
                        "list failed", e)
        return False
    for rchild, lchild in subdirs:
        had = yield from api._iter_remote(
            sftp, rchild, lchild, True, root, problems=problems,
            include_dirs=include_dirs, clashes=clashes)
        had_file = had_file or had
    if include_dirs and not had_file:
        yield (lp, rp, 0, 0, True)
    return had_file


def _scan_and_queue(api, roots, direction, on_conflict, scan_id, stop_event, local_root=None,
                    initial_problems=()):
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
    clashes = []
    admission_clashes = 0
    scan_clashes = 0
    excluded = []
    batch = []
    problems = ScanProblems(api, scan_id, initial_problems)
    try:
        if direction == "download":
            try:
                sftp = api._client.open_sftp()
            except Exception as e:
                problems.append(f"could not open a transfer session: {e}")

        def flush():
            nonlocal batch, found, admission_clashes
            if not batch:
                return
            api._scan_wait_for_room(stop_event)
            if stop_event.is_set() or not api._connected:
                return
            queued, clash_count = api._enqueue_files(
                batch, direction, on_conflict, local_root=local_root)
            found += queued
            admission_clashes += clash_count
            api._bump_scan_found(scan_id, queued)
            batch = []

        def flush_clashes():
            nonlocal clashes, scan_clashes
            for clash in clashes:
                api._queue.add_clash(direction, clash["local_path"], clash["remote_path"],
                                     os.path.basename(clash["local_path"]),
                                     clash["other_remote"], clash["is_dir"])
                scan_clashes += 1
            clashes = []

        stopped_early = False
        root_keys = {}
        for lp, rp, is_dir in (roots if sftp is not None or direction == "upload" else ()):
            if stop_event.is_set() or not api._connected:
                stopped_early = True
                break
            if direction == "download":
                key = windows_destination_key(lp)
                other_remote = root_keys.get(key)
                if other_remote is not None:
                    clashes.append({"local_path": lp, "remote_path": rp,
                                    "other_remote": other_remote, "is_dir": is_dir})
                    continue
                root_keys[key] = rp
                gen = api._iter_remote(sftp, rp, lp, is_dir, local_root,
                                       problems=problems, include_dirs=True, clashes=clashes)
            else:
                gen = api._iter_local(lp, rp, is_dir, problems=problems,
                                     include_dirs=True, excluded=excluded)
            for triple in gen:
                if stop_event.is_set() or not api._connected:
                    stopped_early = True
                    break
                batch.append(triple)
                batch_cap = min(64, constants.SCAN_QUEUE_HIGH_WATER) or 64
                if len(batch) >= batch_cap:
                    flush()
                    flush_clashes()
                    if stop_event.is_set() or not api._connected:
                        stopped_early = True
                        break
            if stopped_early:
                break
        flush()
        flush_clashes()
    except _ScanStopped:
        pass
    except Exception as e:
        try:
            flush()
            flush_clashes()
        except Exception:
            pass
        try:
            problems.append(f"scan failed: {e}")
        except _ScanStopped:
            pass
    finally:
        reason = _scan_stop_reason(api, scan_id)
        lost = reason == "connection lost" or (reason is None and not api._connected)
        clash_count = scan_clashes + admission_clashes
        link_suffix = f", {len(excluded)} link(s) skipped" if excluded else ""
        clash_suffix = f", {clash_count} name clash(es) not downloaded" if clash_count else ""
        if lost:
            if not len(problems):
                problems.add("Connection lost before every folder was read.")
            report_id = _store_scan_report(api, scan_id, direction, problems)
            extra = f", {len(problems)} location(s) not read" if len(problems) else ""
            api._queue.add_scan_incomplete(
                direction, "Scan halted",
                "Connection lost before every folder was read. Reconnect and run the transfer again.",
                report_id)
            api._worker_log(
                f"Scan halted: disconnected before every folder was read ({found} file(s) queued{extra})"
                f"{clash_suffix}{link_suffix}", "error", "scan", outcome="disconnected",
                items=found, report_id=report_id)
        elif len(problems):
            report_id = _store_scan_report(api, scan_id, direction, problems)
            api._queue.add_scan_incomplete(
                direction, "Scan incomplete",
                f"{len(problems)} location(s) not read. Run the transfer again after fixing access.",
                report_id)
            api._worker_log(
                f"Scan incomplete: {found} file(s) queued, {len(problems)} location(s) not read"
                f"{clash_suffix}{link_suffix}", "warn", "scan", outcome="incomplete",
                items=found, report_id=report_id)
        elif stop_event.is_set():
            api._worker_log(f"Scan stopped ({found} file(s) queued before stopping{clash_suffix}{link_suffix})",
                            "warn", "scan", outcome="stopped", items=found)
        elif clash_count or excluded:
            api._worker_log(f"Scan complete: {found} file(s) queued{clash_suffix}{link_suffix}",
                            "warn", "scan", outcome="complete", items=found)
        elif found:
            api._worker_log(f"Scan complete: {found} file(s) queued", event="scan", outcome="complete", items=found)
        else:
            api._worker_log("Scan complete: nothing to transfer", event="scan", outcome="complete", items=0)
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        api._deregister_scan(scan_id)
