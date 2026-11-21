"""Service functions for the watching area."""

import os
import posixpath
import threading
import time
from app.debug import log_event
from app.errors import friendly_error
from app.paths import local_link_kind
from app.scratch_journal import is_active_local
from app.services.browse_session import close_browsing_sftp, open_browsing_sftp


def snapshot(api, local_dir, excluded):
    """Return the watchable files below local_dir, excluding local links.
    excluded carries the paths already reported across polls, so a link or
    unreadable entry is logged once while it stays that way."""
    snap = {}
    active_links = set()

    def skip(path, kind):
        active_links.add(path)
        if path not in excluded:
            excluded.add(path)
            api._worker_log(f"skipped {path}: it is a {kind}; links are not uploaded",
                            "warn", "link skipped")

    def unreadable(path, error):
        active_links.add(path)
        if path not in excluded:
            excluded.add(path)
            api._worker_log(f"could not read {path}: {error}", "error", "link skipped", error=error)

    # The watched folder itself is the one shown in the pane, so it is
    # watched even if it is a link; only links inside it are left out.
    for root, dirs, files in os.walk(local_dir):
        for name in dirs[:]:
            path = os.path.join(root, name)
            try:
                kind = local_link_kind(path)
            except OSError as e:
                unreadable(path, e)
                dirs.remove(name)
                continue
            if kind:
                skip(path, kind)
                dirs.remove(name)
        for fn in files:
            fp = os.path.join(root, fn)
            if is_active_local(fp):
                continue
            try:
                kind = local_link_kind(fp)
                if kind:
                    skip(fp, kind)
                    continue
                st = os.stat(fp)
                snap[fp] = (st.st_mtime, st.st_size)
            except OSError:
                pass
    excluded.intersection_update(active_links)
    return snap


def start_watch(api, local_dir, remote_dir):
    """Polls local_dir every 2 seconds and uploads files that changed since
    it started. The watcher opens its own bounded session for each pass, never
    uses the file pane's session, and stays bound to the connection it started
    on. _legacy_active is set only while an upload is actually running (not for
    the whole watch session), so a pane transfer can still be queued while
    watch idles between polls.

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
    snapshot skips active local scratch files, so a transfer in progress
    is never mistaken for a new local change.

    A stop only clears the watcher's shared state once its thread has
    actually retired; if it is still finishing up after a few seconds, a
    following start_watch is refused rather than risking two watch threads
    running over the same folder at once."""
    api.stop_watch()
    if not api._connected:
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
    if api._queue.pending() > 0:
        return {"ok": False, "error": "A transfer queue is active. Wait for it to finish."}
    # This run's own stop Event, captured by loop() below. Never read the
    # shared self._watch_stop from inside the thread: a stop or a restart can
    # replace it, and this local reference stays valid for this run alone.
    stop = threading.Event()

    excluded_links = set()

    # last: files whose current state has been accepted (uploaded, or present
    # unchanged since start) and must not be re-sent. Taken here, before the
    # thread starts, so any change made after start_watch returns is caught.
    # Taken inside the thread instead, a file written in the moment before
    # the thread ran would be recorded as already present and never uploaded.
    last = snapshot(api, local_dir, excluded_links)
    client = api._client
    generation = api._conn_generation

    def loop():
        # seen_changed: a changed file's state at its previous sighting, used
        # to require one stable poll before uploading.
        seen_changed = {}
        pending_open = None

        def connection_gone():
            return (not api._connected or api._client is not client or
                    api._conn_generation != generation)

        while not stop.is_set():
            time.sleep(api._watch_interval)
            if stop.is_set() or connection_gone():
                break
            session = None
            try:
                cur = snapshot(api, local_dir, excluded_links)
                changed = [fp for fp in cur if last.get(fp) != cur[fp]]
                # A queue worker uses its own session, and watch uploads leave
                # an active batch alone. Leave last and seen_changed untouched
                # so the change is retried on a later idle poll.
                if changed and api._queue.pending() > 0:
                    continue
                refreshed_folders = set()
                for fp in changed:
                    if stop.is_set() or connection_gone():
                        break
                    # Stability gate: upload only after the file looks the same
                    # as the previous poll, so a file mid-write waits.
                    if seen_changed.get(fp) != cur[fp]:
                        seen_changed[fp] = cur[fp]
                        continue
                    rel = os.path.relpath(fp, local_dir).replace("\\", "/")
                    rp = posixpath.join(remote_dir, rel)
                    if session is None:
                        if pending_open is not None:
                            if not pending_open.is_set():
                                break
                            pending_open = None
                        try:
                            session = open_browsing_sftp(client)
                        except Exception as e:
                            if isinstance(e, TimeoutError) and hasattr(e, "opener_done"):
                                pending_open = e.opener_done
                            api._worker_log(
                                f"Watch error: could not open an upload session - {friendly_error(e)}",
                                "error", "watch upload", outcome="failed", error=e)
                            # Keep the change pending and require another stable
                            # sighting before a later open attempt.
                            seen_changed.pop(fp, None)
                            break
                        if stop.is_set() or connection_gone():
                            close_browsing_sftp(session)
                            session = None
                            break
                    api._legacy_active.set()
                    try:
                        rdir = posixpath.dirname(rp)

                        def _cb(done_b, _t):  # watch has no progress bar; swallow progress callbacks
                            pass

                        api._ensure_remote_dir(rdir, session)
                        finished = api._put_file(session, fp, rp, _cb, stop.is_set)
                        if finished:
                            api._worker_log(f"Watch: uploaded {rel}", "ok", "watch upload", outcome="ok")
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
                        api._worker_log(f"Watch error: {rel} - {friendly_error(e)}", "error", "watch upload", outcome="failed", error=e)
                        # Keep the change pending: leave last unadvanced so a
                        # later poll retries. Reset the stability gate so a
                        # transient failure does not skip the next steadiness
                        # check.
                        seen_changed.pop(fp, None)
                        close_browsing_sftp(session)
                        session = None
                    finally:
                        api._legacy_active.clear()
                if connection_gone():
                    break
                if refreshed_folders:
                    with api._watch_refresh_lock:
                        if not stop.is_set():
                            api._watch_refresh.update(refreshed_folders)
                # Forget entries for files that no longer exist so the two maps
                # do not grow without bound over a long session.
                for tracked in (last, seen_changed):
                    for gone in [k for k in tracked if k not in cur]:
                        del tracked[gone]
            finally:
                close_browsing_sftp(session)
            if connection_gone():
                break
        # A watch never follows a reconnect onto a new connection, which may
        # be a different server; it ends and says so instead.
        if not stop.is_set() and connection_gone():
            api._worker_log("Watch stopped: the connection it started on has ended. "
                            "Start the watch again once connected.", "warn", "watch ended")
        elif stop.is_set():
            api._worker_log("Watch stopped.", "info", "watch ended")

    t = threading.Thread(target=loop, daemon=True)
    with api._watch_lock:
        api._watch_stop = stop
        api._watch_thread = t
    t.start()
    log_event("watch start")
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
            log_event("watch stop delayed")
            return {"ok": True}
    with api._watch_lock:
        if api._watch_thread is thread:
            api._watch_stop = None
            api._watch_thread = None
    return {"ok": True}
