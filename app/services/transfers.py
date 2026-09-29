"""Service functions for the transfers area."""

import os
import stat
import time
import threading
import posixpath
from app.constants import MTIME_TOL, WORKER_COUNT
from app.debug import debug
from app.formatting import human_size
from app.paths import local_link_target, safe_local_child
from app.workers import worker_target


def cancel(api):
    """Cancel-all, wired to the footer Cancel button: cancels every waiting
    queue item and flags every active one so each worker's byte loop stops
    and finalizes it as cancelled. Also stops any background scan still
    streaming files in, so a cancel during a huge scan halts it promptly.
    self._cancel is also set to stop a running folder-size calculation,
    which is not on the per-item flag."""
    api._stop_all_scans()
    api._stop_all_compares()
    api.queue.cancel_all()
    api._cancel.set()
    return {"ok": True}


def poll_queue(api):
    """Pulled by the window on a timer (~200ms) while a queue is active.
    This is the only channel from worker threads and the watcher to the UI:
    it never calls evaluate_js, so it cannot deadlock the window."""
    with api._console_lock:
        lines = api._console_buffer
        api._console_buffer = []
    with api._watch_refresh_lock:
        watch_refresh = sorted(api._watch_refresh)
        api._watch_refresh.clear()
    with api._watch_lock:
        watching = (api._watch_thread is not None
                    and api._watch_thread.is_alive()
                    and api._watch_stop is not None
                    and not api._watch_stop.is_set())
    items, pending = api.queue.snapshot_and_pending()
    active_ids = [it["id"] for it in items if it["state"] == "active"]
    with api._progress_lock:
        progress = {str(k): v for k, v in api._progress_by_id.items()}
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
    with api._compare_lock:
        for cid, entry in api._compares.items():
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
        api._deregister_compare(deregister_id)
    return {
        "items": items,
        "pending": pending,
        "active_ids": active_ids,
        "progress": progress,
        "console": lines,
        "watch_refresh": watch_refresh,
        "watching": watching,
        "paused": api.queue.is_paused(),
        # A background scan (enqueue/upload_paths) queues files as it
        # finds them, so the UI needs its own signal to show "Scanning…
        # N found" and read accurate totals even after old items have
        # aged out of items/pending above.
        "scanning": api._scan_active(),
        "scan_found": api._scan_found_total(),
        # A background compare/sync job walks the tree the same way, on
        # its own separate registry so its progress and the scan
        # progress above never collide.
        "comparing": api._compare_active(),
        "compare_found": api._compare_found_total(),
        "compare_done": compare_done,
        "counts": api.queue.counts(),
        "debug_warnings": api._drain_debug_warnings(),
        "debug_enabled": debug.is_enabled(),
    }


def cancel_item(api, item_id):
    """Cancel a single queued item. Waiting items go straight to cancelled;
    an active item is flagged (TransferItem.cancel_requested) so whichever
    worker owns it interrupts its byte loop and finalizes it."""
    api.queue.cancel(item_id)
    return {"ok": True}


def clear_finished(api):
    """Remove completed/failed/cancelled/skipped items so the window can
    re-render the queue without the clutter of finished transfers."""
    api.queue.clear_finished()
    items, pending = api.queue.snapshot_and_pending()
    return {"items": items, "pending": pending}


def retry_item(api, item_id):
    """One-click retry: put a failed or cancelled queue item back in line and
    wake the worker pool. Wired to the ↻ control on failed/cancelled rows."""
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    if not api.queue.requeue(item_id):
        return {"ok": False, "error": "That item can't be retried."}
    api._ensure_worker()
    return {"ok": True}


def retry_all_failed(api):
    """Put every FAILED item back in line and wake the worker pool.
    Wired to a footer "retry all failed" control."""
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    n = api.queue.retry_all_failed()
    api._ensure_worker()
    return {"ok": True, "requeued": n}


def pause_queue(api):
    """Pause the queue: stop claiming new items. Files already mid-transfer
    are left to finish; the worker pool winds down once they do. Wired to the
    footer Pause control."""
    api.queue.pause()
    api._worker_log("Pausing queue…")
    return {"ok": True}


def resume_queue(api):
    """Resume a paused queue and wake the worker pool to drain the waiting
    items in order."""
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    api.queue.resume()
    api._worker_log("Resuming queue")
    api._ensure_worker()
    return {"ok": True}


