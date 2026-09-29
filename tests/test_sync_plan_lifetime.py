"""
Tests for how long a sync plan token stays alive in api._compares.

A sync_plan() call stashes the full transfer list (and a result summary) on
an entry in api._compares, keyed by the token handed back through
poll_queue()'s compare_done payload. That stash must not outlive its
usefulness: start_sync() consumes it exactly once, an empty plan is dropped
on delivery, a declined or refused plan is dropped by discard_sync(), and a
safety net in sync_plan() itself drops any finished plan still sitting there
before starting a new one (in case the page never got to send a discard,
such as after a reload). These tests check what is left behind in
api._compares, not the 200-row preview cap.
"""
import os


def _put_local(local_dir, name, data, mtime=None):
    p = local_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _put_remote(api, server_root, name, data, mtime=None):
    p = server_root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _make_tree(local_dir, n, prefix="f"):
    for i in range(n):
        _put_local(local_dir, f"{prefix}{i}.txt", os.urandom(32))


def test_repeated_declined_previews_leave_nothing_behind(sftp_env, wait_for_compare):
    """Three large previews in a row, each declined via discard_sync (as the
    UI does when the user says no to the confirmation): _compares ends up
    empty, holding no leftover transfer list."""
    api, server_root, local_dir = sftp_env
    _make_tree(local_dir, 300)

    for _ in range(3):
        r = api.sync_plan(str(local_dir), "/", "upload")
        assert r["ok"] is True
        done = wait_for_compare(api, kind="sync")
        assert done["ok"] is True
        token = done["result"]["token"]
        discard = api.discard_sync(token)
        assert discard["ok"] is True

    assert api._compares == {}


def test_repeated_previews_with_no_discard_keeps_at_most_one(sftp_env, wait_for_compare):
    """If the page never sends a discard (e.g. it was reloaded), sync_plan()'s
    safety net still caps how much is retained: at most one finished sync
    entry survives, never one per run."""
    api, server_root, local_dir = sftp_env
    _make_tree(local_dir, 300)

    for _ in range(3):
        r = api.sync_plan(str(local_dir), "/", "upload")
        assert r["ok"] is True
        wait_for_compare(api, kind="sync")

    with api._compare_lock:
        entries = list(api._compares.values())
    assert len(entries) <= 1
    # whatever is retained must be a finished sync entry, not a stray one
    for e in entries:
        assert e["kind"] == "sync" and e["done"] is True


def test_noop_plan_is_dropped_on_delivery(sftp_env, wait_for_compare):
    """A plan with nothing to transfer and no conflicts (already in sync) is
    freed as soon as poll_queue() delivers it, the same as a plain compare."""
    api, server_root, local_dir = sftp_env
    data = b"identical everywhere"
    _put_local(local_dir, "same.txt", data)
    _put_remote(api, server_root, "same.txt", data)

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    done = wait_for_compare(api, kind="sync")
    assert done["ok"] is True
    assert done["result"]["count"] == 0
    assert not done["result"]["conflicts"]

    assert api._compares == {}


def test_start_sync_consumes_the_plan_immediately(sftp_env, wait_for_compare, wait_for_drain):
    """Normal use: start_sync() queues the transfers (same as
    test_sync_start_transfers_only_changed_and_new_files) and the entry is
    gone from _compares right after start_sync() returns, before the
    transfer even finishes."""
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "new.txt", b"only local, new")

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    done = wait_for_compare(api, kind="sync")
    assert done["ok"] is True
    token = done["result"]["token"]

    start = api.start_sync(token)
    assert start["ok"] is True
    assert start["scanning"] is True

    # gone immediately, no need to wait for the drain
    assert api._compares == {}

    wait_for_drain(api)
    assert (server_root / "new.txt").exists()
    assert (server_root / "new.txt").read_bytes() == b"only local, new"


def test_stale_token_is_refused_and_queues_nothing_extra(sftp_env, wait_for_compare, wait_for_drain):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "new.txt", b"only local, new")

    api.sync_plan(str(local_dir), "/", "upload")
    done = wait_for_compare(api, kind="sync")
    token = done["result"]["token"]

    start1 = api.start_sync(token)
    assert start1["ok"] is True
    # A repeat while the first is still streaming must not queue the files twice.
    repeat = api.start_sync(token)
    assert repeat["ok"] is False
    assert "no longer available" in (repeat["error"] or "")
    wait_for_drain(api)
    before = api._queue.counts()

    start2 = api.start_sync(token)
    assert start2["ok"] is False
    assert "no longer available" in (start2["error"] or "")
    assert api._queue.counts() == before


def test_start_sync_after_discard_is_refused(sftp_env, wait_for_compare):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "new.txt", b"only local, new")

    api.sync_plan(str(local_dir), "/", "upload")
    done = wait_for_compare(api, kind="sync")
    token = done["result"]["token"]

    discard = api.discard_sync(token)
    assert discard["ok"] is True

    start = api.start_sync(token)
    assert start["ok"] is False
    assert "no longer available" in (start["error"] or "")


def test_discard_sync_on_unknown_token_is_ok(sftp_env):
    api, server_root, local_dir = sftp_env
    result = api.discard_sync(999999)
    assert result["ok"] is True


def test_discard_sync_does_not_remove_a_still_running_job(sftp_env):
    api, server_root, local_dir = sftp_env
    cid, _stop_event = api._start_compare("sync")

    result = api.discard_sync(cid)
    assert result["ok"] is True

    with api._compare_lock:
        assert cid in api._compares
        assert api._compares[cid]["done"] is False


def test_disconnect_cleanup_leaves_compares_empty(sftp_env, wait_for_compare):
    api, server_root, local_dir = sftp_env
    _make_tree(local_dir, 50)

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    wait_for_compare(api, kind="sync")
    assert api._compares != {}

    api._shutdown()

    assert api._compares == {}
