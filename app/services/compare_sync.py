"""Service functions for the compare_sync area."""

import os
import threading
import posixpath
from app import constants
from app.constants import MTIME_TOL
from app.errors import ScanIncomplete, friendly_error
from app.paths import windows_destination_key


class _BoundedCollector(list):
    """A list the remote walk appends skipped names to. It refuses (via
    on_full, which raises ScanIncomplete) once it would hold more than
    COMPARE_SYNC_ENTRY_LIMIT entries, so a server sending endless unusable
    names is refused during the walk instead of filling memory."""
    def __init__(self, on_full):
        super().__init__()
        self._on_full = on_full

    def append(self, item):
        if len(self) >= constants.COMPARE_SYNC_ENTRY_LIMIT:
            self._on_full()
        super().append(item)


class _PairMap(dict):
    """Comparison entries plus local spelling aliases for their result keys."""

    def __init__(self):
        super().__init__()
        self.local_aliases = {}


def _compute_pair_maps(api, sftp, local_dir, remote_dir, on_progress=None, stop_event=None,
                       excluded=None, omissions=None):
    """Build canonical comparison maps while preserving each side's paths.

    Entries pair only when every relative-path segment is equal apart from
    ASCII letter case. Result keys use remote spelling where that folder or
    entry exists remotely, otherwise local spelling. Windows-equivalent names
    that are not ASCII-case pairs, local destination clashes, unsafe remote
    names, and scanner-reported remote clashes are held back with omissions.
    Folder inventories include non-empty folders, allowing a file on one side
    and a folder on the other to be reported as a conflict. The compared root
    (rel ".") is never added. on_progress receives a count since its prior
    call, not a running total. Returns (None, None) on cancellation so callers
    can distinguish it from an empty result. Raises ScanIncomplete for any
    listing or metadata failure, because a partially seen tree can misclassify
    entries, and for an over-limit walk. A local walk problem or limit refusal
    happens before the remote network walk starts; a remote walk is likewise
    refused when its skipped names exceed the limit."""
    local_map = _PairMap()
    remote_map = _PairMap()
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

    def record_omission(name, reason, is_dir):
        if omissions is None:
            return
        omissions["count"] += 1
        if len(omissions["entries"]) < 200:
            omissions["entries"].append(
                {"name": name, "reason": reason, "is_dir": bool(is_dir)})

    rel_keys = {}

    def rel_key(rel):
        key = rel_keys.get(rel)
        if key is None:
            key = windows_destination_key(os.path.join(local_dir, *rel.split("/")))
            rel_keys[rel] = key
        return key

    def ascii_pair(left, right):
        left_parts = left.split("/")
        right_parts = right.split("/")
        if len(left_parts) != len(right_parts):
            return False
        for left_part, right_part in zip(left_parts, right_parts, strict=True):
            if len(left_part) != len(right_part):
                return False
            for left_char, right_char in zip(left_part, right_part, strict=True):
                if left_char == right_char:
                    continue
                if not (left_char.isascii() and right_char.isascii()
                        and left_char.isalpha() and right_char.isalpha()
                        and left_char.lower() == right_char.lower()):
                    return False
        return True

    def inventory(entries):
        folders = {}
        for rel, entry in entries:
            parts = rel.split("/")
            for depth in range(1, len(parts)):
                folders.setdefault("/".join(parts[:depth]), True)
            if entry[4]:
                folders[rel] = True
        return folders

    def held_by_key(key, keys):
        while key:
            if key in keys:
                return key
            parent, sep, _ = key.rpartition("\\")
            if not sep:
                return None
            key = parent
        return None

    def record_entries(entries, reason):
        for rel, entry in entries:
            record_omission(rel, reason, entry[4])

    def keep_outside(entries, held_keys):
        return [(rel, entry) for rel, entry in entries
                if held_by_key(rel_key(rel), held_keys) is None]

    def indexed_inventory(entries, folders):
        inventory_by_key = {}
        for rel, _entry in entries:
            inventory_by_key.setdefault(rel_key(rel), rel)
        for rel in folders:
            inventory_by_key.setdefault(rel_key(rel), rel)
        return inventory_by_key

    local_excluded = []
    local_entries = []
    for lp, rp, size, mtime, is_dir in api._iter_local(
            local_dir, remote_dir, True, problems=problems, include_dirs=True,
            excluded=local_excluded, allow_root_link=True):
        if stop_event is not None and stop_event.is_set():
            return None, None
        rel = posixpath.relpath(rp, remote_dir)
        if rel == ".":
            continue
        if len(local_entries) >= constants.COMPARE_SYNC_ENTRY_LIMIT:
            _raise_too_many()
        local_entries.append((rel, (size, mtime, lp, rp, is_dir)))
        bump()
    # Fail fast: if the local walk already found a problem, refuse now
    # rather than running the whole remote (network) walk just to discard it.
    if problems:
        _raise_incomplete()
    excluded_rels = [os.path.relpath(path, local_dir).replace("\\", "/")
                     for path in local_excluded]
    if excluded is not None:
        excluded.extend(excluded_rels)
    remote_clashes = _BoundedCollector(_raise_too_many)
    unsafe_names = _BoundedCollector(_raise_too_many)
    remote_entries = []
    for lp, rp, size, mtime, is_dir in api._iter_remote(
            sftp, remote_dir, local_dir, True, local_dir, problems=problems, include_dirs=True,
            clashes=remote_clashes, unsafe_names=unsafe_names):
        if stop_event is not None and stop_event.is_set():
            return None, None
        rel = posixpath.relpath(rp, remote_dir)
        if rel == ".":
            continue
        if len(remote_entries) >= constants.COMPARE_SYNC_ENTRY_LIMIT:
            _raise_too_many()
        remote_entries.append((rel, (size, mtime, lp, rp, is_dir)))
        bump()
    for unsafe in unsafe_names:
        record_omission(posixpath.relpath(unsafe["remote_path"], remote_dir),
                        "unsafe name", unsafe["is_dir"])
    # Build these before any holding filter. In particular, a remote folder
    # with a held clash below it still supplies the spelling for local-only
    # files elsewhere in that folder.
    local_folders = inventory(local_entries)
    remote_folders = inventory(remote_entries)
    if (len(local_folders) > constants.COMPARE_SYNC_ENTRY_LIMIT
            or len(remote_folders) > constants.COMPARE_SYNC_ENTRY_LIMIT):
        _raise_too_many()
    clash_groups = {}
    for clash in remote_clashes:
        key = windows_destination_key(clash["local_path"])
        clash_groups.setdefault(key, clash["other_remote"])
        if len(clash_groups) > constants.COMPARE_SYNC_ENTRY_LIMIT:
            _raise_too_many()
        record_omission(posixpath.relpath(clash["remote_path"], remote_dir),
                        "name clash", clash["is_dir"])
    excluded_keys = {rel_key(rel) for rel in excluded_rels}
    remote_entries = [
        (rel, entry) for rel, entry in remote_entries
        if held_by_key(rel_key(rel), excluded_keys) is None
    ]
    remote_folders = {
        rel: is_dir for rel, is_dir in remote_folders.items()
        if held_by_key(rel_key(rel), excluded_keys) is None
    }
    if clash_groups:
        # Each entry's key is computed once and checked, with each of its
        # ancestors, against the group keys, so the cost grows with the
        # tree size, not tree size times the number of clash groups.
        def held_key(entry_key):
            k = entry_key
            while k:
                if k in clash_groups:
                    return k
                head, sep, _ = k.rpartition("\\")
                if not sep:
                    return None
                k = head
            return None

        kept_is_dir = {}
        local_top = {}
        kept_remote_entries = []
        kept_local_entries = []
        for entries, is_local in ((remote_entries, False), (local_entries, True)):
            for rel, entry in entries:
                key = held_key(rel_key(rel))
                if key is None:
                    (kept_local_entries if is_local else kept_remote_entries).append((rel, entry))
                    continue
                if is_local:
                    depth = posixpath.relpath(clash_groups[key], remote_dir).count("/") + 1
                    parts = rel.split("/")
                    is_dir = len(parts) > depth or bool(entry[4])
                    _, was_dir = local_top.get(key, (None, False))
                    local_top[key] = ("/".join(parts[:depth]), is_dir or was_dir)
                else:
                    is_dir = entry[3] != clash_groups[key] or bool(entry[4])
                    kept_is_dir[key] = kept_is_dir.get(key, False) or is_dir
        remote_entries = kept_remote_entries
        local_entries = kept_local_entries
        local_folders = {
            rel: is_dir for rel, is_dir in local_folders.items()
            if held_key(rel_key(rel)) is None
        }
        remote_folders = {
            rel: is_dir for rel, is_dir in remote_folders.items()
            if held_key(rel_key(rel)) is None
        }
        for key, kept_remote in clash_groups.items():
            record_omission(posixpath.relpath(kept_remote, remote_dir), "name clash",
                            kept_is_dir.get(key, False))
            if key in local_top:
                record_omission(local_top[key][0], "name clash", local_top[key][1])

    # A local synthetic walk can contain two names Windows would merge. Do
    # not let a dictionary overwrite either one, and hold the corresponding
    # remote destination subtree as well.
    local_groups = {}
    for rel, entry in local_entries:
        local_groups.setdefault(rel_key(rel), []).append((rel, entry))
    local_clash_keys = {key for key, entries in local_groups.items() if len(entries) > 1}
    if local_clash_keys:
        held_local = [(rel, entry) for rel, entry in local_entries
                      if held_by_key(rel_key(rel), local_clash_keys) is not None]
        held_remote = [(rel, entry) for rel, entry in remote_entries
                       if held_by_key(rel_key(rel), local_clash_keys) is not None]
        record_entries(held_local, "name clash")
        record_entries(held_remote, "name clash")
        local_entries = keep_outside(local_entries, local_clash_keys)
        remote_entries = keep_outside(remote_entries, local_clash_keys)
        local_folders = {
            rel: is_dir for rel, is_dir in local_folders.items()
            if held_by_key(rel_key(rel), local_clash_keys) is None
        }
        remote_folders = {
            rel: is_dir for rel, is_dir in remote_folders.items()
            if held_by_key(rel_key(rel), local_clash_keys) is None
        }

    # A Windows key can over-merge non-ASCII names. Those are not safe pairs:
    # hold both paths and every descendant instead of picking a spelling.
    local_inventory = indexed_inventory(local_entries, local_folders)
    remote_inventory = indexed_inventory(remote_entries, remote_folders)
    uncertain_keys = set()
    for key, local_rel in local_inventory.items():
        remote_rel = remote_inventory.get(key)
        if remote_rel is not None and not ascii_pair(local_rel, remote_rel):
            uncertain_keys.add(key)
    if uncertain_keys:
        held_local = [(rel, entry) for rel, entry in local_entries
                      if held_by_key(rel_key(rel), uncertain_keys) is not None]
        held_remote = [(rel, entry) for rel, entry in remote_entries
                       if held_by_key(rel_key(rel), uncertain_keys) is not None]
        record_entries(held_local, "uncertain name match")
        record_entries(held_remote, "uncertain name match")
        local_entries = keep_outside(local_entries, uncertain_keys)
        remote_entries = keep_outside(remote_entries, uncertain_keys)
        local_folders = {
            rel: is_dir for rel, is_dir in local_folders.items()
            if held_by_key(rel_key(rel), uncertain_keys) is None
        }
        remote_folders = {
            rel: is_dir for rel, is_dir in remote_folders.items()
            if held_by_key(rel_key(rel), uncertain_keys) is None
        }

    local_inventory = indexed_inventory(local_entries, local_folders)
    remote_inventory = indexed_inventory(remote_entries, remote_folders)
    canonical_rels = {}

    def canonical_rel(rel):
        canonical = canonical_rels.get(rel)
        if canonical is not None:
            return canonical
        parts = rel.split("/")
        result = []
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            candidate = remote_inventory.get(rel_key(prefix))
            result.append(candidate.split("/")[-1] if candidate else parts[depth - 1])
        canonical = "/".join(result)
        canonical_rels[rel] = canonical
        return canonical

    def local_destination(rel):
        parts = rel.split("/")
        result = []
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            candidate = local_inventory.get(rel_key(prefix))
            result.append(candidate.split("/")[-1] if candidate else parts[depth - 1])
        return os.path.join(local_dir, *result)

    def make_entry(entry, destination_local, destination_remote, actual=True):
        return (*entry, destination_local, destination_remote, actual)

    remote_by_key = {}
    for rel, entry in remote_entries:
        remote_by_key.setdefault(rel_key(rel), []).append((rel, entry))
    used_remote = set()
    for local_rel, local_entry in local_entries:
        candidates = remote_by_key.get(rel_key(local_rel), [])
        pair = next(((remote_rel, remote_entry) for remote_rel, remote_entry in candidates
                     if ascii_pair(local_rel, remote_rel)), None)
        if pair is None:
            canonical = canonical_rel(local_rel)
            local_map[canonical] = make_entry(
                local_entry, local_entry[2], posixpath.join(remote_dir, canonical))
            continue
        remote_rel, remote_entry = pair
        used_remote.add(remote_rel)
        canonical = canonical_rel(remote_rel)
        local_map[canonical] = make_entry(local_entry, local_entry[2], remote_entry[3])
        remote_map[canonical] = make_entry(remote_entry, local_entry[2], remote_entry[3])
    for remote_rel, remote_entry in remote_entries:
        if remote_rel in used_remote:
            continue
        canonical = canonical_rel(remote_rel)
        remote_map[canonical] = make_entry(
            remote_entry, local_destination(canonical), remote_entry[3])

    # Non-empty folders have no scanner entry. Add a virtual folder only
    # where it faces a real file, then remove its descendants from both maps.
    remote_files = {rel_key(rel): (rel, entry) for rel, entry in remote_entries if not entry[4]}
    local_files = {rel_key(rel): (rel, entry) for rel, entry in local_entries if not entry[4]}
    conflict_keys = set()
    for key, local_rel in local_inventory.items():
        remote_file = remote_files.get(key)
        if local_rel in local_folders and remote_file is not None:
            remote_rel, remote_entry = remote_file
            canonical = canonical_rel(remote_rel)
            local_map.setdefault(canonical, make_entry(
                (0, 0, local_destination(local_rel), posixpath.join(remote_dir, canonical), True),
                local_destination(canonical), remote_entry[3], False))
            conflict_keys.add(key)
    for key, remote_rel in remote_inventory.items():
        local_file = local_files.get(key)
        if remote_rel in remote_folders and local_file is not None:
            _local_rel, local_entry = local_file
            canonical = canonical_rel(remote_rel)
            remote_map.setdefault(canonical, make_entry(
                (0, 0, local_entry[2], posixpath.join(remote_dir, remote_rel), True),
                local_entry[2], posixpath.join(remote_dir, canonical), False))
            conflict_keys.add(key)
    if conflict_keys:
        for mapping in (local_map, remote_map):
            for rel in list(mapping):
                if rel_key(rel) not in conflict_keys and held_by_key(rel_key(rel), conflict_keys) is not None:
                    del mapping[rel]

    local_aliases = {}
    for local_rel in local_inventory.values():
        canonical = canonical_rel(local_rel)
        if local_rel != canonical:
            local_aliases[local_rel] = canonical
    local_map.local_aliases = local_aliases
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
    _run_compare drives. Returns canonical remote-preferred result names,
    including ``local_aliases`` for local spellings that differ from those
    names, or None if stop_event fired before the walk finished.

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
    excluded = []
    omissions = {"entries": [], "count": 0}
    local_map, remote_map = api._compute_pair_maps(
        sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event,
        excluded=excluded, omissions=omissions)
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
    return {"files": files, "folders": folders, "local_aliases": local_map.local_aliases,
            "excluded_links": excluded,
            "not_compared": omissions["entries"],
            "not_compared_count": omissions["count"]}


