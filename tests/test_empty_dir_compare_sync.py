"""
Tests for commit 2 of the empty-dir-transfers work: compare and sync now see
empty folders (the markers _iter_local/_iter_remote already emit, per
test_empty_dir_transfers.py's commit 1), and a file-vs-folder name clash at
the same relative path is classified as "conflict", shown in both the files
and folders side of a compare result, and excluded from a sync's transfers
while being reported back to the caller.

Runs against the same in-process paramiko SFTP server and sftp_env/
wait_for_drain fixtures as test_compare.py and test_sync_plan.py.
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


def _by_name(plan):
    return {p["name"]: p for p in plan}


def _sync(api, local_dir, direction, changed_only=True):
    return api._compute_sync(api.sftp, str(local_dir), "/", direction, changed_only)


# ───────────── compare: empty folders ─────────────

def test_local_only_empty_folder_appears_in_folders_map(sftp_env):
    api, _server_root, local_dir = sftp_env
    (local_dir / "empty_local").mkdir()

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert data["folders"]["empty_local"] == "local_only"
    assert "empty_local" not in data["files"]


def test_remote_only_empty_folder_appears_in_folders_map(sftp_env):
    api, server_root, local_dir = sftp_env
    (server_root / "empty_remote").mkdir()

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert data["folders"]["empty_remote"] == "remote_only"
    assert "empty_remote" not in data["files"]


def test_empty_folder_on_both_sides_is_same_and_not_reported(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "both_empty").mkdir()
    (server_root / "both_empty").mkdir()

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert "both_empty" not in data["folders"]
    assert "both_empty" not in data["files"]


def test_nested_empty_folders_are_represented(sftp_env):
    api, _server_root, local_dir = sftp_env
    (local_dir / "top" / "mid" / "leaf").mkdir(parents=True)

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert data["folders"]["top/mid/leaf"] == "local_only"


def test_compared_root_itself_never_appears_in_either_map(sftp_env):
    api, _server_root, local_dir = sftp_env
    # both sides of the compared root are empty: "." must never be a key
    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert "." not in data["files"]
    assert "." not in data["folders"]


# ───────────── sync: empty folders ─────────────

def test_upload_of_one_sided_local_empty_folder_creates_it_on_remote(sftp_env, wait_for_compare, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "up_empty").mkdir()

    plan, transfers, conflicts = _sync(api, local_dir, "upload")
    by_name = _by_name(plan)
    assert by_name["up_empty"]["status"] == "local_only"
    assert by_name["up_empty"]["is_dir"] is True
    assert conflicts == []
    matching = [t for t in transfers if t[1] == "/up_empty"]
    assert len(matching) == 1
    assert matching[0][3] is True  # is_dir

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    done = wait_for_compare(api, kind="sync")
    start = api.start_sync(done["result"]["token"])
    assert start["ok"] is True
    wait_for_drain(api)
    assert (server_root / "up_empty").is_dir()


def test_download_of_one_sided_remote_empty_folder_creates_it_locally(sftp_env, wait_for_compare, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (server_root / "dn_empty").mkdir()

    plan, transfers, conflicts = _sync(api, local_dir, "download")
    by_name = _by_name(plan)
    assert by_name["dn_empty"]["status"] == "remote_only"
    assert by_name["dn_empty"]["is_dir"] is True
    assert conflicts == []
    matching = [t for t in transfers if t[1] == "/dn_empty"]
    assert len(matching) == 1
    assert matching[0][3] is True  # is_dir

    r = api.sync_plan(str(local_dir), "/", "download")
    assert r["ok"] is True
    done = wait_for_compare(api, kind="sync")
    start = api.start_sync(done["result"]["token"])
    assert start["ok"] is True
    wait_for_drain(api)
    assert (local_dir / "dn_empty").is_dir()


def test_folder_already_existing_on_destination_still_ends_fine(sftp_env, wait_for_compare, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (local_dir / "already").mkdir()
    (server_root / "already").mkdir()  # already there: same on both sides, so "same", not queued

    plan, transfers, conflicts = _sync(api, local_dir, "upload")
    assert "already" not in _by_name(plan)
    assert conflicts == []
    assert not any(t[1] == "/already" for t in transfers)

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    done = wait_for_compare(api, kind="sync")
    # nothing to transfer, but the token is still usable and the folder is
    # still there afterwards (idempotent, via commit 1's _make_dir)
    start = api.start_sync(done["result"]["token"])
    assert start["ok"] is True
    wait_for_drain(api)
    assert (server_root / "already").is_dir()


# ───────────── file-vs-folder name clash ─────────────

def test_file_on_one_side_folder_on_other_classifies_as_conflict(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "clash", b"a local file")
    (server_root / "clash").mkdir()

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert data["files"]["clash"] == "conflict"
    assert data["folders"]["clash"] == "conflict"


def test_conflict_excluded_from_sync_transfers_and_listed_in_conflicts(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "clash", b"a local file")
    (server_root / "clash").mkdir()

    plan, transfers, conflicts = _sync(api, local_dir, "upload")
    assert "clash" not in _by_name(plan)
    assert not any(t[1] == "/clash" for t in transfers)
    assert conflicts == [{"name": "clash"}]

    plan, transfers, conflicts = _sync(api, local_dir, "download")
    assert "clash" not in _by_name(plan)
    assert not any(t[1] == "/clash" for t in transfers)
    assert conflicts == [{"name": "clash"}]


def test_conflict_survives_a_sync_run_untouched(sftp_env, wait_for_compare, wait_for_drain):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "clash", b"a local file")
    (server_root / "clash").mkdir()

    r = api.sync_plan(str(local_dir), "/", "upload")
    assert r["ok"] is True
    done = wait_for_compare(api, kind="sync")
    result = done["result"]
    assert result["conflicts"] == [{"name": "clash"}]

    start = api.start_sync(result["token"])
    assert start["ok"] is True
    wait_for_drain(api)

    # nothing overwritten: the local file and the remote folder are untouched
    assert (local_dir / "clash").is_file()
    assert (local_dir / "clash").read_bytes() == b"a local file"
    assert (server_root / "clash").is_dir()


# ───────────── regression guard: a file-only tree behaves as before ─────────────

def test_file_only_compare_and_sync_are_unaffected(sftp_env):
    api, server_root, local_dir = sftp_env
    _put_local(local_dir, "top/new.txt", b"only local, new")
    data_bytes = b"identical bytes"
    _put_local(local_dir, "top/same.txt", data_bytes)
    _put_remote(api, server_root, "top/same.txt", data_bytes)

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert data["files"]["top/new.txt"] == "local_only"
    assert data["files"]["top/same.txt"] == "same"
    assert data["folders"]["top"] == "has_changes"

    plan, transfers, conflicts = _sync(api, local_dir, "upload")
    by_name = _by_name(plan)
    assert by_name["top/new.txt"]["status"] == "local_only"
    assert by_name["top/new.txt"]["is_dir"] is False
    assert "top/same.txt" not in by_name
    assert conflicts == []
    assert transfers == [
        (str(local_dir / "top" / "new.txt"), "/top/new.txt", len(b"only local, new"), False),
    ]
