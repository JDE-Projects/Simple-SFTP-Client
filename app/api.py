"""The bridge object the page calls."""

import os
import stat
import time
import threading
import functools
import traceback
import posixpath


from app import constants
from app import services
from app.constants import MTIME_TOL, WORKER_COUNT
from app.debug import debug
from app.errors import ScanIncomplete, friendly_error
from app.paths import is_temp_part, safe_local_child
from app.transfer_queue import TransferQueue


def _browsing(method):
    """Serialize a bridge call that uses the shared browsing SFTP session
    (self.sftp). Paramiko's synchronous SFTP is not safe for two callers on one
    session at once, and pywebview runs each bridge call on its own thread, so
    without this the health ping and a listing (or any two browsing operations)
    can overlap and consume each other's replies, leaving a listing blocked with
    no error. Uses the re-entrant self._sftp_lock so a guarded method may call
    another guarded helper on the same thread (delete -> _rremove). Transfer
    workers and the background scanner each own a separate session, so they are
    deliberately not guarded here and keep running in parallel."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._sftp_lock:
            return method(self, *args, **kwargs)
    return wrapper


class Api:
    def __init__(self, app_version):
        self._app_version = app_version
        self._window = None
        self.connected = False
        self.client = None
        self.sftp = None
        self._cred_pass = ""
        # Set only after a successful PASSWORD login: (host, port, username,
        # "password"), all stripped. save_session may write the remembered
        # password to Credential Manager only when the settings on screen
        # match this exactly, so an edited-fields or wrong-server save is
        # refused rather than misfiled.
        self._cred_identity = None
        self._pending_host_key = None  # (hostname, offered_key) awaiting user trust
        self._lock = threading.Lock()
        # Serializes every operation on the shared browsing session (self.sftp)
        # so two bridge threads never use it at once. Re-entrant so a guarded
        # call can nest another (see _browsing). Transfer workers and the
        # scanner use their own sessions and are intentionally not on this lock.
        self._sftp_lock = threading.RLock()
        self._cancel = threading.Event()
        # Watcher lifecycle. _watch_lock guards start/stop/replace of the two
        # fields below so a stop can never null them while a restart is setting
        # them. Each run also captures its own stop Event locally (never these
        # shared fields), so an old thread can't observe a newer run's event.
        self._watch_lock = threading.Lock()
        self._watch_stop = None
        self._watch_thread = None
        self._watch_interval = 2.0  # seconds between watch polls
        self._watch_refresh_lock = threading.Lock()
        self._watch_refresh = set()
        # transfer queue: a pool of workers drains it, each over its own SFTP
        # session (never self.sftp, that stays reserved for the file browser).
        # Pool size is WORKER_COUNT (2) by default, up to WORKER_COUNT_MAX (5)
        # for a batch of many small files.
        self.queue = TransferQueue()
        self._workers = []  # live worker Thread objects, at most self._target_workers
        self._worker_lock = threading.Lock()
        # How many workers the pool tops up to. A fresh batch sets this (see
        # _enqueue_files); it resets to WORKER_COUNT once the pool empties.
        self._target_workers = WORKER_COUNT
        # set while sync/watch/external-drop run, so the queue worker knows to wait
        self._legacy_active = threading.Event()
        # Per-connection memory of files whose modification time could not be
        # stamped after a transfer (server refused on upload, or os.utime
        # failed on download). Maps a normalized (local_path, remote_path)
        # key (see _mtime_fallback_key) to the file size recorded at transfer
        # time, so a later same-size compare on this connection still reads
        # the file as unchanged instead of newer_local/newer_remote forever.
        # Cleared on disconnect (see shutdown()): it does not survive a
        # reconnect.
        self._mtime_fallback = {}
        self._mtime_fallback_lock = threading.Lock()
        # Poll model: workers never touch evaluate_js. They write plain state
        # here, the window pulls it on a timer via poll_queue(). Progress is
        # keyed by queue item id since up to WORKER_COUNT_MAX items can be
        # active at once; guarded by its own lock since multiple workers write it.
        self._progress_by_id = {}
        self._progress_lock = threading.Lock()
        self._console_buffer = []
        self._console_lock = threading.Lock()
        # Background folder scans (enqueue/upload_paths): each running scan
        # gets its own id and stop Event here, guarded by _scan_lock. A fresh
        # transfer always gets a brand-new Event, so a stop from a previous,
        # already-finished scan can never reach into a later one.
        self._scan_lock = threading.Lock()
        self._scans = {}   # scan_id -> {"stop": threading.Event, "found": int}
        self._scan_next_id = 1
        # Background compare/sync jobs (compare/sync_plan): a separate
        # registry from _scans, on its own lock, so the "Comparing… N"
        # progress channel never leaks into the download-scan "Scanning…
        # N found" label or vice versa. See _start_compare for the entry
        # shape.
        self._compare_lock = threading.Lock()
        self._compares = {}
        self._compare_next_id = 1
        # One idempotent teardown path (see shutdown()): guards Disconnect,
        # window close, and a plain process exit from ever running the close
        # sequence twice. Cleared again on the next successful connect() so a
        # later disconnect runs the full sequence again.
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        # Last folder list_local() actually listed (never the synthetic
        # "DRIVES" placeholder); connect() sweeps this folder for leftover
        # .sxtpart scratch files. See _sweep_scratch_files.
        self._local_cwd = None
        # First worker to notice the shared transport itself is dead (as
        # opposed to one file failing) reports it and stops the batch; this
        # keeps that report to one console line instead of one per worker.
        # Reset on the next successful connect().
        self._conn_dead_lock = threading.Lock()
        self._conn_dead_reported = False
        # Set by confirm_quit() once the page answers yes to the "transfers
        # are still running" quit prompt raised from main()'s closing veto.
        self._quit_confirmed = False
        # Debug log warnings (a failed write, a locked old log that couldn't
        # be pruned...) land here from debug.on_warning, which can fire on any
        # thread. Never surfaced through _emit/evaluate_js: see _on_debug_warning.
        self._debug_warnings = []
        self._debug_warnings_lock = threading.Lock()

    def set_window(self, w):
        return services.window.set_window(self, w)

    def get_meta(self):
        return services.window.get_meta(self)

    def get_theme(self):
        return services.window.get_theme(self)

    def save_theme(self, theme):
        return services.window.save_theme(self, theme)

    def set_debug(self, on):
        return services.window.set_debug(self, on)

    def _on_debug_warning(self, msg):
        return services.window._on_debug_warning(self, msg)

    def _drain_debug_warnings(self):
        return services.window._drain_debug_warnings(self)

    def drain_debug_warnings(self):
        return services.window.drain_debug_warnings(self)

    def export_console(self, text):
        return services.window.export_console(self, text)

    def _emit(self, event, payload):
        return services.window._emit(self, event, payload)

    def _vlog(self, msg, level="info"):
        return services.window._vlog(self, msg, level)

    def _worker_log(self, msg, level="info"):
        return services.window._worker_log(self, msg, level)

    # ───────────── sessions (servers.json, never passwords) ─────────────
    def _load_sessions(self):
        return services.sessions._load_sessions(self)

    def _sessions_notice(self, msg):
        return services.sessions._sessions_notice(self, msg)

    def _save_sessions(self, sessions):
        return services.sessions._save_sessions(self, sessions)

    def save_session(self, s):
        return services.sessions.save_session(self, s)

    def delete_session(self, name):
        return services.sessions.delete_session(self, name)

    def _remembered_password(self, host, username, port=22):
        return services.sessions._remembered_password(self, host, username, port)

    def get_remembered(self, host, username, port=22):
        return services.sessions.get_remembered(self, host, username, port)

    # ───────────── connect ─────────────
    def _open(self, host, port, username, password, key_path, passphrase):
        return services.connections._open(self, host, port, username, password, key_path, passphrase)

    def _close_partial(self, client, sftp):
        return services.connections._close_partial(self, client, sftp)

    def connect(self, p):
        return services.connections.connect(self, p)

    def _sweep_scratch_files(self):
        return services.connections._sweep_scratch_files(self)

    def trust_host_key(self):
        return services.connections.trust_host_key(self)

    def get_host_key(self, host, port=22):
        return services.connections.get_host_key(self, host, port)

    def _transport_info(self, client=None):
        return services.connections._transport_info(self, client)

    def test_connection(self, p):
        return services.connections.test_connection(self, p)

    def disconnect(self):
        return services.connections.disconnect(self)

    def shutdown(self):
        return services.window.shutdown(self)

    def _transfers_active(self):
        return services.transfers._transfers_active(self)

    def transfers_active(self):
        return services.transfers.transfers_active(self)

    def confirm_quit(self):
        return services.window.confirm_quit(self)

    @_browsing
    def ping(self):
        return services.browsing.ping(self)

    # ───────────── listing ─────────────
    def list_local(self, path):
        return services.browsing.list_local(self, path)

    @_browsing
    def list_remote(self, path):
        return services.browsing.list_remote(self, path)

    # ───────────── file ops ─────────────
    @_browsing
    def make_dir(self, side, path, name):
        return services.browsing.make_dir(self, side, path, name)

    @_browsing
    def rename(self, side, path, old, new):
        return services.browsing.rename(self, side, path, old, new)

    @_browsing
    def delete(self, side, path, items):
        return services.browsing.delete(self, side, path, items)

    def _rremove(self, path):
        return services.browsing._rremove(self, path)

    def open_local(self, path, name):
        return services.browsing.open_local(self, path, name)

    # ───────────── background scans (streaming enqueue) ─────────────
    def _register_scan(self):
        """Start tracking a new background scan: a fresh stop Event and found
        count, keyed by its own id. Always a brand-new Event, never a shared
        or reused one, so a stop from an earlier scan can never reach a scan
        started after it."""
        with self._scan_lock:
            scan_id = self._scan_next_id
            self._scan_next_id += 1
            self._scans[scan_id] = {"stop": threading.Event(), "found": 0}
            return scan_id, self._scans[scan_id]["stop"]

    def _deregister_scan(self, scan_id):
        with self._scan_lock:
            self._scans.pop(scan_id, None)

    def _stop_all_scans(self):
        """Signal every currently-running scan to stop walking. Used by
        cancel-all and disconnect so a huge scan halts promptly instead of
        continuing to queue files nobody will drain."""
        with self._scan_lock:
            for s in self._scans.values():
                s["stop"].set()

    def _scan_active(self):
        with self._scan_lock:
            return bool(self._scans)

    def _bump_scan_found(self, scan_id, n):
        with self._scan_lock:
            entry = self._scans.get(scan_id)
            if entry is not None:
                entry["found"] += n

    def _scan_found_total(self):
        """Sum of found-so-far across every active scan, for poll_queue()."""
        with self._scan_lock:
            return sum(s["found"] for s in self._scans.values())

    def _scan_wait_for_room(self, stop_event):
        """Block the scanner thread (never the bridge thread) while the queue
        has too many WAITING items, or is paused, so a huge scan cannot flood
        memory faster than the worker pool can drain it. Returns as soon as
        there is room, the scan is stopped, or the connection drops."""
        while not stop_event.is_set() and self.connected:
            if self.queue.waiting() >= constants.SCAN_QUEUE_HIGH_WATER:
                time.sleep(0.05)
                continue
            if self.queue.is_paused():
                time.sleep(0.05)
                continue
            break

    def _iter_local(self, lp, rp, is_dir, problems=None, include_dirs=False):
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
                        had = yield from self._iter_local(
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
            self._worker_log(f"could not list {lp}: {e}", "error")
            if problems is not None:
                problems.append(f"could not list {lp}: {e}")
            return False
        if include_dirs and not had_file:
            yield (lp, rp, 0, 0, True)
        return had_file

    def _iter_remote(self, sftp, rp, lp, is_dir, root, problems=None, include_dirs=False):
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
            size, mtime = self._rstat(sftp, rp)
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
                    self._worker_log(f"skipped unsafe remote name {a.filename!r}: {e}", "error")
                    continue
                if stat.S_ISDIR(a.st_mode):
                    subdirs.append((rchild, lchild))
                else:
                    yield (lchild, rchild, a.st_size, int(a.st_mtime or 0), False)
                    had_file = True
        except Exception as e:
            self._worker_log(f"could not list {rp}: {friendly_error(e)}", "error")
            if problems is not None:
                problems.append(f"could not list {rp}: {friendly_error(e)}")
            return False
        for rchild, lchild in subdirs:
            had = yield from self._iter_remote(
                sftp, rchild, lchild, True, root, problems=problems, include_dirs=include_dirs)
            had_file = had_file or had
        if include_dirs and not had_file:
            yield (lp, rp, 0, 0, True)
        return had_file

    def _scan_and_queue(self, roots, direction, on_conflict, scan_id, stop_event, local_root=None):
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
                    sftp = self.client.open_sftp()
                except Exception as e:
                    self._worker_log(
                        f"scan: could not open a transfer session: {friendly_error(e)}", "error")
                    return

            def flush():
                nonlocal batch, found
                if not batch:
                    return
                self._scan_wait_for_room(stop_event)
                if stop_event.is_set() or not self.connected:
                    return
                found += len(batch)
                self._bump_scan_found(scan_id, len(batch))
                self._enqueue_files(batch, direction, on_conflict)
                batch = []

            stopped_early = False
            for lp, rp, is_dir in roots:
                if stop_event.is_set() or not self.connected:
                    stopped_early = True
                    break
                gen = self._iter_local(lp, rp, is_dir, include_dirs=True) if direction == "upload" \
                    else self._iter_remote(sftp, rp, lp, is_dir, local_root, include_dirs=True)
                for triple in gen:
                    if stop_event.is_set() or not self.connected:
                        stopped_early = True
                        break
                    batch.append(triple)
                    batch_cap = min(64, constants.SCAN_QUEUE_HIGH_WATER) or 64
                    if len(batch) >= batch_cap:
                        flush()
                        if stop_event.is_set() or not self.connected:
                            stopped_early = True
                            break
                if stopped_early:
                    break
            flush()
            if stop_event.is_set():
                self._worker_log(f"Scan stopped ({found} file(s) queued before stopping)", "warn")
            elif not self.connected:
                self._worker_log(f"Scan halted: disconnected ({found} file(s) queued)", "warn")
            elif found:
                self._worker_log(f"Scan complete: {found} file(s) queued")
            else:
                self._worker_log("Scan complete: nothing to transfer")
        except Exception as e:
            self._worker_log(f"scan failed: {friendly_error(e)}", "error")
            debug.log("SCAN failed", traceback.format_exc())
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            self._deregister_scan(scan_id)

    # ───────────── transfers (queue + progress + resume + retry) ─────────────
    def _connection_dead(self, sftp):
        return services.connections._connection_dead(self, sftp)

    def _report_dead_connection(self):
        return services.connections._report_dead_connection(self)

    def cancel(self):
        return services.transfers.cancel(self)

    def poll_queue(self):
        return services.transfers.poll_queue(self)

    def cancel_item(self, item_id):
        return services.transfers.cancel_item(self, item_id)

    def clear_finished(self):
        return services.transfers.clear_finished(self)

    def retry_item(self, item_id):
        return services.transfers.retry_item(self, item_id)

    def retry_all_failed(self):
        return services.transfers.retry_all_failed(self)

    def pause_queue(self):
        return services.transfers.pause_queue(self)

    def resume_queue(self):
        return services.transfers.resume_queue(self)

    def enqueue(self, jobs, direction, local_dir, remote_dir, on_conflict="overwrite"):
        return services.transfers.enqueue(self, jobs, direction, local_dir, remote_dir, on_conflict)

    def _enqueue_files(self, files, direction, on_conflict):
        return services.transfers._enqueue_files(self, files, direction, on_conflict)

    def _ensure_worker(self):
        return services.transfers._ensure_worker(self)

    def _worker_loop(self):
        return services.transfers._worker_loop(self)

    def upload_paths(self, paths, remote_dir, on_conflict="overwrite"):
        return services.transfers.upload_paths(self, paths, remote_dir, on_conflict)

    @staticmethod
    def _normalize_drop_path(p):
        return services.transfers._normalize_drop_path(p)

    def on_external_drop(self, event):
        return services.transfers.on_external_drop(self, event)

    def _one(self, direction, lp, rp, name, idx, total, on_conflict, sftp,
             cancel_check, progress_key=None, dir_cache=None):
        return services.transfers._one(self, direction, lp, rp, name, idx, total, on_conflict, sftp, cancel_check, progress_key, dir_cache)

    def _put_resume(self, sftp, lp, rp, offset, cb, cancel_check):
        return services.transfer_io._put_resume(self, sftp, lp, rp, offset, cb, cancel_check)

    def _get_resume(self, sftp, rp, lp, offset, cb, cancel_check):
        return services.transfer_io._get_resume(self, sftp, rp, lp, offset, cb, cancel_check)

    def _progress(self, name, idx, total, sent, size, elapsed, progress_key=None):
        return services.transfers._progress(self, name, idx, total, sent, size, elapsed, progress_key)

    def _rstat(self, sftp, rp):
        return services.transfers._rstat(self, sftp, rp)

    def _mtime_fallback_key(self, lp, rp):
        return services.transfer_io._mtime_fallback_key(self, lp, rp)

    def _record_mtime_fallback(self, lp, rp, size):
        return services.transfer_io._record_mtime_fallback(self, lp, rp, size)

    def _clear_mtime_fallback(self, lp, rp):
        return services.transfer_io._clear_mtime_fallback(self, lp, rp)

    def _mtime_fallback_matches(self, lp, rp, size):
        return services.transfer_io._mtime_fallback_matches(self, lp, rp, size)

    def _apply_download_mtime(self, lp, rp, mtime, size):
        return services.transfer_io._apply_download_mtime(self, lp, rp, mtime, size)

    def _apply_upload_mtime(self, sftp, rp, lp):
        return services.transfer_io._apply_upload_mtime(self, sftp, rp, lp)

    # ───────────── compare / sync plan / download-changed ─────────────
    def _compute_pair_maps(self, sftp, local_dir, remote_dir, on_progress=None, stop_event=None):
        """Builds rel -> (size, mtime, local_path, remote_path, is_dir) maps
        for both sides of a folder pair, by streaming the same
        _iter_local/_iter_remote enumerators the background scanner uses
        (including their empty-folder markers), so compare/sync descend the
        whole tree instead of one directory level. rel is a posix path
        relative to remote_dir, shared between both maps so a file or folder
        at any depth lines up on both sides. The compared root itself (rel
        ".") is never added: it is the folder being compared, not a child of
        it. on_progress, if given, is called with a count of files seen since
        the last call (not a running total), so the caller can accumulate it
        the same way the scan progress counter does. Returns (None, None) if
        stop_event fires before either walk finishes, the signal a caller
        uses to tell a cancelled compare/sync apart from a genuinely empty
        one. Raises ScanIncomplete if a folder could not be listed or a
        file's metadata could not be read during either walk, since a
        compare/sync result built on a tree that was not fully seen can
        misclassify files as one-sided or in-sync (unlike the ordinary
        transfer scan, which reports partial progress instead). Also raises
        ScanIncomplete if either side's map would grow past
        COMPARE_SYNC_ENTRY_LIMIT entries, refusing before the remote (network)
        walk ever starts if the local walk already hit the limit. An unsafe
        remote name is skipped and logged, not treated as incomplete."""
        local_map = {}
        remote_map = {}
        seen_since_report = 0
        problems = []

        def bump():
            nonlocal seen_since_report
            seen_since_report += 1
            if on_progress and seen_since_report >= 200:
                on_progress(seen_since_report)
                seen_since_report = 0

        def _raise_incomplete():
            raise ScanIncomplete(
                f"Some folders could not be read ({problems[0]}) "
                f"[{len(problems)} problem(s)]; compare/sync was not run.")

        def _raise_too_many():
            raise ScanIncomplete(
                f"Too many files to compare (more than "
                f"{constants.COMPARE_SYNC_ENTRY_LIMIT:,}). Pick a smaller folder.")

        for lp, rp, size, mtime, is_dir in self._iter_local(
                local_dir, remote_dir, True, problems=problems, include_dirs=True):
            if stop_event is not None and stop_event.is_set():
                return None, None
            rel = posixpath.relpath(rp, remote_dir)
            if rel == ".":
                continue
            if len(local_map) >= constants.COMPARE_SYNC_ENTRY_LIMIT:
                _raise_too_many()
            local_map[rel] = (size, mtime, lp, rp, is_dir)
            bump()
        # Fail fast: if the local walk already found a problem, refuse now
        # rather than running the whole remote (network) walk just to discard it.
        if problems:
            _raise_incomplete()
        for lp, rp, size, mtime, is_dir in self._iter_remote(
                sftp, remote_dir, local_dir, True, local_dir, problems=problems, include_dirs=True):
            if stop_event is not None and stop_event.is_set():
                return None, None
            rel = posixpath.relpath(rp, remote_dir)
            if rel == ".":
                continue
            if len(remote_map) >= constants.COMPARE_SYNC_ENTRY_LIMIT:
                _raise_too_many()
            remote_map[rel] = (size, mtime, lp, rp, is_dir)
            bump()
        if on_progress and seen_since_report:
            on_progress(seen_since_report)
        if stop_event is not None and stop_event.is_set():
            return None, None
        if problems:
            _raise_incomplete()
        return local_map, remote_map

    def _classify(self, rel, local_map, remote_map):
        """Metadata equality, not proven byte equality: a pair is 'same' when
        sizes match and modification times agree within MTIME_TOL. Because
        transfers preserve the source mtime, a same-size edit does not hide
        as 'same' -- its mtime differs, so it sorts to newer_local
        or newer_remote by time, UNLESS this connection remembers this pair as
        having failed its time stamp at transfer time (see
        _mtime_fallback_matches), in which case a matching size alone still
        counts as 'same'. local_map/remote_map are rel -> (size, mtime, lp,
        rp, is_dir) as built by _compute_pair_maps.

        A rel present on only one side is 'local_only'/'remote_only' whether
        it is a file or a folder. A rel present on both sides where one side
        is a folder and the other a file is a 'conflict': never resolved
        automatically, never overwritten. A rel that is a folder on both
        sides is 'same' (both are empty-subtree folders at the same path, so
        there is nothing to move)."""
        if rel in local_map and rel not in remote_map:
            return "local_only"
        if rel in remote_map and rel not in local_map:
            return "remote_only"
        l_is_dir = local_map[rel][4]
        r_is_dir = remote_map[rel][4]
        if l_is_dir != r_is_dir:
            return "conflict"
        if l_is_dir and r_is_dir:
            return "same"
        lsize, lmtime = local_map[rel][0], local_map[rel][1]
        rsize, rmtime = remote_map[rel][0], remote_map[rel][1]
        if lsize == rsize:
            if abs(lmtime - rmtime) <= MTIME_TOL:
                return "same"
            lp, rp = local_map[rel][2], remote_map[rel][3]
            if self._mtime_fallback_matches(lp, rp, lsize):
                return "same"
        return "newer_local" if lmtime >= rmtime else "newer_remote"

    def _compute_compare(self, sftp, local_dir, remote_dir, on_progress=None, stop_event=None):
        """Pure recursive compare core: no threading, no bridge concerns, so
        it can be called directly from tests or from the background thread
        _run_compare drives. Returns {"files": {rel: status}, "folders":
        {rel: status}}, or None if stop_event fired before the walk finished.

        "folders" holds both kinds of entry: every ancestor of a changed file
        gets "has_changes" (so a deep change is visible without opening every
        level), and every rel that is itself an empty-subtree folder gets its
        own explicit status (local_only/remote_only/conflict; a folder that
        is 'same' on both sides adds nothing, since there is no change to
        show). The explicit status is applied after the ancestor pass so it
        always wins if the two would ever land on the same rel. A "conflict"
        rel (a file on one side, an empty folder on the other) is added to
        BOTH "files" and "folders", so whichever pane is showing it (the file
        row in one, the folder row in the other) is colored to flag it."""
        local_map, remote_map = self._compute_pair_maps(
            sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
        if local_map is None:
            return None
        statuses = {rel: self._classify(rel, local_map, remote_map)
                    for rel in set(local_map) | set(remote_map)}

        def is_dir_rel(rel):
            entry = local_map.get(rel) or remote_map.get(rel)
            return bool(entry[4])

        files = {}
        folders = {}
        for rel, status in statuses.items():
            if status == "conflict":
                files[rel] = "conflict"
                folders[rel] = "conflict"
                continue
            if is_dir_rel(rel):
                if status != "same":
                    folders[rel] = status
            else:
                files[rel] = status
        for rel, status in statuses.items():
            if status == "same":
                continue
            parent = posixpath.dirname(rel)
            while parent not in ("", "."):
                folders.setdefault(parent, "has_changes")
                parent = posixpath.dirname(parent)
        return {"files": files, "folders": folders}

    def _compute_sync(self, sftp, local_dir, remote_dir, direction, changed_only=True,
                       on_progress=None, stop_event=None):
        """Pure recursive sync-plan core (see _compute_compare). Returns
        (plan, transfers, conflicts): plan is the list the UI shows (name is
        the rel path, so a nested file's plan entry still reads as its full
        relative name, and is_dir marks a folder-creation entry rather than a
        file); transfers is the resolved (local_path, remote_path, size,
        is_dir) list actually handed to the queue, built from the same maps
        so it never has to re-derive a path from a name that came back from
        the page. conflicts is a list of {"name": rel} for every rel that is
        a file on one side and an empty folder on the other: a conflict is
        never transferred in either direction (it is excluded from `wanted`
        for both directions), only reported, so nothing is ever overwritten
        to resolve it. Returns (None, None, None) if stop_event fired before
        the walk finished."""
        local_map, remote_map = self._compute_pair_maps(
            sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
        if local_map is None:
            return None, None, None
        wanted = ("local_only", "newer_local") if direction == "upload" else ("remote_only", "newer_remote")
        plan = []
        transfers = []
        conflicts = []
        for rel in set(local_map) | set(remote_map):
            status = self._classify(rel, local_map, remote_map)
            if status == "conflict":
                conflicts.append({"name": rel})
                continue
            if status not in wanted and (changed_only or status == "same"):
                continue
            lentry = local_map.get(rel)
            rentry = remote_map.get(rel)
            local = {"size": lentry[0], "mtime": lentry[1]} if lentry else None
            remote = {"size": rentry[0], "mtime": rentry[1]} if rentry else None
            is_dir = bool((lentry or rentry)[4])
            plan.append({"name": rel, "status": status, "local": local, "remote": remote,
                         "is_dir": is_dir})
            # The transfer's source side must actually exist to send anything;
            # changed_only=False can otherwise list a status that makes no
            # sense for this direction (e.g. a remote_only file on an
            # upload), which nothing can transfer.
            source = lentry if direction == "upload" else rentry
            if source is not None:
                transfers.append((source[2], source[3], source[0], is_dir))
        return plan, transfers, conflicts

    # ───────────── compare / sync background jobs (own thread, own session) ─────────────
    def _start_compare(self, kind):
        """Start tracking a new background compare/sync job, mirroring
        _register_scan: its own stop Event and found count, keyed by its own
        id, so a stop from an earlier job can never reach a later one. Kept
        on a separate registry from _scans so the download-scan "Scanning…
        N found" label and the compare/sync "Comparing… N" label never
        leak into each other."""
        with self._compare_lock:
            cid = self._compare_next_id
            self._compare_next_id += 1
            self._compares[cid] = {
                "stop": threading.Event(), "found": 0, "kind": kind, "direction": None,
                "done": False, "ok": False, "error": None, "result": None,
                "transfers": None, "delivered": False,
            }
            return cid, self._compares[cid]["stop"]

    def _deregister_compare(self, cid):
        with self._compare_lock:
            self._compares.pop(cid, None)

    def _stop_all_compares(self):
        """Signal every currently-running compare/sync job to stop walking.
        Used by Cancel, a dead connection, and disconnect, same as
        _stop_all_scans."""
        with self._compare_lock:
            for c in self._compares.values():
                c["stop"].set()

    def _compare_active(self):
        with self._compare_lock:
            return any(not c["done"] for c in self._compares.values())

    def _bump_compare_found(self, cid, n):
        with self._compare_lock:
            entry = self._compares.get(cid)
            if entry is not None:
                entry["found"] += n

    def _compare_found_total(self):
        """Sum of found-so-far across every still-running compare/sync job,
        for poll_queue()."""
        with self._compare_lock:
            return sum(c["found"] for c in self._compares.values() if not c["done"])

    def _run_compare(self, cid, local_dir, remote_dir, stop_event, direction=None, changed_only=True):
        """Runs a compare or a sync-plan computation entirely on its own
        daemon thread, never the pywebview bridge thread, over its own sftp
        session (self.sftp stays reserved for the file browser). kind is
        read off the registered entry: "compare" stores the recursive
        files/folders result; "sync" stores a plan summary and stashes the
        full transfer list on the entry for start_sync() to stream later.
        Always marks the entry done and closes its session, and never lets
        an unexpected exception fail silently."""
        with self._compare_lock:
            entry = self._compares.get(cid)
        kind = entry["kind"] if entry else "compare"

        def _finish(ok, error=None, result=None, transfers=None):
            with self._compare_lock:
                e = self._compares.get(cid)
                if e is not None:
                    e["ok"] = ok
                    e["error"] = error
                    e["result"] = result
                    e["transfers"] = transfers
                    e["done"] = True

        sftp = None
        try:
            try:
                sftp = self.client.open_sftp()
            except Exception as e:
                reason = friendly_error(e)
                self._worker_log(f"compare: could not open a transfer session: {reason}", "error")
                _finish(False, error=reason)
                return
            on_progress = lambda n: self._bump_compare_found(cid, n)  # noqa: E731
            if kind == "sync":
                plan, transfers, conflicts = self._compute_sync(
                    sftp, local_dir, remote_dir, direction, changed_only,
                    on_progress=on_progress, stop_event=stop_event)
                if plan is None:
                    _finish(False, error="Cancelled.")
                    return
                with self._compare_lock:
                    e = self._compares.get(cid)
                    if e is not None:
                        e["direction"] = direction
                total_bytes = sum(t[2] for t in transfers if t[2] and t[2] > 0)
                result = {"count": len(transfers), "total_bytes": total_bytes,
                          "sample": plan[:200], "more": max(0, len(plan) - 200), "token": cid,
                          "conflicts": conflicts}
                _finish(True, result=result, transfers=transfers)
            else:
                data = self._compute_compare(
                    sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
                if data is None:
                    _finish(False, error="Cancelled.")
                    return
                data["root_local"] = local_dir
                data["root_remote"] = remote_dir
                _finish(True, result=data)
        except ScanIncomplete as e:
            self._worker_log(f"compare: {e}", "error")
            _finish(False, error=str(e))
        except Exception as e:
            reason = friendly_error(e)
            self._worker_log(f"compare failed: {reason}", "error")
            debug.log("COMPARE failed", traceback.format_exc())
            _finish(False, error=reason)
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass

    def compare(self, local_dir, remote_dir):
        """Starts a recursive compare on its own daemon thread and returns
        immediately; the result arrives via poll_queue()'s compare_done key.
        Deliberately not @_browsing: it must not hold the shared browsing
        session lock, since it walks over its own sftp session and can take
        a long time on a big tree."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._compare_active():
            return {"ok": False, "error": "A compare or sync is already running."}
        cid, stop_event = self._start_compare("compare")
        t = threading.Thread(target=self._run_compare, args=(cid, local_dir, remote_dir, stop_event),
                              daemon=True)
        t.start()
        return {"ok": True, "comparing": True}

    def sync_plan(self, local_dir, remote_dir, direction, changed_only=True):
        """Starts a recursive sync-plan computation on its own daemon thread
        and returns immediately; the summary (and a token for start_sync())
        arrives via poll_queue()'s compare_done key. Not @_browsing, for the
        same reason as compare().

        Safety net: drops any earlier finished sync plan still sitting in
        _compares before registering the new one. Normally the page frees a
        plan itself (consuming it via start_sync() or declining it via
        discard_sync()), but if the page never gets the chance (a reload
        while a plan is still on screen), this keeps at most one unconsumed
        plan around instead of piling one up per sync attempt."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._legacy_active.is_set():
            return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
        if self._compare_active():
            return {"ok": False, "error": "A compare or sync is already running."}
        with self._compare_lock:
            stale = [cid for cid, e in self._compares.items() if e["kind"] == "sync" and e["done"]]
            for cid in stale:
                self._compares.pop(cid, None)
        cid, stop_event = self._start_compare("sync")
        t = threading.Thread(target=self._run_compare,
                              args=(cid, local_dir, remote_dir, stop_event, direction, changed_only),
                              daemon=True)
        t.start()
        return {"ok": True, "comparing": True}

    def _stream_sync_transfers(self, transfers, direction, on_conflict, scan_id, stop_event, token):
        """Streams an already-resolved sync-plan transfer list onto the
        normal transfer queue in backpressured batches, reusing the same
        scan registry (and so the same "Scanning… N" / drain UI and
        backpressure) as a folder scan, since there is nothing left to walk.
        The sync plan's entry in _compares was already popped by start_sync()
        before this thread started, so the token argument only identifies
        the job for logging; deregistering it here is a harmless no-op.
        Always deregisters the scan registry entry."""
        found = 0
        batch = []
        try:
            def flush():
                nonlocal batch, found
                if not batch:
                    return
                self._scan_wait_for_room(stop_event)
                if stop_event.is_set() or not self.connected:
                    return
                found += len(batch)
                self._bump_scan_found(scan_id, len(batch))
                self._enqueue_files(batch, direction, on_conflict)
                batch = []

            batch_cap = min(64, constants.SCAN_QUEUE_HIGH_WATER) or 64
            for lp, rp, size, is_dir in transfers:
                if stop_event.is_set() or not self.connected:
                    break
                batch.append((lp, rp, size, 0, is_dir))
                if len(batch) >= batch_cap:
                    flush()
                    if stop_event.is_set() or not self.connected:
                        break
            flush()
            if stop_event.is_set():
                self._worker_log(f"Sync stopped ({found} item(s) queued before stopping)", "warn")
            elif not self.connected:
                self._worker_log(f"Sync halted: disconnected ({found} item(s) queued)", "warn")
            elif found:
                self._worker_log(f"Sync: {found} item(s) queued")
            else:
                self._worker_log("Sync: nothing to transfer")
        except Exception as e:
            self._worker_log(f"sync failed: {friendly_error(e)}", "error")
            debug.log("SYNC failed", traceback.format_exc())
        finally:
            self._deregister_scan(scan_id)
            self._deregister_compare(token)

    def start_sync(self, token, on_conflict="overwrite"):
        """Streams the transfer list a prior sync_plan() computed (identified
        by token, the id poll_queue() handed back in the summary) onto the
        transfer queue. The transfer list itself never round-trips through
        the page: it stays server-side from computation to enqueue.

        A token works exactly once: the plan is popped out of _compares here,
        before the streaming thread even starts, so a second call with the
        same token (a double-click, or the page re-sending it) always gets
        the "no longer available" error instead of re-queuing the same
        files. If not connected or another legacy sync/watch is running, the
        plan is left untouched: the caller can retry the same token."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._legacy_active.is_set():
            return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
        with self._compare_lock:
            entry = self._compares.get(token)
            if entry is not None and entry.get("transfers") is not None:
                self._compares.pop(token, None)
            else:
                entry = None
        if entry is None:
            return {"ok": False, "error": "That sync plan is no longer available. Run Sync again."}
        transfers = entry["transfers"]
        direction = entry["direction"]
        scan_id, stop_event = self._register_scan()
        t = threading.Thread(target=self._stream_sync_transfers,
                              args=(transfers, direction, on_conflict, scan_id, stop_event, token),
                              daemon=True)
        t.start()
        return {"ok": True, "scanning": True}

    def discard_sync(self, token):
        """Bridge method for the page to free a sync plan it decided not to
        use: the user declined the confirmation, or start_sync() refused it.
        Pops the entry only if it is a finished sync plan (done computing,
        never a still-running one, which a stop can't safely interrupt from
        here); a still-running job is left alone. Idempotent and always
        returns ok, so a stale or unknown token (already consumed, already
        discarded, or from a job that failed and was already dropped) is
        harmless to pass."""
        with self._compare_lock:
            entry = self._compares.get(token)
            if entry is not None and entry["kind"] == "sync" and entry["done"]:
                self._compares.pop(token, None)
        return {"ok": True}

    @_browsing
    def calc_remote_size(self, remote_dir, name):
        return services.browsing.calc_remote_size(self, remote_dir, name)

    # ───────────── keygen / install key ─────────────
    def default_key_path(self, key_type):
        return services.keys.default_key_path(self, key_type)

    def browse_save_key(self, suggested):
        return services.keys.browse_save_key(self, suggested)

    def browse_folder(self):
        return services.browsing.browse_folder(self)

    def generate_key(self, key_type, out_path, passphrase, overwrite=False):
        return services.keys.generate_key(self, key_type, out_path, passphrase, overwrite)

    @_browsing
    def install_pubkey(self, pubtext):
        return services.keys.install_pubkey(self, pubtext)

    # ───────────── upload watcher ─────────────
    def start_watch(self, local_dir, remote_dir):
        """Polls local_dir every 2 seconds and uploads files that changed since
        it started. Uploads run on the shared browsing session, serialized with
        listing and health checks by self._sftp_lock. _legacy_active is set only
        while an upload is actually running (not for the whole watch session), so
        a pane transfer can still be queued while watch idles between polls.

        A file is uploaded only once its size and modification time have held
        steady across one poll, so a file still being written is not sent as a
        partial snapshot. A failed upload keeps its change pending and is retried
        on a later poll instead of being forgotten.

        Each upload uses the same scratch-file-then-atomic-swap publish as the
        transfer queue: the new copy is written to a hidden temp file next to
        the destination and only swapped in once it is proven complete, so an
        interrupted upload can never leave a half-written file in place of a
        working one, and the remote file's modification time is stamped from
        the local source on success. A server that cannot do that atomic swap
        has the upload refused rather than risking a partial file. The watch
        snapshot skips this app's own scratch files, so a transfer in progress
        is never mistaken for a new local change.

        A stop only clears the watcher's shared state once its thread has
        actually retired; if it is still finishing up after a few seconds, a
        following start_watch is refused rather than risking two watch threads
        running over the same folder at once."""
        self.stop_watch()
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        with self._watch_lock:
            prev = self._watch_thread
        if prev is not None and prev.is_alive():
            return {"ok": False, "error": "Still stopping the previous watch. Try again in a moment."}
        if prev is not None:
            with self._watch_lock:
                if self._watch_thread is prev:
                    self._watch_stop = None
                    self._watch_thread = None
        if self.queue.pending() > 0:
            return {"ok": False, "error": "A transfer queue is active. Wait for it to finish."}
        # This run's own stop Event, captured by loop() below. Never read the
        # shared self._watch_stop from inside the thread: a stop or a restart can
        # replace it, and this local reference stays valid for this run alone.
        stop = threading.Event()

        def snapshot():
            snap = {}
            for root, _d, files in os.walk(local_dir):
                for fn in files:
                    if is_temp_part(fn):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        st = os.stat(fp)
                        snap[fp] = (st.st_mtime, st.st_size)
                    except Exception:
                        pass
            return snap

        def loop():
            # last: files whose current state has been accepted (uploaded, or
            # present unchanged since start) and must not be re-sent.
            # seen_changed: a changed file's state at its previous sighting, used
            # to require one stable poll before uploading.
            last = snapshot()
            seen_changed = {}
            while not stop.is_set():
                time.sleep(self._watch_interval)
                if stop.is_set():
                    break
                cur = snapshot()
                changed = [fp for fp in cur if last.get(fp) != cur[fp]]
                # A queue worker uses its own session, but this shared-session
                # upload path is deliberately kept clear of an active batch.
                # Leave last and seen_changed untouched so the change is retried
                # on a later idle poll.
                if changed and self.queue.pending() > 0:
                    continue
                refreshed_folders = set()
                for fp in changed:
                    if stop.is_set():
                        break
                    # Stability gate: upload only after the file looks the same
                    # as the previous poll, so a file mid-write waits.
                    if seen_changed.get(fp) != cur[fp]:
                        seen_changed[fp] = cur[fp]
                        continue
                    rel = os.path.relpath(fp, local_dir).replace("\\", "/")
                    rp = posixpath.join(remote_dir, rel)
                    self._legacy_active.set()
                    try:
                        rdir = posixpath.dirname(rp)

                        def _cb(done_b, _t):  # watch has no progress bar; swallow progress callbacks
                            pass

                        # Serialize against the browsing session: this upload runs
                        # on the watcher thread and shares self.sftp with listing
                        # and health-ping bridge calls (see _browsing).
                        finished = False
                        with self._sftp_lock:
                            if stop.is_set():
                                seen_changed.pop(fp, None)
                                break
                            self._ensure_remote_dir(rdir)
                            finished = self._put_resume(self.sftp, fp, rp, 0, _cb, stop.is_set)
                        if finished:
                            self._worker_log(f"Watch: uploaded {rel}", "ok")
                            refreshed_folders.add(posixpath.dirname(rp))
                            # Accept this state so it is not re-sent.
                            last[fp] = cur[fp]
                            seen_changed.pop(fp, None)
                        else:
                            # Stop fired mid-upload: leave last unadvanced so a
                            # later poll retries, and stop this batch.
                            seen_changed.pop(fp, None)
                            break
                    except Exception as e:
                        self._worker_log(f"Watch error: {rel} - {friendly_error(e)}", "error")
                        # Keep the change pending: leave last unadvanced so a
                        # later poll retries. Reset the stability gate so a
                        # transient failure does not skip the next steadiness
                        # check.
                        seen_changed.pop(fp, None)
                    finally:
                        self._legacy_active.clear()
                if refreshed_folders:
                    with self._watch_refresh_lock:
                        if not stop.is_set():
                            self._watch_refresh.update(refreshed_folders)
                # Forget entries for files that no longer exist so the two maps
                # do not grow without bound over a long session.
                for tracked in (last, seen_changed):
                    for gone in [k for k in tracked if k not in cur]:
                        del tracked[gone]

        t = threading.Thread(target=loop, daemon=True)
        with self._watch_lock:
            self._watch_stop = stop
            self._watch_thread = t
        t.start()
        debug.log("WATCH start", {"local": local_dir, "remote": remote_dir})
        return {"ok": True}

    def _make_dir(self, direction, local_path, remote_path, sftp, dir_cache=None):
        return services.transfers._make_dir(self, direction, local_path, remote_path, sftp, dir_cache)

    def _ensure_remote_dir(self, path, sftp=None):
        return services.transfers._ensure_remote_dir(self, path, sftp)

    def stop_watch(self):
        """Signal the current watcher to stop and wait (bounded) for its thread
        to retire before clearing the shared state, so a following start_watch
        never overlaps the old thread. If the thread has not retired within the
        wait, the shared state is left as-is (recording the straggler) instead
        of being cleared, so start_watch can tell one is still running and
        refuse to launch a second thread over the same folder. Called by
        start_watch (for a restart), Disconnect, and shutdown; always returns
        ok True so those callers proceed regardless."""
        with self._watch_lock:
            stop = self._watch_stop
            thread = self._watch_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)
            if thread.is_alive():
                debug.log("WATCH: thread still running 3s after stop")
                return {"ok": True}
        with self._watch_lock:
            if self._watch_thread is thread:
                self._watch_stop = None
                self._watch_thread = None
        return {"ok": True}

    # ───────────── update check ─────────────
    def check_update(self):
        return services.updates.check_update(self)

    def _is_newer(self, latest, current):
        return services.updates._is_newer(self, latest, current)

    def open_url(self, url):
        return services.updates.open_url(self, url)