def _compute_sync(api, sftp, local_dir, remote_dir, direction, changed_only=True,
                   on_progress=None, stop_event=None, excluded=None, omissions=None):
    """Pure recursive sync-plan core (see _compute_compare). Returns
    (plan, transfers, conflicts): names use the canonical remote-preferred
    spelling, while transfer tuples retain actual source paths and target an
    existing destination spelling. conflicts lists every file-versus-folder
    rel, including non-empty folders without scanner markers; none of their
    descendants transfer. A plan entry whose local spelling differs from its
    name also carries that spelling as local_name, so the preview can show
    both before the user confirms. Returns (None, None, None) if cancelled."""
    local_map, remote_map = api._compute_pair_maps(
        sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event,
        excluded=excluded, omissions=omissions)
    if local_map is None:
        return None, None, None
    local_spellings = {canonical: local_rel for local_rel, canonical
                       in getattr(local_map, "local_aliases", {}).items()}
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
        entry = {"name": rel, "status": status, "local": local, "remote": remote,
                 "is_dir": is_dir}
        if lentry and rel in local_spellings:
            entry["local_name"] = local_spellings[rel]
        plan.append(entry)
        # The transfer's source side must actually exist to send anything;
        # changed_only=False can otherwise list a status that makes no
        # sense for this direction (e.g. a remote_only file on an
        # upload), which nothing can transfer.
        source = lentry if direction == "upload" else rentry
        if source is not None and source[7]:
            if direction == "upload":
                transfers.append((source[2], source[6], source[0], is_dir))
            else:
                transfers.append((source[5], source[3], source[0], is_dir))
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
            "transfers": None, "not_compared_count": 0, "delivered": False,
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

    def _finish(ok, error=None, result=None, transfers=None, not_compared_count=0):
        with api._compare_lock:
            e = api._compares.get(cid)
            if e is not None:
                e["ok"] = ok
                e["error"] = error
                e["result"] = result
                e["transfers"] = transfers
                e["not_compared_count"] = not_compared_count
                e["done"] = True

    sftp = None
    try:
        try:
            sftp = api._client.open_sftp()
        except Exception as e:
            reason = friendly_error(e)
            api._worker_log(f"compare: could not open a transfer session: {reason}", "error", "compare failed", error=e)
            _finish(False, error=reason)
            return
        on_progress = lambda n: api._bump_compare_found(cid, n)  # noqa: E731
        if kind == "sync":
            excluded = []
            omissions = {"entries": [], "count": 0}
            plan, transfers, conflicts = api._compute_sync(
                sftp, local_dir, remote_dir, direction, changed_only,
                on_progress=on_progress, stop_event=stop_event, excluded=excluded,
                omissions=omissions)
            if plan is None:
                _finish(False, error="Cancelled.")
                return
            with api._compare_lock:
                e = api._compares.get(cid)
                if e is not None:
                    e["direction"] = direction
                    e["local_root"] = local_dir
            total_bytes = sum(t[2] for t in transfers if t[2] and t[2] > 0)
            result = {"count": len(transfers), "total_bytes": total_bytes,
                      "sample": plan[:200], "more": max(0, len(plan) - 200), "token": cid,
                      "conflicts": conflicts, "excluded_links": excluded[:200],
                      "excluded_more": max(0, len(excluded) - 200),
                      "not_compared": omissions["entries"],
                      "not_compared_count": omissions["count"]}
            if excluded:
                api._worker_log(
                    f"{len(excluded)} local link(s) left out of compare/sync: "
                    f"{', '.join(excluded[:200])}", "warn", "link skipped")
            if omissions["count"]:
                api._worker_log(
                    f"{omissions['count']} entries not compared "
                    f"(skipped folders include everything inside them): "
                    f"{', '.join(item['name'] for item in omissions['entries'])}",
                    "warn")
            _finish(True, result=result, transfers=transfers,
                    not_compared_count=omissions["count"])
        else:
            data = api._compute_compare(
                sftp, local_dir, remote_dir, on_progress=on_progress, stop_event=stop_event)
            if data is None:
                _finish(False, error="Cancelled.")
                return
            data["root_local"] = local_dir
            data["root_remote"] = remote_dir
            excluded = data["excluded_links"]
            if excluded:
                api._worker_log(
                    f"{len(excluded)} local link(s) left out of compare/sync: "
                    f"{', '.join(excluded[:200])}", "warn", "link skipped")
            if data["not_compared_count"]:
                api._worker_log(
                    f"{data['not_compared_count']} entries not compared "
                    f"(skipped folders include everything inside them): "
                    f"{', '.join(item['name'] for item in data['not_compared'])}",
                    "warn")
            _finish(True, result=data)
    except ScanIncomplete as e:
        api._worker_log(f"compare: {e}", "error", "compare failed", error=e)
        _finish(False, error=str(e))
    except Exception as e:
        reason = friendly_error(e)
        api._worker_log(f"compare failed: {reason}", "error", "compare failed", error=e)
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


