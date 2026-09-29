"""The bridge object the page calls."""

import os
import stat
import errno
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
from app.formatting import human_size
from app.paths import is_temp_part, local_link_target, local_temp_path, remote_temp_path, safe_local_child
from app.transfer_queue import TransferQueue
from app.workers import worker_target


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
        """Single source of truth for 'a transfer batch is running', used by
        both the window-close veto and the page's own Disconnect confirm
        (see transfers_active()). True when the queue has waiting or active
        items, a background scan is still walking a folder, or any worker
        thread is alive."""
        if self.queue.pending() > 0:
            return True
        if self._scan_active():
            return True
        with self._worker_lock:
            return any(w.is_alive() for w in self._workers)

    def transfers_active(self):
        """JS-callable wrapper for _transfers_active(): pywebview never
        exposes underscore-prefixed methods to the page, so this is what
        onConn()'s Disconnect confirm actually calls."""
        return self._transfers_active()

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
        """Cancel-all, wired to the footer Cancel button: cancels every waiting
        queue item and flags every active one so each worker's byte loop stops
        and finalizes it as cancelled. Also stops any background scan still
        streaming files in, so a cancel during a huge scan halts it promptly.
        self._cancel is also set to stop a running folder-size calculation,
        which is not on the per-item flag."""
        self._stop_all_scans()
        self._stop_all_compares()
        self.queue.cancel_all()
        self._cancel.set()
        return {"ok": True}

    def poll_queue(self):
        """Pulled by the window on a timer (~200ms) while a queue is active.
        This is the only channel from worker threads and the watcher to the UI:
        it never calls evaluate_js, so it cannot deadlock the window."""
        with self._console_lock:
            lines = self._console_buffer
            self._console_buffer = []
        with self._watch_refresh_lock:
            watch_refresh = sorted(self._watch_refresh)
            self._watch_refresh.clear()
        with self._watch_lock:
            watching = (self._watch_thread is not None
                        and self._watch_thread.is_alive()
                        and self._watch_stop is not None
                        and not self._watch_stop.is_set())
        items, pending = self.queue.snapshot_and_pending()
        active_ids = [it["id"] for it in items if it["state"] == "active"]
        with self._progress_lock:
            progress = {str(k): v for k, v in self._progress_by_id.items()}
        # Deliver at most one finished compare/sync payload per poll, so the
        # page never has to reconcile two at once. One-shot for a plain
        # compare (deregistered right after delivery), and for a failed sync
        # (nothing left to keep) or a successful sync plan with nothing to
        # transfer and no conflicts (a no-op, same as a plain compare). A
        # successful sync plan with something to transfer or a conflict
        # stays registered so its stashed transfer list survives for
        # start_sync(); that entry's lifetime then belongs to
        # start_sync()/discard_sync()/the sync_plan() safety net, not here.
        compare_done = None
        deregister_id = None
        with self._compare_lock:
            for cid, entry in self._compares.items():
                if entry["done"] and not entry["delivered"]:
                    entry["delivered"] = True
                    compare_done = {"id": cid, "kind": entry["kind"], "ok": entry["ok"],
                                     "error": entry["error"], "result": entry["result"]}
                    if entry["kind"] == "compare" or not entry["ok"]:
                        deregister_id = cid
                    elif entry["kind"] == "sync":
                        result = entry["result"] or {}
                        if not result.get("count") and not result.get("conflicts"):
                            deregister_id = cid
                    break
        if deregister_id is not None:
            self._deregister_compare(deregister_id)
        return {
            "items": items,
            "pending": pending,
            "active_ids": active_ids,
            "progress": progress,
            "console": lines,
            "watch_refresh": watch_refresh,
            "watching": watching,
            "paused": self.queue.is_paused(),
            # A background scan (enqueue/upload_paths) queues files as it
            # finds them, so the UI needs its own signal to show "Scanning…
            # N found" and read accurate totals even after old items have
            # aged out of items/pending above.
            "scanning": self._scan_active(),
            "scan_found": self._scan_found_total(),
            # A background compare/sync job walks the tree the same way, on
            # its own separate registry so its progress and the scan
            # progress above never collide.
            "comparing": self._compare_active(),
            "compare_found": self._compare_found_total(),
            "compare_done": compare_done,
            "counts": self.queue.counts(),
            "debug_warnings": self._drain_debug_warnings(),
            "debug_enabled": debug.is_enabled(),
        }

    def cancel_item(self, item_id):
        """Cancel a single queued item. Waiting items go straight to cancelled;
        an active item is flagged (TransferItem.cancel_requested) so whichever
        worker owns it interrupts its byte loop and finalizes it."""
        self.queue.cancel(item_id)
        return {"ok": True}

    def clear_finished(self):
        """Remove completed/failed/cancelled/skipped items so the window can
        re-render the queue without the clutter of finished transfers."""
        self.queue.clear_finished()
        items, pending = self.queue.snapshot_and_pending()
        return {"items": items, "pending": pending}

    def retry_item(self, item_id):
        """One-click retry: put a failed or cancelled queue item back in line and
        wake the worker pool. Wired to the ↻ control on failed/cancelled rows."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if not self.queue.requeue(item_id):
            return {"ok": False, "error": "That item can't be retried."}
        self._ensure_worker()
        return {"ok": True}

    def retry_all_failed(self):
        """Put every FAILED item back in line and wake the worker pool.
        Wired to a footer "retry all failed" control."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        n = self.queue.retry_all_failed()
        self._ensure_worker()
        return {"ok": True, "requeued": n}

    def pause_queue(self):
        """Pause the queue: stop claiming new items. Files already mid-transfer
        are left to finish; the worker pool winds down once they do. Wired to the
        footer Pause control."""
        self.queue.pause()
        self._worker_log("Pausing queue…")
        return {"ok": True}

    def resume_queue(self):
        """Resume a paused queue and wake the worker pool to drain the waiting
        items in order."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        self.queue.resume()
        self._worker_log("Resuming queue")
        self._ensure_worker()
        return {"ok": True}

    def enqueue(self, jobs, direction, local_dir, remote_dir, on_conflict="overwrite"):
        """New entry point for pane transfers: starts a background scan that
        streams jobs (a list of {name, is_dir}) into per-file queue
        items and returns immediately, instead of walking the whole tree up
        front. On a huge folder that walk used to block with zero feedback
        and create empty folder shells; now files are queued (and their
        remote/local parent folders created) as they are found, one at a
        time, with backpressure so the queue never balloons ahead of what the
        worker pool can drain. See _scan_and_queue and poll_queue's
        "scanning"/"scan_found" keys for how the UI observes progress."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._legacy_active.is_set():
            return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
        roots = []   # (local_path, remote_path, is_dir)
        for j in jobs:
            name = j["name"]
            is_dir = bool(j.get("is_dir"))
            if direction == "upload":
                lp = os.path.join(local_dir, name)
                rp = posixpath.join(remote_dir, name)
            else:
                rp = posixpath.join(remote_dir, name)
                try:
                    lp = safe_local_child(local_dir, name, local_dir)
                except ValueError as e:
                    self._worker_log(f"skipped unsafe remote name {name!r}: {e}", "error")
                    continue
                target = local_link_target(lp) if is_dir else None
                if target:
                    self._worker_log(f"{name} is symlinked to {target}; files will be written there.", "warn")
            roots.append((lp, rp, is_dir))
        scan_id, stop_event = self._register_scan()
        t = threading.Thread(target=self._scan_and_queue,
                              args=(roots, direction, on_conflict, scan_id, stop_event, local_dir),
                              daemon=True)
        t.start()
        return {"ok": True, "scanning": True}

    def _enqueue_files(self, files, direction, on_conflict):
        """Append (local_path, remote_path, size, mtime, is_dir) tuples to the
        queue as per-item jobs and wake the worker pool. Returns the count
        enqueued. Shared by pane transfers (enqueue) and external drops
        (upload_paths). Sizes are carried from enumeration (real local size
        for uploads, real remote size for downloads), never re-stat here.
        mtime is part of the enumeration tuple but unused here: timestamp
        preservation re-reads the live source at publish time, not this
        scan-time value. is_dir marks a "make folder" job (always size 0)
        rather than a file transfer; see _worker_loop/_make_dir.

        Also decides the worker-pool size for this batch (worker_target) and
        raises the pool's target under _worker_lock: only ever up, never torn
        down under a still-draining pool, so a mid-batch top-up never shrinks
        workers already running."""
        sizes = []
        for lp, rp, size, _mtime, is_dir in files:
            name = os.path.basename(lp)
            sizes.append(size)
            queue_size = size if size and size > 0 else 0
            self.queue.append(direction, lp, rp, name, size=queue_size, on_conflict=on_conflict,
                               is_dir=is_dir)
        batch_target = worker_target(sizes)
        with self._worker_lock:
            if not self._workers:
                self._target_workers = batch_target
            else:
                self._target_workers = max(self._target_workers, batch_target)
        self._ensure_worker()
        return len(files)

    def _ensure_worker(self):
        """Top the worker pool up to self._target_workers live threads
        whenever there is waiting work. Workers are started here under the
        same lock a worker retires itself with, so starting and stopping
        workers can never overlap and leave the pool in the wrong state."""
        with self._worker_lock:
            if self.queue.is_paused():
                return
            self._workers = [w for w in self._workers if w.is_alive()]
            while len(self._workers) < self._target_workers and self.queue.waiting() > 0:
                w = threading.Thread(target=self._worker_loop, daemon=True)
                self._workers.append(w)
                w.start()

    def _worker_loop(self):
        """Drains the queue, one item at a time, as one of up to
        self._target_workers (WORKER_COUNT by default, up to WORKER_COUNT_MAX
        for a batch of many small files) workers running concurrently. Each
        worker opens its own SFTP session
        here and closes it on exit; workers never touch self.sftp, that stays
        reserved for the file browser. done_count/total are only for the
        progress display, an approximation is fine there (each worker only
        knows its own done_count).

        Runs entirely off the pywebview bridge thread, so it must never call
        evaluate_js (that is what deadlocked the window). _progress() only
        updates in-memory state and _worker_log() buffers console lines, both
        for the window to pull via poll_queue()."""
        me = threading.current_thread()
        try:
            if not self.connected or self.client is None:
                # Disconnected before this worker could start. Give a plain
                # reason instead of leaking a raw "'NoneType' has no attribute
                # open_sftp" from the line below.
                raise ConnectionError("disconnected before the transfer started")
            sftp = self.client.open_sftp()
        except Exception as e:
            reason = str(e).strip() or e.__class__.__name__
            # No silent failure: surface it in the console log and let this
            # worker retire; the other worker (if any) keeps draining the queue.
            self._worker_log(f"could not open a transfer session: {reason}", "error")
            with self._worker_lock:
                if me in self._workers:
                    self._workers.remove(me)
                if not self._workers:
                    self._target_workers = WORKER_COUNT
                    with self._progress_lock:
                        self._progress_by_id = {}
                    # Last worker out and none could open a session: don't leave
                    # queued items sitting as WAITING with nothing to drain them.
                    # Mark them failed so the failure is visible in the queue.
                    stranded = self.queue.fail_waiting(f"transfer session unavailable: {reason}")
                    if stranded:
                        self._worker_log(
                            f"{stranded} queued item(s) marked failed: no transfer session",
                            "error")
            return
        # Remote directories this worker has already confirmed exist, so an
        # upload only pays the stat/mkdir round trip once per directory, not
        # once per file (see _one's dir_cache parameter).
        dir_cache = set()
        try:
            done_count = 0
            while True:
                item = self.queue.claim()
                if item is None:
                    # Decide whether to stop under the same lock _ensure_worker
                    # starts workers with, and re-check the queue while holding
                    # it. Without this, a file enqueued in the instant this
                    # worker finds the queue empty would see a still-alive
                    # worker (this one) and _ensure_worker would skip starting
                    # one, yet this worker has already left the loop, and the
                    # file would wait forever. waiting() (not pending()) is the
                    # right check: pending() also counts items ACTIVE on the
                    # other worker, which would wrongly keep this one alive.
                    # Paused also retires here: claim() returns None while
                    # paused even with items still WAITING, and without this
                    # the worker would just busy-loop on those items instead
                    # of winding down.
                    with self._worker_lock:
                        paused = self.queue.is_paused()
                        if self.queue.waiting() == 0 or paused:
                            if me in self._workers:
                                self._workers.remove(me)
                            # Only the last worker to leave clears pool-wide
                            # state and logs the drain summary; the others just
                            # retire quietly.
                            if not self._workers:
                                with self._progress_lock:
                                    self._progress_by_id = {}
                                if self.queue.is_paused() and self.queue.waiting() > 0:
                                    # Paused with items still held: leave the target
                                    # alone so a resume drains the rest at the count
                                    # this batch was scaled to, not the default.
                                    self._worker_log(
                                        f"Queue paused ({self.queue.waiting()} still waiting)")
                                else:
                                    # Reached the empty queue (paused with nothing
                                    # left, or a normal drain). Reset the pool target
                                    # so a later lone retry does not inherit a stale
                                    # scaled-up count, and clear a stale pause flag so
                                    # the next batch is not held back by a pause that
                                    # belonged to a queue now emptied.
                                    self._target_workers = WORKER_COUNT
                                    self.queue.resume()
                                    counts = self.queue.counts()
                                    self._worker_log(
                                        f"Queue drained: {counts.get('completed', 0)} completed, "
                                        f"{counts.get('failed', 0)} failed, {counts.get('cancelled', 0)} cancelled, "
                                        f"{counts.get('skipped', 0)} skipped")
                            break
                    continue
                total = done_count + self.queue.pending()
                if item.cancel_requested:
                    # cancel() on a waiting item goes straight to CANCELLED, so this
                    # should not happen in practice, but guard anyway.
                    self.queue.mark_cancelled(item.id)
                    self._worker_log(f"cancelled {item.name}", "warn")
                    done_count += 1
                    continue
                arrow = "↑" if item.direction == "upload" else "↓"
                ok = False
                res = None
                last_err = ""
                for attempt in range(3):
                    if item.cancel_requested:
                        break
                    try:
                        if item.is_dir:
                            # A "make folder" job: no bytes to move, so there
                            # is no progress callback and nothing to skip.
                            self._make_dir(item.direction, item.local_path,
                                            item.remote_path, sftp, dir_cache)
                            res = "ok"
                        else:
                            res = self._one(item.direction, item.local_path, item.remote_path,
                                             item.name, done_count, total, item.on_conflict,
                                             sftp, cancel_check=lambda it=item: it.cancel_requested,
                                             progress_key=item.id, dir_cache=dir_cache)
                        ok = True
                        break  # success (including "skip"/"cancelled"): stop retrying
                    except Exception as e:
                        last_err = str(e)
                        debug.log(f"transfer retry {attempt+1}", f"{item.name}: {e}")
                        if item.cancel_requested:
                            break
                        if self._connection_dead(sftp):
                            # The session itself is gone, not just this one
                            # file: retrying it (or any remaining item) would
                            # only grind through the same failure. Stop the
                            # whole batch once instead of one error per file.
                            last_err = "Connection lost"
                            self._report_dead_connection()
                            break
                        time.sleep(0.6)
                if res == "cancelled" or (not ok and item.cancel_requested):
                    # res == "cancelled": the byte loop itself broke early, so
                    # the transfer did not finish; that is the one true source
                    # of truth here, not a flag re-check after the fact, which
                    # could mislabel a file that finished sending its very last
                    # byte the instant cancel arrived.
                    self.queue.mark_cancelled(item.id)
                    self._worker_log(f"cancelled {item.name}", "warn")
                elif res == "skip":
                    self.queue.mark_skipped(item.id)
                    self._worker_log(f"skip {item.name} (already up to date)")
                elif ok:
                    self.queue.mark_completed(item.id)
                    if item.is_dir:
                        if item.direction == "upload":
                            self._worker_log(f"{arrow} folder {item.remote_path}", "ok")
                        else:
                            self._worker_log(f"created folder {item.local_path}", "ok")
                    else:
                        self._worker_log(f"{arrow} {(item.remote_path if item.direction == 'upload' else item.name)}", "ok")
                else:
                    self.queue.mark_failed(item.id, last_err or "transfer failed")
                    self._worker_log(f"failed {item.name}", "error")
                done_count += 1
                with self._progress_lock:
                    self._progress_by_id.pop(item.id, None)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            # Safety net for an exit by exception rather than the clean
            # empty-queue path above: remove this worker if it is still in the
            # pool, and only clear pool-wide state if it was the last one.
            with self._worker_lock:
                if me in self._workers:
                    self._workers.remove(me)
                if not self._workers:
                    # Leave the target alone during a pause-hold so a resume drains
                    # the rest at the scaled count; any real drain resets it.
                    if not (self.queue.is_paused() and self.queue.waiting() > 0):
                        self._target_workers = WORKER_COUNT
                    with self._progress_lock:
                        self._progress_by_id = {}

    def upload_paths(self, paths, remote_dir, on_conflict="overwrite"):
        """Upload absolute local paths (files or folders) dragged in from outside
        the app, into remote_dir. Only the top-level dropped items are stat'd
        here (cheap: one isdir check each); the actual folder contents stream
        in via the same background scan as enqueue(), so a huge dropped folder
        does not block this call."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._legacy_active.is_set():
            return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
        roots = []   # (local_path, remote_path, is_dir)
        try:
            for raw in paths or []:
                lp = self._normalize_drop_path(raw)
                if not lp:
                    continue
                name = os.path.basename(lp.rstrip("\\/"))
                rp = posixpath.join(remote_dir, name)
                if os.path.isdir(lp):
                    roots.append((lp, rp, True))
                elif os.path.isfile(lp):
                    roots.append((lp, rp, False))
        except Exception as e:
            return {"ok": False, "error": f"Could not read the dropped items: {e}"}
        if not roots:
            return {"ok": False, "error": "No files were found in the dropped items."}
        debug.log("EXTERNAL UPLOAD", {"items": len(roots), "remote": remote_dir})
        scan_id, stop_event = self._register_scan()
        t = threading.Thread(target=self._scan_and_queue,
                              args=(roots, "upload", on_conflict, scan_id, stop_event),
                              daemon=True)
        t.start()
        return {"ok": True, "scanning": True}

    @staticmethod
    def _normalize_drop_path(p):
        """pywebview yields file-URL style paths (e.g. '/C:/Users/..') on Windows."""
        p = (p or "").strip()
        if os.name == "nt":
            if len(p) >= 3 and p[0] == "/" and p[2] == ":":
                p = p[1:]
            p = p.replace("/", "\\")
        return p

    def on_external_drop(self, event):
        """pywebview Qt drop handler: hand the real file paths back to the UI,
        which uploads them into the currently open remote folder."""
        try:
            files = ((event or {}).get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            debug.log("EXTERNAL DROP", {"paths": paths})
            if paths:
                self._emit("external_drop", {"paths": paths})
        except Exception as e:
            debug.log("external drop handler failed", str(e))

    def _one(self, direction, lp, rp, name, idx, total, on_conflict, sftp,
             cancel_check, progress_key=None, dir_cache=None):
        """sftp is the worker's own session to transfer over. cancel_check is
        a callable returning True to stop the byte loop early. progress_key is
        what _progress() files this transfer's progress under (the queue item
        id; defaults to the file name). dir_cache, if given,
        is a set of remote directories this worker has already confirmed
        exist, owned by the caller: since folders are no longer created up
        front by a scan, an upload must ensure its remote parent directory
        exists lazily, and the cache avoids a stat/mkdir round trip for every
        single file once a worker has already ensured that directory."""
        if progress_key is None:
            progress_key = name
        # size- and time-aware: skip identical when asked, otherwise resend
        # the whole file
        if direction == "upload":
            src_stat = os.stat(lp)
            src_size, src_mtime = src_stat.st_size, int(src_stat.st_mtime)
            dst_size, dst_mtime = self._rstat(sftp, rp)
        else:
            src_size, src_mtime = self._rstat(sftp, rp)
            if os.path.exists(lp):
                dst_stat = os.stat(lp)
                dst_size, dst_mtime = dst_stat.st_size, int(dst_stat.st_mtime)
            else:
                dst_size, dst_mtime = -1, 0
        sizes_match = src_size >= 0 and dst_size == src_size
        if on_conflict == "skip" and sizes_match and (
                abs(dst_mtime - src_mtime) <= MTIME_TOL
                or self._mtime_fallback_matches(lp, rp, src_size)):
            # user chose skip and the other side matches on size, and either
            # the modification time also matches (within tolerance) or this
            # connection remembers this pair as having failed its time stamp
            # at transfer time with the same size (see _mtime_fallback_matches)
            # -> leave it. overwrite deliberately falls through and resends,
            # even when size and time match, since neither alone proves the
            # contents match.
            self._progress(name, idx, total, src_size, src_size, 0, progress_key)
            return "skip"
        # Always rewrite from the start. A smaller destination is not proof of
        # an interrupted transfer worth resuming: it is just as likely an older,
        # different file. Appending onto it would splice the old head onto the
        # new tail, and because that mangled file often ends up the same size as
        # the source, a later compare would read it as "same" and never fix it.
        # Sending fresh every time is the only safe rule size alone supports.
        offset = 0
        start = time.time()

        def cb(done_b, _t, base=offset):
            self._progress(name, idx, total, base + done_b, src_size, time.time() - start, progress_key)

        os.makedirs(os.path.dirname(lp), exist_ok=True) if direction == "download" else None
        if direction == "upload":
            rdir = posixpath.dirname(rp)
            if rdir and rdir != "/" and (dir_cache is None or rdir not in dir_cache):
                self._ensure_remote_dir(rdir, sftp)
                if dir_cache is not None:
                    dir_cache.add(rdir)
            finished = self._put_resume(sftp, lp, rp, offset, cb, cancel_check)
        else:
            finished = self._get_resume(sftp, rp, lp, offset, cb, cancel_check)
        # finished is False only if the byte loop broke early on cancel_check;
        # a transfer that sent every byte is "ok" even if cancel arrived a
        # moment later, so the caller must not re-check a cancel flag itself.
        return "ok" if finished else "cancelled"

    def _put_resume(self, sftp, lp, rp, offset, cb, cancel_check):
        # Writes into a scratch file next to the real remote destination
        # (never rp itself), so the destination is only touched once the new
        # copy is proven complete. On success the scratch file is confirmed
        # to be the right size. If this upload is overwriting an existing
        # file, the scratch file is given that file's standard permission
        # bits before the swap, so the published file keeps the same
        # permissions instead of picking up the server's default for a new
        # file. A brand new destination (nothing to overwrite) just keeps
        # the server default. If the existing file's permissions cannot be
        # read, or the server refuses to set them on the scratch file, the
        # upload is refused rather than publishing a file with the wrong
        # permissions; the existing file is left in place. Once the
        # permissions are settled the scratch file is swapped in with
        # posix_rename, which is atomic: a cancel, dropped connection, or
        # exhausted retry can only ever leave the scratch file behind, never
        # a half-written rp.
        #
        # Check cancel_check() only once there is another chunk actually to
        # send, and only after confirming there is more file left (the read
        # came back non-empty). That way a cancel arriving in the instant
        # right after the last real chunk was already written finds nothing
        # left to abort: the next read hits EOF first and the loop exits with
        # finished=True, so a fully-sent file is never mislabeled cancelled.
        temp = remote_temp_path(rp)
        finished = True
        published = False
        try:
            with open(lp, "rb") as src:
                src.seek(offset)
                with sftp.open(temp, "w") as dst:
                    dst.set_pipelined(True)
                    sent = 0
                    while True:
                        chunk = src.read(32768)
                        if not chunk:
                            break
                        if cancel_check():
                            finished = False
                            break
                        dst.write(chunk)
                        sent += len(chunk)
                        cb(sent, 0)
            if finished:
                src_size = os.path.getsize(lp)
                temp_size = sftp.stat(temp).st_size
                if temp_size != src_size:
                    raise IOError(
                        f"upload incomplete: wrote {temp_size} of {src_size} bytes")
                try:
                    existing_attr = sftp.stat(rp)
                except Exception as e:
                    if getattr(e, "errno", None) == errno.ENOENT:
                        existing_attr = None
                    else:
                        raise IOError(
                            "could not read the remote file's current "
                            f"permissions ({e}); existing file left in "
                            "place to protect its permissions") from e
                if existing_attr is not None:
                    if existing_attr.st_mode is None:
                        raise IOError(
                            "could not read the remote file's current "
                            "permissions (the server did not report them); "
                            "existing file left in place to protect its "
                            "permissions")
                    mode = stat.S_IMODE(existing_attr.st_mode) & 0o777
                    try:
                        sftp.chmod(temp, mode)
                    except Exception as e:
                        raise IOError(
                            "server refused to set the target's permissions "
                            "on the new file; upload refused to protect the "
                            "existing copy") from e
                try:
                    sftp.posix_rename(temp, rp)
                except Exception as e:
                    raise IOError(
                        "server does not support safe atomic replace; "
                        "file not written to protect the existing copy") from e
                published = True
                self._apply_upload_mtime(sftp, rp, lp)
            return finished
        finally:
            # Anything other than a proven, published swap leaves nothing
            # behind: delete the scratch file (best effort) and, on an
            # exception, let it propagate so the retry loop tries again with
            # a brand new scratch file.
            if not published:
                try:
                    sftp.remove(temp)
                except Exception:
                    pass

    def _get_resume(self, sftp, rp, lp, offset, cb, cancel_check):
        # Same idea as _put_resume: stream into a local scratch file next to
        # the real destination, confirm its size once the loop finishes, then
        # publish with os.replace, which is atomic on the same drive. A
        # cancel, dropped connection, or exhausted retry only ever leaves the
        # scratch file behind; the real destination is never opened for
        # writing until the new copy is proven complete.
        #
        # Same ordering as _put_resume, for the same reason: only treat a
        # cancel as having interrupted the transfer if there was still more
        # to read when it was observed.
        temp = local_temp_path(lp)
        finished = True
        published = False
        try:
            with sftp.open(rp, "r") as src:
                src.prefetch()
                src.seek(offset)
                with open(temp, "wb") as dst:
                    got = 0
                    while True:
                        chunk = src.read(32768)
                        if not chunk:
                            break
                        if cancel_check():
                            finished = False
                            break
                        dst.write(chunk)
                        got += len(chunk)
                        cb(got, 0)
            if finished:
                # Confirm the remote size directly rather than through _rstat,
                # which hides a failed lookup as -1. A -1 there would skip the
                # size check entirely and publish an unverified download. Any
                # failure to read the size (source gone, permission denied,
                # connection dropped) must raise so the retry loop retries and,
                # if it keeps failing, leaves the existing local file untouched.
                # A genuine zero-byte file still stats fine and publishes.
                try:
                    src_stat = sftp.stat(rp)
                except Exception as e:
                    raise IOError(
                        f"download not verified: could not read the remote "
                        f"file size ({e}); existing file left in place") from e
                src_size = src_stat.st_size
                src_mtime = int(src_stat.st_mtime or 0)
                temp_size = os.path.getsize(temp)
                if temp_size != src_size:
                    raise IOError(
                        f"download incomplete: wrote {temp_size} of {src_size} bytes")
                os.replace(temp, lp)
                published = True
                self._apply_download_mtime(lp, rp, src_mtime, src_size)
            return finished
        finally:
            if not published:
                try:
                    os.remove(temp)
                except Exception:
                    pass

    def _progress(self, name, idx, total, sent, size, elapsed, progress_key=None):
        speed = (sent / elapsed) if elapsed > 0 else 0
        eta = ((size - sent) / speed) if speed > 0 and size > 0 else 0
        payload = {"name": name, "index": idx, "total": total,
                   "pct": int(sent * 100 / size) if size else 100,
                   "speed": human_size(speed) + "/s" if speed else "",
                   "eta": int(eta)}
        # Called from worker threads: keyed by item id and kept in memory
        # only, since the window pulls it through poll_queue(). Never emit
        # from here (evaluate_js off the bridge thread deadlocks the window).
        with self._progress_lock:
            self._progress_by_id[progress_key] = payload

    def _rstat(self, sftp, rp):
        """Remote file size and modification time. -1 size / 0 mtime on
        failure."""
        try:
            a = sftp.stat(rp)
            return a.st_size, int(a.st_mtime or 0)
        except Exception:
            return -1, 0

    def _mtime_fallback_key(self, lp, rp):
        """Build the one normalized key every record/lookup/clear of
        self._mtime_fallback must use, so the two sides never drift apart and
        silently stop matching. Local paths are normalized for case and made
        absolute; remote paths are normalized as posix paths."""
        return (os.path.normcase(os.path.abspath(lp)), posixpath.normpath(rp))

    def _record_mtime_fallback(self, lp, rp, size):
        key = self._mtime_fallback_key(lp, rp)
        with self._mtime_fallback_lock:
            self._mtime_fallback[key] = size

    def _clear_mtime_fallback(self, lp, rp):
        key = self._mtime_fallback_key(lp, rp)
        with self._mtime_fallback_lock:
            self._mtime_fallback.pop(key, None)

    def _mtime_fallback_matches(self, lp, rp, size):
        """True only if this (local, remote) pair was remembered as having
        failed its time stamp on this connection, and the size given (the
        caller has already confirmed both sides' current sizes are equal)
        still matches the size recorded at transfer time. A size mismatch
        means the file has genuinely changed since, so the stale entry is
        dropped rather than kept around to answer False forever."""
        key = self._mtime_fallback_key(lp, rp)
        with self._mtime_fallback_lock:
            recorded = self._mtime_fallback.get(key)
            if recorded is None:
                return False
            if recorded == size:
                return True
            del self._mtime_fallback[key]
            return False

    def _apply_download_mtime(self, lp, rp, mtime, size):
        """Stamp a freshly downloaded local file with the remote file's
        modification time, so a later size+mtime compare reads an
        unchanged file as 'same'. A mtime of 0 means the server's reply
        carried no modification time; the file keeps its own time. If there
        is no time to set, or setting it fails, the file is remembered for
        the rest of this connection: a later compare or skip check that
        finds a matching size will still treat it as unchanged, even though
        its time does not match. This memory does not survive a
        disconnect/reconnect."""
        if not mtime:
            self._worker_log(f"server reported no modification time for {rp}; "
                             "this file will be treated as unchanged by size "
                             "for the rest of this connection", "warn")
            self._record_mtime_fallback(lp, rp, size)
            return
        try:
            os.utime(lp, (mtime, mtime))
        except OSError as e:
            self._worker_log(f"could not set modification time on {lp}: {e}; "
                             "this file will be treated as unchanged by size "
                             "for the rest of this connection", "warn")
            self._record_mtime_fallback(lp, rp, size)
        else:
            self._clear_mtime_fallback(lp, rp)

    def _apply_upload_mtime(self, sftp, rp, lp):
        """Stamp an uploaded remote file with the local source's modification
        time. If the server refuses to set the time, the file is
        remembered for the rest of this connection: a later compare or skip
        check that finds a matching size will still treat it as unchanged,
        even though its time does not match. This memory does not survive a
        disconnect/reconnect."""
        try:
            local_stat = os.stat(lp)
            mtime = int(local_stat.st_mtime)
        except OSError:
            return
        try:
            sftp.utime(rp, (mtime, mtime))
        except Exception as e:
            self._worker_log(f"server refused to set modification time on {rp}: "
                             f"{friendly_error(e)}; this file will be treated "
                             "as unchanged by size for the rest of this "
                             "connection", "warn")
            self._record_mtime_fallback(lp, rp, local_stat.st_size)
        else:
            self._clear_mtime_fallback(lp, rp)

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
        """Execute a "make folder" queue item: create the empty folder at
        remote_path (upload) or local_path (download). Idempotent if the
        folder is already there. Raises with a clear message (never swallows)
        if the target already exists as a FILE, or if creation otherwise
        fails, so the caller's existing retry/failure handling marks the
        queue item FAILED with that message exactly like a file transfer.

        dir_cache, if given, is updated with a newly-confirmed remote folder
        so a later file item under it skips the redundant ensure in _one."""
        if direction == "upload":
            try:
                st = sftp.stat(remote_path)
            except Exception:
                st = None
            if st is not None:
                if stat.S_ISDIR(st.st_mode):
                    if dir_cache is not None:
                        dir_cache.add(remote_path)
                    return
                raise IOError(
                    f"cannot create folder: a file with this name already exists: {remote_path}")
            parent = posixpath.dirname(remote_path)
            if parent and parent != "/":
                self._ensure_remote_dir(parent, sftp)
            try:
                sftp.mkdir(remote_path)
            except Exception:
                pass  # confirmed (or refuted) by the stat below, not by mkdir's own result
            # _ensure_remote_dir deliberately swallows mkdir errors, so the
            # only way to know the folder actually exists is to stat it here.
            st2 = sftp.stat(remote_path)
            if not stat.S_ISDIR(st2.st_mode):
                raise IOError(
                    f"cannot create folder: a file with this name already exists: {remote_path}")
            if dir_cache is not None:
                dir_cache.add(remote_path)
        else:
            if os.path.exists(local_path):
                if os.path.isdir(local_path):
                    return
                raise IOError(
                    f"cannot create folder: a file with this name already exists: {local_path}")
            try:
                os.makedirs(local_path, exist_ok=True)
            except OSError as e:
                raise IOError(f"cannot create folder: {e}") from e

    def _ensure_remote_dir(self, path, sftp=None):
        """Create path and any missing parents over sftp (defaults to
        self.sftp, which the folder watcher uses under _sftp_lock; a worker
        passes its own session, never the shared browsing one)."""
        sftp = self.sftp if sftp is None else sftp
        parts = path.strip("/").split("/")
        cur = "/"
        for part in parts:
            cur = posixpath.join(cur, part)
            try:
                sftp.stat(cur)
            except Exception:
                try:
                    sftp.mkdir(cur)
                except Exception:
                    pass

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