def enqueue(api, jobs, direction, local_dir, remote_dir, on_conflict="overwrite"):
    """New entry point for pane transfers: starts a background scan that
    streams jobs (a list of {name, is_dir}) into per-file queue
    items and returns immediately, instead of walking the whole tree up
    front. On a huge folder that walk used to block with zero feedback
    and create empty folder shells; now files are queued (and their
    remote/local parent folders created) as they are found, one at a
    time, with backpressure so the queue never balloons ahead of what the
    worker pool can drain. See _scan_and_queue and poll_queue's
    "scanning"/"scan_found" keys for how the UI observes progress."""
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    if api._legacy_active.is_set():
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
                api._worker_log(f"skipped unsafe remote name {name!r}: {e}", "error")
                continue
            target = local_link_target(lp) if is_dir else None
            if target:
                api._worker_log(f"{name} is symlinked to {target}; files will be written there.", "warn")
        roots.append((lp, rp, is_dir))
    scan_id, stop_event = api._register_scan()
    t = threading.Thread(target=api._scan_and_queue,
                          args=(roots, direction, on_conflict, scan_id, stop_event, local_dir),
                          daemon=True)
    t.start()
    return {"ok": True, "scanning": True}


def _enqueue_files(api, files, direction, on_conflict):
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
        api.queue.append(direction, lp, rp, name, size=queue_size, on_conflict=on_conflict,
                           is_dir=is_dir)
    batch_target = worker_target(sizes)
    with api._worker_lock:
        if not api._workers:
            api._target_workers = batch_target
        else:
            api._target_workers = max(api._target_workers, batch_target)
    api._ensure_worker()
    return len(files)


def _ensure_worker(api):
    """Top the worker pool up to self._target_workers live threads
    whenever there is waiting work. Workers are started here under the
    same lock a worker retires itself with, so starting and stopping
    workers can never overlap and leave the pool in the wrong state."""
    with api._worker_lock:
        if api.queue.is_paused():
            return
        api._workers = [w for w in api._workers if w.is_alive()]
        while len(api._workers) < api._target_workers and api.queue.waiting() > 0:
            w = threading.Thread(target=api._worker_loop, daemon=True)
            api._workers.append(w)
            w.start()