def _stream_sync_transfers(api, transfers, direction, on_conflict, scan_id, stop_event, token,
                           local_root=None, not_compared_count=0):
    """Streams an already-resolved sync-plan transfer list onto the
    normal transfer queue in backpressured batches, reusing the same
    scan registry (and so the same "Scanning… N" / drain UI and
    backpressure) as a folder scan, since there is nothing left to walk.
    The sync plan's entry in _compares was already popped by start_sync()
    before this thread started, so the token argument only identifies
    the job for logging; deregistering it here is a harmless no-op.
    Always deregisters the scan registry entry."""
    found = 0
    clash_count = 0
    batch = []
    try:
        def flush():
            nonlocal batch, found, clash_count
            if not batch:
                return
            api._scan_wait_for_room(stop_event)
            if stop_event.is_set() or not api._connected:
                return
            queued, clashes = api._enqueue_files(batch, direction, on_conflict, local_root=local_root)
            found += queued
            clash_count += clashes
            api._bump_scan_found(scan_id, queued)
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
        suffix = f", {clash_count} name clash(es) not downloaded" if clash_count else ""
        omission_suffix = (f", {not_compared_count} entries not compared"
                           if not_compared_count else "")
        if stop_event.is_set():
            api._worker_log(f"Sync stopped ({found} item(s) queued before stopping{suffix}{omission_suffix})", "warn", "sync", outcome="stopped", items=found)
        elif not api._connected:
            api._worker_log(f"Sync halted: disconnected ({found} item(s) queued{suffix}{omission_suffix})", "warn", "sync", outcome="disconnected", items=found)
        elif clash_count:
            api._worker_log(f"Sync: {found} item(s) queued{suffix}{omission_suffix}", "warn", "sync", outcome="incomplete" if not_compared_count else "complete", items=found)
        elif found:
            if not_compared_count:
                api._worker_log(f"Sync: {found} item(s) queued{omission_suffix}", "warn", "sync", outcome="incomplete", items=found)
            else:
                api._worker_log(f"Sync: {found} item(s) queued", event="sync", outcome="complete", items=found)
        else:
            if not_compared_count:
                api._worker_log(f"Sync: nothing to transfer{omission_suffix}", "warn", "sync", outcome="incomplete", items=0)
            else:
                api._worker_log("Sync: nothing to transfer", event="sync", outcome="complete", items=0)
    except Exception as e:
        api._worker_log(f"sync failed: {friendly_error(e)}", "error", "sync", outcome="failed", items=found, error=e)
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
    local_root = entry.get("local_root")
    not_compared_count = entry.get("not_compared_count", 0)
    scan_id, stop_event = api._register_scan()
    t = threading.Thread(target=api._stream_sync_transfers,
                          args=(transfers, direction, on_conflict, scan_id, stop_event, token, local_root,
                                not_compared_count),
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
