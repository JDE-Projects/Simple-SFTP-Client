"""Service functions for the compare_sync area."""

import threading
import traceback
import posixpath
from app import constants
from app.constants import MTIME_TOL
from app.debug import debug
from app.errors import ScanIncomplete, friendly_error


def _compute_pair_maps(api, sftp, local_dir, remote_dir, on_progress=None, stop_event=None):
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

    for lp, rp, size, mtime, is_dir in api._iter_local(
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
    for lp, rp, size, mtime, is_dir in api._iter_remote(
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


def _classify(api, rel, local_map, remote_map):
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
        if api._mtime_fallback_matches(lp, rp, lsize):
            return "same"
    return "newer_local" if lmtime >= rmtime else "newer_remote"


def _compute_compare(api, sftp, local_dir, remote_dir, on_progress=None, stop_event=None):
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
    local_map, remote_map = api._compute_pair_maps(
        sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
    if local_map is None:
        return None
    statuses = {rel: api._classify(rel, local_map, remote_map)
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


def _compute_sync(api, sftp, local_dir, remote_dir, direction, changed_only=True,
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
    local_map, remote_map = api._compute_pair_maps(
        sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
    if local_map is None:
        return None, None, None
    wanted = ("local_only", "newer_local") if direction == "upload" else ("remote_only", "newer_remote")
    plan = []
    transfers = []
    conflicts = []
    for rel in set(local_map) | set(remote_map):
        status = api._classify(rel, local_map, remote_map)
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


def _start_compare(api, kind):
    """Start tracking a new background compare/sync job, mirroring
    _register_scan: its own stop Event and found count, keyed by its own
    id, so a stop from an earlier job can never reach a later one. Kept
    on a separate registry from _scans so the download-scan "Scanning…
    N found" label and the compare/sync "Comparing… N" label never
    leak into each other."""
    with api._compare_lock:
        cid = api._compare_next_id
        api._compare_next_id += 1
        api._compares[cid] = {
            "stop": threading.Event(), "found": 0, "kind": kind, "direction": None,
            "done": False, "ok": False, "error": None, "result": None,
            "transfers": None, "delivered": False,
        }
        return cid, api._compares[cid]["stop"]


def _deregister_compare(api, cid):
    with api._compare_lock:
        api._compares.pop(cid, None)


def _stop_all_compares(api):
    """Signal every currently-running compare/sync job to stop walking.
    Used by Cancel, a dead connection, and disconnect, same as
    _stop_all_scans."""
    with api._compare_lock:
        for c in api._compares.values():
            c["stop"].set()


def _compare_active(api):
    with api._compare_lock:
        return any(not c["done"] for c in api._compares.values())


def _bump_compare_found(api, cid, n):
    with api._compare_lock:
        entry = api._compares.get(cid)
        if entry is not None:
            entry["found"] += n


def _compare_found_total(api):
    """Sum of found-so-far across every still-running compare/sync job,
    for poll_queue()."""
    with api._compare_lock:
        return sum(c["found"] for c in api._compares.values() if not c["done"])


def _run_compare(api, cid, local_dir, remote_dir, stop_event, direction=None, changed_only=True):
    """Runs a compare or a sync-plan computation entirely on its own
    daemon thread, never the pywebview bridge thread, over its own sftp
    session (self._sftp stays reserved for the file browser). kind is
    read off the registered entry: "compare" stores the recursive
    files/folders result; "sync" stores a plan summary and stashes the
    full transfer list on the entry for start_sync() to stream later.
    Always marks the entry done and closes its session, and never lets
    an unexpected exception fail silently."""
    with api._compare_lock:
        entry = api._compares.get(cid)
    kind = entry["kind"] if entry else "compare"

    def _finish(ok, error=None, result=None, transfers=None):
        with api._compare_lock:
            e = api._compares.get(cid)
            if e is not None:
                e["ok"] = ok
                e["error"] = error
                e["result"] = result
                e["transfers"] = transfers
                e["done"] = True

    sftp = None
    try:
        try:
            sftp = api._client.open_sftp()
        except Exception as e:
            reason = friendly_error(e)
            api._worker_log(f"compare: could not open a transfer session: {reason}", "error")
            _finish(False, error=reason)
            return
        on_progress = lambda n: api._bump_compare_found(cid, n)  # noqa: E731
        if kind == "sync":
            plan, transfers, conflicts = api._compute_sync(
                sftp, local_dir, remote_dir, direction, changed_only,
                on_progress=on_progress, stop_event=stop_event)
            if plan is None:
                _finish(False, error="Cancelled.")
                return
            with api._compare_lock:
                e = api._compares.get(cid)
                if e is not None:
                    e["direction"] = direction
            total_bytes = sum(t[2] for t in transfers if t[2] and t[2] > 0)
            result = {"count": len(transfers), "total_bytes": total_bytes,
                      "sample": plan[:200], "more": max(0, len(plan) - 200), "token": cid,
                      "conflicts": conflicts}
            _finish(True, result=result, transfers=transfers)
        else:
            data = api._compute_compare(
                sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
            if data is None:
                _finish(False, error="Cancelled.")
                return
            data["root_local"] = local_dir
            data["root_remote"] = remote_dir
            _finish(True, result=data)
    except ScanIncomplete as e:
        api._worker_log(f"compare: {e}", "error")
        _finish(False, error=str(e))
    except Exception as e:
        reason = friendly_error(e)
        api._worker_log(f"compare failed: {reason}", "error")
        debug.log("COMPARE failed", traceback.format_exc())
        _finish(False, error=reason)
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass


def compare(api, local_dir, remote_dir):
    """Starts a recursive compare on its own daemon thread and returns
    immediately; the result arrives via poll_queue()'s compare_done key.
    Deliberately not @_browsing: it must not hold the shared browsing
    session lock, since it walks over its own sftp session and can take
    a long time on a big tree."""
    if not api._connected:
        return {"ok": False, "error": "Not connected."}
    if api._compare_active():
        return {"ok": False, "error": "A compare or sync is already running."}
    cid, stop_event = api._start_compare("compare")
    t = threading.Thread(target=api._run_compare, args=(cid, local_dir, remote_dir, stop_event),
                          daemon=True)
    t.start()
    return {"ok": True, "comparing": True}


def sync_plan(api, local_dir, remote_dir, direction, changed_only=True):
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
    if not api._connected:
        return {"ok": False, "error": "Not connected."}
    if api._legacy_active.is_set():
        return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
    if api._compare_active():
        return {"ok": False, "error": "A compare or sync is already running."}
    with api._compare_lock:
        stale = [cid for cid, e in api._compares.items() if e["kind"] == "sync" and e["done"]]
        for cid in stale:
            api._compares.pop(cid, None)
    cid, stop_event = api._start_compare("sync")
    t = threading.Thread(target=api._run_compare,
                          args=(cid, local_dir, remote_dir, stop_event, direction, changed_only),
                          daemon=True)
    t.start()
    return {"ok": True, "comparing": True}


def _stream_sync_transfers(api, transfers, direction, on_conflict, scan_id, stop_event, token):
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
            api._scan_wait_for_room(stop_event)
            if stop_event.is_set() or not api._connected:
                return
            found += len(batch)
            api._bump_scan_found(scan_id, len(batch))
            api._enqueue_files(batch, direction, on_conflict)
            batch = []

        batch_cap = min(64, constants.SCAN_QUEUE_HIGH_WATER) or 64
        for lp, rp, size, is_dir in transfers:
            if stop_event.is_set() or not api._connected:
                break
            batch.append((lp, rp, size, 0, is_dir))
            if len(batch) >= batch_cap:
                flush()
                if stop_event.is_set() or not api._connected:
                    break
        flush()
        if stop_event.is_set():
            api._worker_log(f"Sync stopped ({found} item(s) queued before stopping)", "warn")
        elif not api._connected:
            api._worker_log(f"Sync halted: disconnected ({found} item(s) queued)", "warn")
        elif found:
            api._worker_log(f"Sync: {found} item(s) queued")
        else:
            api._worker_log("Sync: nothing to transfer")
    except Exception as e:
        api._worker_log(f"sync failed: {friendly_error(e)}", "error")
        debug.log("SYNC failed", traceback.format_exc())
    finally:
        api._deregister_scan(scan_id)
        api._deregister_compare(token)


def start_sync(api, token, on_conflict="overwrite"):
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
    if not api._connected:
        return {"ok": False, "error": "Not connected."}
    if api._legacy_active.is_set():
        return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
    with api._compare_lock:
        entry = api._compares.get(token)
        if entry is not None and entry.get("transfers") is not None:
            api._compares.pop(token, None)
        else:
            entry = None
    if entry is None:
        return {"ok": False, "error": "That sync plan is no longer available. Run Sync again."}
    transfers = entry["transfers"]
    direction = entry["direction"]
    scan_id, stop_event = api._register_scan()
    t = threading.Thread(target=api._stream_sync_transfers,
                          args=(transfers, direction, on_conflict, scan_id, stop_event, token),
                          daemon=True)
    t.start()
    return {"ok": True, "scanning": True}


def discard_sync(api, token):
    """Bridge method for the page to free a sync plan it decided not to
    use: the user declined the confirmation, or start_sync() refused it.
    Pops the entry only if it is a finished sync plan (done computing,
    never a still-running one, which a stop can't safely interrupt from
    here); a still-running job is left alone. Idempotent and always
    returns ok, so a stale or unknown token (already consumed, already
    discarded, or from a job that failed and was already dropped) is
    harmless to pass."""
    with api._compare_lock:
        entry = api._compares.get(token)
        if entry is not None and entry["kind"] == "sync" and entry["done"]:
            api._compares.pop(token, None)
    return {"ok": True}
