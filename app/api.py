"""The bridge object the page calls."""

import os
import time
import threading
import functools
import posixpath


from app import services
from app.constants import WORKER_COUNT
from app.debug import debug
from app.errors import friendly_error
from app.paths import is_temp_part
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
        return services.scanning._register_scan(self)

    def _deregister_scan(self, scan_id):
        return services.scanning._deregister_scan(self, scan_id)

    def _stop_all_scans(self):
        return services.scanning._stop_all_scans(self)

    def _scan_active(self):
        return services.scanning._scan_active(self)

    def _bump_scan_found(self, scan_id, n):
        return services.scanning._bump_scan_found(self, scan_id, n)

    def _scan_found_total(self):
        return services.scanning._scan_found_total(self)

    def _scan_wait_for_room(self, stop_event):
        return services.scanning._scan_wait_for_room(self, stop_event)

    def _iter_local(self, lp, rp, is_dir, problems=None, include_dirs=False):
        return services.scanning._iter_local(self, lp, rp, is_dir, problems, include_dirs)

    def _iter_remote(self, sftp, rp, lp, is_dir, root, problems=None, include_dirs=False):
        return services.scanning._iter_remote(self, sftp, rp, lp, is_dir, root, problems, include_dirs)

    def _scan_and_queue(self, roots, direction, on_conflict, scan_id, stop_event, local_root=None):
        return services.scanning._scan_and_queue(self, roots, direction, on_conflict, scan_id, stop_event, local_root)

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
        return services.compare_sync._compute_pair_maps(self, sftp, local_dir, remote_dir, on_progress, stop_event)

    def _classify(self, rel, local_map, remote_map):
        return services.compare_sync._classify(self, rel, local_map, remote_map)

    def _compute_compare(self, sftp, local_dir, remote_dir, on_progress=None, stop_event=None):
        return services.compare_sync._compute_compare(self, sftp, local_dir, remote_dir, on_progress, stop_event)

    def _compute_sync(self, sftp, local_dir, remote_dir, direction, changed_only=True,
                       on_progress=None, stop_event=None):
        return services.compare_sync._compute_sync(self, sftp, local_dir, remote_dir, direction, changed_only, on_progress, stop_event)

    # ───────────── compare / sync background jobs (own thread, own session) ─────────────
    def _start_compare(self, kind):
        return services.compare_sync._start_compare(self, kind)

    def _deregister_compare(self, cid):
        return services.compare_sync._deregister_compare(self, cid)

    def _stop_all_compares(self):
        return services.compare_sync._stop_all_compares(self)

    def _compare_active(self):
        return services.compare_sync._compare_active(self)

    def _bump_compare_found(self, cid, n):
        return services.compare_sync._bump_compare_found(self, cid, n)

    def _compare_found_total(self):
        return services.compare_sync._compare_found_total(self)

    def _run_compare(self, cid, local_dir, remote_dir, stop_event, direction=None, changed_only=True):
        return services.compare_sync._run_compare(self, cid, local_dir, remote_dir, stop_event, direction, changed_only)

    def compare(self, local_dir, remote_dir):
        return services.compare_sync.compare(self, local_dir, remote_dir)

    def sync_plan(self, local_dir, remote_dir, direction, changed_only=True):
        return services.compare_sync.sync_plan(self, local_dir, remote_dir, direction, changed_only)

    def _stream_sync_transfers(self, transfers, direction, on_conflict, scan_id, stop_event, token):
        return services.compare_sync._stream_sync_transfers(self, transfers, direction, on_conflict, scan_id, stop_event, token)

    def start_sync(self, token, on_conflict="overwrite"):
        return services.compare_sync.start_sync(self, token, on_conflict)

    def discard_sync(self, token):
        return services.compare_sync.discard_sync(self, token)

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
