"""
Tests for the recursive compare/sync core (_compute_compare/_compute_sync)
and the async, poll_queue()-based delivery contract that compare()/
sync_plan()/start_sync() use instead of blocking the pywebview bridge
thread. Runs against the in-process SFTP server and fixtures from
conftest.py.
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


# ───────────── _compute_compare: pure recursive core ─────────────

def test_recursive_compare_finds_a_deep_difference(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "top/mid/deep/changed.txt", b"local version")
    _put_remote(api, server_root, "top/mid/deep/changed.txt", b"remote ver")

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert data is not None
    assert data["files"]["top/mid/deep/changed.txt"] in ("newer_local", "newer_remote")


def test_ancestor_folders_of_a_deep_change_are_marked_has_changes(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "top/mid/deep/changed.txt", b"local version")
    _put_remote(api, server_root, "top/mid/deep/changed.txt", b"remote ver")

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert data["folders"]["top"] == "has_changes"
    assert data["folders"]["top/mid"] == "has_changes"
    assert data["folders"]["top/mid/deep"] == "has_changes"


def test_all_identical_nested_tree_yields_no_changes_and_no_flagged_folders(sftp_env):
    api, server_root, local_dir = sftp_env
    data_bytes = b"identical nested content"
    _put_local(local_dir, "top/mid/same.txt", data_bytes)
    _put_remote(api, server_root, "top/mid/same.txt", data_bytes)

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert all(status == "same" for status in data["files"].values())
    assert data["folders"] == {}


# ───────────── _classify: size AND mtime, with a tolerance ─────────────

def test_equal_size_and_equal_mtime_is_same(sftp_env):
    api, server_root, local_dir = sftp_env
    mtime = 1_700_000_000
    _put_local(local_dir, "match.txt", b"identical bytes", mtime=mtime)
    _put_remote(api, server_root, "match.txt", b"identical bytes", mtime=mtime)

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert data["files"]["match.txt"] == "same"


def test_equal_size_but_mtimes_far_apart_is_not_same(sftp_env):
    api, server_root, local_dir = sftp_env
    base = 1_700_000_000
    # same size, different content, mtimes well beyond the 2s tolerance
    _put_local(local_dir, "edited.txt", b"local edit bytes", mtime=base + 3600)
    _put_remote(api, server_root, "edited.txt", b"remote edit bytes", mtime=base)

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert data["files"]["edited.txt"] == "newer_local"


def test_equal_size_but_remote_mtime_newer_is_newer_remote(sftp_env):
    api, server_root, local_dir = sftp_env
    base = 1_700_000_000
    _put_local(local_dir, "edited.txt", b"local edit bytes", mtime=base)
    _put_remote(api, server_root, "edited.txt", b"remote edit bytes", mtime=base + 3600)

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert data["files"]["edited.txt"] == "newer_remote"


def test_equal_size_and_mtimes_within_tolerance_is_same(sftp_env):
    api, server_root, local_dir = sftp_env
    base = 1_700_000_000
    _put_local(local_dir, "close.txt", b"same length!", mtime=base)
    _put_remote(api, server_root, "close.txt", b"same length!", mtime=base + 2)

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    assert data["files"]["close.txt"] == "same"


def test_ascii_case_pair_compare_uses_remote_name_and_local_alias(sftp_env):
    api, server_root, local_dir = sftp_env
    mtime = 1_700_000_000
    _put_local(local_dir, "report.txt", b"same", mtime=mtime)
    _put_remote(api, server_root, "Report.txt", b"same", mtime=mtime)

    data = api._compute_compare(api._sftp, str(local_dir), "/")

    assert data["files"] == {"Report.txt": "same"}
    assert data["local_aliases"] == {"report.txt": "Report.txt"}


def test_paired_folder_uses_remote_spelling_for_local_only_child(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "Docs/new.txt", b"new")
    _put_remote(api, server_root, "docs/other.txt", b"other")

    data = api._compute_compare(api._sftp, str(local_dir), "/")
    _plan, transfers, _conflicts = api._compute_sync(
        api._sftp, str(local_dir), "/", "upload")

    assert data["local_aliases"] == {"Docs": "docs", "Docs/new.txt": "docs/new.txt"}
    assert data["folders"]["docs"] == "has_changes"
    assert (str(local_dir / "Docs" / "new.txt"), "/docs/new.txt", 3, False) in transfers


# ───────────── async contract: compare()/sync_plan()/start_sync() over poll_queue() ─────────────

def test_compare_delivers_result_via_poll_queue(sftp_env, wait_for_compare):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "new.txt", b"hello")

    r = api.compare(str(local_dir), "/")
    assert r["ok"] is True
    assert r["comparing"] is True

    done = wait_for_compare(api, kind="compare")
    assert done["ok"] is True
    assert done["result"]["files"]["new.txt"] == "local_only"
    # one-shot: delivered once, then the job is gone
    status = api.poll_queue()
    assert status["compare_done"] is None
    assert status["comparing"] is False


def test_sync_start_transfers_only_changed_and_new_files(sftp_env, wait_for_compare, wait_for_drain):
    api, server_root, local_dir = sftp_env
    data = b"identical nested bytes"
    _put_local(local_dir, "top/same.txt", data)
    _put_remote(api, server_root, "top/same.txt", data)
    _put_local(local_dir, "top/new.txt", b"only local, new")

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    assert r["comparing"] is True

    done = wait_for_compare(api, kind="sync")
    assert done["ok"] is True
    result = done["result"]
    assert result["count"] == 1
    token = result["token"]

    start = api.start_sync(token)
    assert start["ok"] is True
    assert start["scanning"] is True

    wait_for_drain(api)

    assert (server_root / "top" / "new.txt").exists()
    assert (server_root / "top" / "new.txt").read_bytes() == b"only local, new"
    # the identical nested file was never re-sent: its remote content is
    # untouched (still the original bytes, not overwritten)
    assert (server_root / "top" / "same.txt").read_bytes() == data


def test_start_sync_queues_ascii_case_pair_at_existing_remote_name(
        sftp_env, wait_for_compare, wait_for_queue_count):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "report.txt", b"local", mtime=1_700_003_600)
    _put_remote(api, server_root, "Report.txt", b"remote", mtime=1_700_000_000)

    assert api.sync_plan(str(local_dir), "/", "upload")["ok"] is True
    done = wait_for_compare(api, kind="sync")
    assert api.start_sync(done["result"]["token"])["ok"] is True
    wait_for_queue_count(api, 1)

    assert api._queue._items[0].remote_path.endswith("/Report.txt")


def test_compare_or_sync_refuses_while_one_is_already_running(sftp_env, wait_for_compare):
    api, server_root, local_dir = sftp_env
    for i in range(50):
        _put_local(local_dir, f"f{i}.txt", os.urandom(64))

    r = api.compare(str(local_dir), "/")
    assert r["ok"] is True
    r2 = api.sync_plan(str(local_dir), "/", "upload")
    assert r2["ok"] is False
    assert "already running" in (r2["error"] or "")

    # drain the first job so it doesn't leak into later tests
    wait_for_compare(api, kind="compare")