def _worker_loop(api):
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
        if not api.connected or api.client is None:
            # Disconnected before this worker could start. Give a plain
            # reason instead of leaking a raw "'NoneType' has no attribute
            # open_sftp" from the line below.
            raise ConnectionError("disconnected before the transfer started")
        sftp = api.client.open_sftp()
    except Exception as e:
        reason = str(e).strip() or e.__class__.__name__
        # No silent failure: surface it in the console log and let this
        # worker retire; the other worker (if any) keeps draining the queue.
        api._worker_log(f"could not open a transfer session: {reason}", "error")
        with api._worker_lock:
            if me in api._workers:
                api._workers.remove(me)
            if not api._workers:
                api._target_workers = WORKER_COUNT
                with api._progress_lock:
                    api._progress_by_id = {}
                # Last worker out and none could open a session: don't leave
                # queued items sitting as WAITING with nothing to drain them.
                # Mark them failed so the failure is visible in the queue.
                stranded = api.queue.fail_waiting(f"transfer session unavailable: {reason}")
                if stranded:
                    api._worker_log(
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
            item = api.queue.claim()
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
                with api._worker_lock:
                    paused = api.queue.is_paused()
                    if api.queue.waiting() == 0 or paused:
                        if me in api._workers:
                            api._workers.remove(me)
                        # Only the last worker to leave clears pool-wide
                        # state and logs the drain summary; the others just
                        # retire quietly.
                        if not api._workers:
                            with api._progress_lock:
                                api._progress_by_id = {}
                            if api.queue.is_paused() and api.queue.waiting() > 0:
                                # Paused with items still held: leave the target
                                # alone so a resume drains the rest at the count
                                # this batch was scaled to, not the default.
                                api._worker_log(
                                    f"Queue paused ({api.queue.waiting()} still waiting)")
                            else:
                                # Reached the empty queue (paused with nothing
                                # left, or a normal drain). Reset the pool target
                                # so a later lone retry does not inherit a stale
                                # scaled-up count, and clear a stale pause flag so
                                # the next batch is not held back by a pause that
                                # belonged to a queue now emptied.
                                api._target_workers = WORKER_COUNT
                                api.queue.resume()
                                counts = api.queue.counts()
                                api._worker_log(
                                    f"Queue drained: {counts.get('completed', 0)} completed, "
                                    f"{counts.get('failed', 0)} failed, {counts.get('cancelled', 0)} cancelled, "
                                    f"{counts.get('skipped', 0)} skipped")
                        break
                continue
            total = done_count + api.queue.pending()
            if item.cancel_requested:
                # cancel() on a waiting item goes straight to CANCELLED, so this
                # should not happen in practice, but guard anyway.
                api.queue.mark_cancelled(item.id)
                api._worker_log(f"cancelled {item.name}", "warn")
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
                        api._make_dir(item.direction, item.local_path,
                                        item.remote_path, sftp, dir_cache)
                        res = "ok"
                    else:
                        res = api._one(item.direction, item.local_path, item.remote_path,
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
                    if api._connection_dead(sftp):
                        # The session itself is gone, not just this one
                        # file: retrying it (or any remaining item) would
                        # only grind through the same failure. Stop the
                        # whole batch once instead of one error per file.
                        last_err = "Connection lost"
                        api._report_dead_connection()
                        break
                    time.sleep(0.6)
            if res == "cancelled" or (not ok and item.cancel_requested):
                # res == "cancelled": the byte loop itself broke early, so
                # the transfer did not finish; that is the one true source
                # of truth here, not a flag re-check after the fact, which
                # could mislabel a file that finished sending its very last
                # byte the instant cancel arrived.
                api.queue.mark_cancelled(item.id)
                api._worker_log(f"cancelled {item.name}", "warn")
            elif res == "skip":
                api.queue.mark_skipped(item.id)
                api._worker_log(f"skip {item.name} (already up to date)")
            elif ok:
                api.queue.mark_completed(item.id)
                if item.is_dir:
                    if item.direction == "upload":
                        api._worker_log(f"{arrow} folder {item.remote_path}", "ok")
                    else:
                        api._worker_log(f"created folder {item.local_path}", "ok")
                else:
                    api._worker_log(f"{arrow} {(item.remote_path if item.direction == 'upload' else item.name)}", "ok")
            else:
                api.queue.mark_failed(item.id, last_err or "transfer failed")
                api._worker_log(f"failed {item.name}", "error")
            done_count += 1
            with api._progress_lock:
                api._progress_by_id.pop(item.id, None)
    finally:
        try:
            sftp.close()
        except Exception:
            pass
        # Safety net for an exit by exception rather than the clean
        # empty-queue path above: remove this worker if it is still in the
        # pool, and only clear pool-wide state if it was the last one.
        with api._worker_lock:
            if me in api._workers:
                api._workers.remove(me)
            if not api._workers:
                # Leave the target alone during a pause-hold so a resume drains
                # the rest at the scaled count; any real drain resets it.
                if not (api.queue.is_paused() and api.queue.waiting() > 0):
                    api._target_workers = WORKER_COUNT
                with api._progress_lock:
                    api._progress_by_id = {}


def upload_paths(api, paths, remote_dir, on_conflict="overwrite"):
    """Upload absolute local paths (files or folders) dragged in from outside
    the app, into remote_dir. Only the top-level dropped items are stat'd
    here (cheap: one isdir check each); the actual folder contents stream
    in via the same background scan as enqueue(), so a huge dropped folder
    does not block this call."""
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    if api._legacy_active.is_set():
        return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
    roots = []   # (local_path, remote_path, is_dir)
    try:
        for raw in paths or []:
            lp = api._normalize_drop_path(raw)
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
    scan_id, stop_event = api._register_scan()
    t = threading.Thread(target=api._scan_and_queue,
                          args=(roots, "upload", on_conflict, scan_id, stop_event),
                          daemon=True)
    t.start()
    return {"ok": True, "scanning": True}


def _normalize_drop_path(p):
    """pywebview yields file-URL style paths (e.g. '/C:/Users/..') on Windows."""
    p = (p or "").strip()
    if os.name == "nt":
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        p = p.replace("/", "\\")
    return p


def on_external_drop(api, event):
    """pywebview Qt drop handler: hand the real file paths back to the UI,
    which uploads them into the currently open remote folder."""
    try:
        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        debug.log("EXTERNAL DROP", {"paths": paths})
        if paths:
            api._emit("external_drop", {"paths": paths})
    except Exception as e:
        debug.log("external drop handler failed", str(e))


def _one(api, direction, lp, rp, name, idx, total, on_conflict, sftp,
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
        dst_size, dst_mtime = api._rstat(sftp, rp)
    else:
        src_size, src_mtime = api._rstat(sftp, rp)
        if os.path.exists(lp):
            dst_stat = os.stat(lp)
            dst_size, dst_mtime = dst_stat.st_size, int(dst_stat.st_mtime)
        else:
            dst_size, dst_mtime = -1, 0
    sizes_match = src_size >= 0 and dst_size == src_size
    if on_conflict == "skip" and sizes_match and (
            abs(dst_mtime - src_mtime) <= MTIME_TOL
            or api._mtime_fallback_matches(lp, rp, src_size)):
        # user chose skip and the other side matches on size, and either
        # the modification time also matches (within tolerance) or this
        # connection remembers this pair as having failed its time stamp
        # at transfer time with the same size (see _mtime_fallback_matches)
        # -> leave it. overwrite deliberately falls through and resends,
        # even when size and time match, since neither alone proves the
        # contents match.
        api._progress(name, idx, total, src_size, src_size, 0, progress_key)
        return "skip"
    # Always rewrite from the start. A smaller destination is not proof of
    # an interrupted transfer worth resuming: it is just as likely an older,
    # different file. Appending onto it would splice the old head onto the
    # new tail, and because that mangled file often ends up the same size as
    # the source, a later compare would read it as "same" and never fix it.
    # Sending fresh every time is the only safe rule size alone supports.
    start = time.time()

    def cb(done_b, _t):
        api._progress(name, idx, total, done_b, src_size, time.time() - start, progress_key)

    os.makedirs(os.path.dirname(lp), exist_ok=True) if direction == "download" else None
    if direction == "upload":
        rdir = posixpath.dirname(rp)
        if rdir and rdir != "/" and (dir_cache is None or rdir not in dir_cache):
            api._ensure_remote_dir(rdir, sftp)
            if dir_cache is not None:
                dir_cache.add(rdir)
        finished = api._put_file(sftp, lp, rp, cb, cancel_check)
    else:
        finished = api._get_file(sftp, rp, lp, cb, cancel_check)
    # finished is False only if the byte loop broke early on cancel_check;
    # a transfer that sent every byte is "ok" even if cancel arrived a
    # moment later, so the caller must not re-check a cancel flag itself.
    return "ok" if finished else "cancelled"


def _progress(api, name, idx, total, sent, size, elapsed, progress_key=None):
    speed = (sent / elapsed) if elapsed > 0 else 0
    eta = ((size - sent) / speed) if speed > 0 and size > 0 else 0
    payload = {"name": name, "index": idx, "total": total,
               "pct": int(sent * 100 / size) if size else 100,
               "speed": human_size(speed) + "/s" if speed else "",
               "eta": int(eta)}
    # Called from worker threads: keyed by item id and kept in memory
    # only, since the window pulls it through poll_queue(). Never emit
    # from here (evaluate_js off the bridge thread deadlocks the window).
    with api._progress_lock:
        api._progress_by_id[progress_key] = payload


def _rstat(api, sftp, rp):
    """Remote file size and modification time. -1 size / 0 mtime on
    failure."""
    try:
        a = sftp.stat(rp)
        return a.st_size, int(a.st_mtime or 0)
    except Exception:
        return -1, 0


def _transfers_active(api):
    """Single source of truth for 'a transfer batch is running', used by
    both the window-close veto and the page's own Disconnect confirm
    (see transfers_active()). True when the queue has waiting or active
    items, a background scan is still walking a folder, or any worker
    thread is alive."""
    if api.queue.pending() > 0:
        return True
    if api._scan_active():
        return True
    with api._worker_lock:
        return any(w.is_alive() for w in api._workers)


def transfers_active(api):
    """JS-callable wrapper for _transfers_active(): pywebview never
    exposes underscore-prefixed methods to the page, so this is what
    onConn()'s Disconnect confirm actually calls."""
    return api._transfers_active()


def _make_dir(api, direction, local_path, remote_path, sftp, dir_cache=None):
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
            api._ensure_remote_dir(parent, sftp)
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


def _ensure_remote_dir(api, path, sftp=None):
    """Create path and any missing parents over sftp (defaults to
    self.sftp, which the folder watcher uses under _sftp_lock; a worker
    passes its own session, never the shared browsing one)."""
    sftp = api.sftp if sftp is None else sftp
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
