"""Service functions for the watching area."""

import os
import time
import threading
import posixpath
from app.debug import debug
from app.errors import friendly_error
from app.paths import is_temp_part


def start_watch(api, local_dir, remote_dir):
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
    api.stop_watch()
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    with api._watch_lock:
        prev = api._watch_thread
    if prev is not None and prev.is_alive():
        return {"ok": False, "error": "Still stopping the previous watch. Try again in a moment."}
    if prev is not None:
        with api._watch_lock:
            if api._watch_thread is prev:
                api._watch_stop = None
                api._watch_thread = None
    if api.queue.pending() > 0:
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
            time.sleep(api._watch_interval)
            if stop.is_set():
                break
            cur = snapshot()
            changed = [fp for fp in cur if last.get(fp) != cur[fp]]
            # A queue worker uses its own session, but this shared-session
            # upload path is deliberately kept clear of an active batch.
            # Leave last and seen_changed untouched so the change is retried
            # on a later idle poll.
            if changed and api.queue.pending() > 0:
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
                api._legacy_active.set()
                try:
                    rdir = posixpath.dirname(rp)

                    def _cb(done_b, _t):  # watch has no progress bar; swallow progress callbacks
                        pass

                    # Serialize against the browsing session: this upload runs
                    # on the watcher thread and shares self.sftp with listing
                    # and health-ping bridge calls (see _browsing).
                    finished = False
                    with api._sftp_lock:
                        if stop.is_set():
                            seen_changed.pop(fp, None)
                            break
                        api._ensure_remote_dir(rdir)
                        finished = api._put_file(api.sftp, fp, rp, _cb, stop.is_set)
                    if finished:
                        api._worker_log(f"Watch: uploaded {rel}", "ok")
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
                    api._worker_log(f"Watch error: {rel} - {friendly_error(e)}", "error")
                    # Keep the change pending: leave last unadvanced so a
                    # later poll retries. Reset the stability gate so a
                    # transient failure does not skip the next steadiness
                    # check.
                    seen_changed.pop(fp, None)
                finally:
                    api._legacy_active.clear()
            if refreshed_folders:
                with api._watch_refresh_lock:
                    if not stop.is_set():
                        api._watch_refresh.update(refreshed_folders)
            # Forget entries for files that no longer exist so the two maps
            # do not grow without bound over a long session.
            for tracked in (last, seen_changed):
                for gone in [k for k in tracked if k not in cur]:
                    del tracked[gone]

    t = threading.Thread(target=loop, daemon=True)
    with api._watch_lock:
        api._watch_stop = stop
        api._watch_thread = t
    t.start()
    debug.log("WATCH start", {"local": local_dir, "remote": remote_dir})
    return {"ok": True}


def stop_watch(api):
    """Signal the current watcher to stop and wait (bounded) for its thread
    to retire before clearing the shared state, so a following start_watch
    never overlaps the old thread. If the thread has not retired within the
    wait, the shared state is left as-is (recording the straggler) instead
    of being cleared, so start_watch can tell one is still running and
    refuse to launch a second thread over the same folder. Called by
    start_watch (for a restart), Disconnect, and shutdown; always returns
    ok True so those callers proceed regardless."""
    with api._watch_lock:
        stop = api._watch_stop
        thread = api._watch_thread
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(3)
        if thread.is_alive():
            debug.log("WATCH: thread still running 3s after stop")
            return {"ok": True}
    with api._watch_lock:
        if api._watch_thread is thread:
            api._watch_stop = None
            api._watch_thread = None
    return {"ok": True}
