"""
Tests for the write-to-temp-then-atomic-swap transfer pattern: a cancel, a
dropped connection, or an exhausted retry must never touch the real
destination file, and must never leave a .sxtpart scratch file behind except
while a transfer is actually in flight.

Runs against the in-process SFTP server from conftest.py (sftp_env and the
posix-rename-unsupported variant, sftp_env_no_posix_rename).
"""
import errno
import os
import stat
import time

import paramiko

from transfer_queue import COMPLETED, CANCELLED, FAILED, WAITING

from simple_sftp_client import is_temp_part


def _local_temp_files(folder):
    return [n for n in os.listdir(folder) if is_temp_part(n)]


def _remote_temp_files(server_root):
    return [n for n in os.listdir(server_root) if is_temp_part(n)]


def _wait_until_active(api, item_id, state_of, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state_of(api, item_id)["state"] != WAITING:
            return
        time.sleep(0.02)


def _enqueue_one(api, direction, local_dir, remote_dir, name, on_conflict, wait_for_queue_count):
    before = len(api.queue.snapshot())
    result = api.enqueue([{"name": name, "is_dir": False}], direction,
                          str(local_dir), remote_dir, on_conflict)
    assert result["ok"] is True
    wait_for_queue_count(api, before + 1)
    return api.queue.snapshot()[-1]["id"]


# ───────────── download: cancel keeps the original ─────────────

def test_download_cancel_leaves_existing_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    original = b"O" * 4096
    (local_dir / name).write_bytes(original)
    # big enough that the byte loop is still running when cancel_item lands
    (server_root / name).write_bytes(os.urandom(6 * 1024 * 1024))

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    _wait_until_active(api, item_id, state_of)
    assert api.cancel_item(item_id) == {"ok": True}

    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == CANCELLED
    assert (local_dir / name).read_bytes() == original
    assert _local_temp_files(local_dir) == []


# ───────────── download: exhausted retries keep the original ─────────────

def test_download_retry_exhausted_leaves_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    original = b"O" * 4096
    (local_dir / name).write_bytes(original)
    (server_root / name).write_bytes(os.urandom(64 * 1024))

    # simulate a connection drop partway through every attempt: the progress
    # callback runs mid-stream, so raising there interrupts the byte loop
    # exactly like a real dropped connection would.
    def always_fail(*args, **kwargs):
        raise OSError("simulated dropped connection")
    api._progress = always_fail

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == FAILED
    assert (local_dir / name).read_bytes() == original
    assert _local_temp_files(local_dir) == []


# ───────────── download: success cleans up ─────────────

def test_download_success_writes_correct_bytes_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    data = os.urandom(96 * 1024 + 5)
    (server_root / name).write_bytes(data)

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (local_dir / name).read_bytes() == data
    assert _local_temp_files(local_dir) == []


# ───────────── download: unverified size must not publish ─────────────

def test_download_final_stat_failure_leaves_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """If the remote size cannot be read after the byte loop finishes, the
    download is unverified and must not be published: the existing local file
    stays and the transfer fails, even though every byte arrived. The old code
    swallowed the failed lookup as -1, skipped the size check, and published."""
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    original = b"O" * 4096
    (local_dir / name).write_bytes(original)
    (server_root / name).write_bytes(os.urandom(64 * 1024))

    real_stat = paramiko.SFTPClient.stat
    calls = {"n": 0}

    def flaky_stat(self, path):
        # The pre-transfer stat (first call) succeeds so the download runs;
        # every stat after that fails, like a connection dropped right at the
        # verification step.
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated dropped connection")
        return real_stat(self, path)

    paramiko.SFTPClient.stat = flaky_stat
    try:
        item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.stat = real_stat

    assert state_of(api, item_id)["state"] == FAILED
    assert (local_dir / name).read_bytes() == original
    assert _local_temp_files(local_dir) == []


def test_download_size_mismatch_leaves_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """If the verified remote size does not match the bytes written, the
    download is treated as incomplete and must not be published."""
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    original = b"O" * 4096
    (local_dir / name).write_bytes(original)
    (server_root / name).write_bytes(os.urandom(64 * 1024))

    real_stat = paramiko.SFTPClient.stat

    def oversize_stat(self, path):
        st = real_stat(self, path)
        st.st_size = st.st_size + 100  # claim more bytes than the file has
        return st

    paramiko.SFTPClient.stat = oversize_stat
    try:
        item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.stat = real_stat

    assert state_of(api, item_id)["state"] == FAILED
    assert (local_dir / name).read_bytes() == original
    assert _local_temp_files(local_dir) == []


def test_download_zero_byte_over_existing_publishes(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """A real zero-byte remote file must still publish over an existing local
    file: the size lookup succeeds and returns 0, so verification passes."""
    api, server_root, local_dir = sftp_env
    name = "empty.bin"
    (local_dir / name).write_bytes(b"old content")
    (server_root / name).write_bytes(b"")

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (local_dir / name).read_bytes() == b""
    assert _local_temp_files(local_dir) == []


def test_download_normal_over_existing_publishes(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """A normal download over an existing local file publishes the new bytes
    once the verified size matches."""
    api, server_root, local_dir = sftp_env
    name = "down.bin"
    (local_dir / name).write_bytes(b"old content")
    data = os.urandom(48 * 1024 + 11)
    (server_root / name).write_bytes(data)

    item_id = _enqueue_one(api, "download", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (local_dir / name).read_bytes() == data
    assert _local_temp_files(local_dir) == []


# ───────────── upload: cancel keeps the original ─────────────

def test_upload_cancel_leaves_existing_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    original = b"O" * 4096
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(os.urandom(6 * 1024 * 1024))

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    _wait_until_active(api, item_id, state_of)
    assert api.cancel_item(item_id) == {"ok": True}

    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == CANCELLED
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── upload: exhausted retries keep the original ─────────────

def test_upload_retry_exhausted_leaves_destination_unchanged_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    original = b"O" * 4096
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(os.urandom(64 * 1024))

    def always_fail(*args, **kwargs):
        raise OSError("simulated dropped connection")
    api._progress = always_fail

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == FAILED
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── upload: success cleans up ─────────────

def test_upload_success_writes_correct_bytes_and_no_temp_left(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "up.bin"
    data = os.urandom(64 * 1024 + 37)
    (local_dir / name).write_bytes(data)

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == data
    assert _remote_temp_files(server_root) == []


# ───────────── upload: refuse-and-keep when the server cannot rename ─────────────

def test_upload_refuses_and_keeps_original_when_posix_rename_unsupported(
        sftp_env_no_posix_rename, wait_for_queue_count, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env_no_posix_rename
    name = "up.bin"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(b"new local content that must never land")

    item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
    wait_for_drain(api)

    entry = state_of(api, item_id)
    assert entry["state"] == FAILED
    assert "server does not support safe atomic replace" in entry["error"]
    assert "file not written to protect the existing copy" in entry["error"]
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── upload: preserve the existing target's permissions ─────────────

# These patch paramiko.SFTPClient at the class level rather than on
# api.sftp, because each queue worker opens its own SFTP session
# (self.client.open_sftp()) instead of reusing the browsing session; api.sftp
# is never touched by a transfer. This is the same approach the existing
# flaky_stat/oversize_stat download tests above use.

def test_upload_overwrite_chmods_scratch_to_target_mode_before_rename(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """When an upload overwrites an existing remote file, the scratch file
    must be given that file's standard permission bits before the atomic
    swap, so the published file keeps the same permissions instead of
    picking up the server's default for a brand new file. Windows does not
    store real Unix permission bits, so the existing file's mode is faked by
    stubbing stat, and the chmod call is recorded rather than checked by
    reading real bits back off disk."""
    api, server_root, local_dir = sftp_env
    name = "script.sh"
    (server_root / name).write_bytes(b"OLD CONTENT")
    (local_dir / name).write_bytes(b"NEW CONTENT")
    target_mode = 0o100755  # regular file, rwxr-xr-x
    expected_mode = stat.S_IMODE(target_mode) & 0o777
    rp = f"/{name}"

    real_stat = paramiko.SFTPClient.stat
    real_chmod = paramiko.SFTPClient.chmod
    real_rename = paramiko.SFTPClient.posix_rename
    chmod_calls = []
    rename_calls = []

    def stubbed_stat(self, path):
        if path == rp:
            attr = paramiko.SFTPAttributes()
            attr.st_mode = target_mode
            attr.st_size = 11
            return attr
        return real_stat(self, path)

    def recording_chmod(self, path, mode):
        chmod_calls.append((path, mode))
        return real_chmod(self, path, mode)

    def recording_rename(self, oldpath, newpath):
        rename_calls.append((oldpath, newpath))
        return real_rename(self, oldpath, newpath)

    paramiko.SFTPClient.stat = stubbed_stat
    paramiko.SFTPClient.chmod = recording_chmod
    paramiko.SFTPClient.posix_rename = recording_rename
    try:
        item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.stat = real_stat
        paramiko.SFTPClient.chmod = real_chmod
        paramiko.SFTPClient.posix_rename = real_rename

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == b"NEW CONTENT"
    assert len(chmod_calls) == 1
    chmod_path, chmod_mode = chmod_calls[0]
    assert is_temp_part(os.path.basename(chmod_path))
    assert chmod_mode == expected_mode
    # the chmod on the scratch file must land before it is swapped in
    assert chmod_path == rename_calls[0][0]


def test_upload_new_file_skips_chmod_and_still_publishes(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """A brand new destination has nothing to preserve: the target stat comes
    back ENOENT, so no chmod is issued and the file keeps the server's
    default permissions for a new file, without failing the upload."""
    api, server_root, local_dir = sftp_env
    name = "brand-new.txt"
    (local_dir / name).write_bytes(b"NEW CONTENT")
    rp = f"/{name}"

    real_stat = paramiko.SFTPClient.stat
    real_chmod = paramiko.SFTPClient.chmod
    chmod_calls = []

    def stubbed_stat(self, path):
        if path == rp:
            raise IOError(errno.ENOENT, "no such file")
        return real_stat(self, path)

    def recording_chmod(self, path, mode):
        chmod_calls.append((path, mode))
        return real_chmod(self, path, mode)

    paramiko.SFTPClient.stat = stubbed_stat
    paramiko.SFTPClient.chmod = recording_chmod
    try:
        item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.stat = real_stat
        paramiko.SFTPClient.chmod = real_chmod

    assert state_of(api, item_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == b"NEW CONTENT"
    assert chmod_calls == []


def test_upload_refuses_when_target_permissions_unreadable(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """If the existing target's metadata cannot be read for a reason other
    than the file being absent, the upload must not guess: it is refused
    rather than risk publishing over a file whose permissions are unknown.
    Without the guard this stat failure would either propagate as a generic
    error, or (if the read failure were mistaken for "file absent") the
    upload would silently publish over a file whose real permissions were
    never checked; this asserts the file is refused up front and the
    original is left untouched."""
    api, server_root, local_dir = sftp_env
    name = "secret.conf"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(b"new content that must never land")
    rp = f"/{name}"

    real_stat = paramiko.SFTPClient.stat

    def stubbed_stat(self, path):
        if path == rp:
            raise IOError(errno.EACCES, "denied")
        return real_stat(self, path)

    paramiko.SFTPClient.stat = stubbed_stat
    try:
        item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.stat = real_stat

    entry = state_of(api, item_id)
    assert entry["state"] == FAILED
    assert "could not read the remote file's current permissions" in entry["error"]
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


def test_upload_refuses_when_server_refuses_chmod(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """If the target's permissions can be read but the server refuses to set
    them on the scratch file, the upload must not publish a file with the
    wrong permissions: it is refused and the original stays in place.
    Without the guard the old code path would rename the scratch file into
    place regardless of the failed chmod, silently dropping the target's
    permissions; this asserts the fixed behavior refuses instead."""
    api, server_root, local_dir = sftp_env
    name = "locked.bin"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(b"new content that must never land")

    real_chmod = paramiko.SFTPClient.chmod

    def failing_chmod(self, path, mode):
        raise IOError("server refused chmod")

    paramiko.SFTPClient.chmod = failing_chmod
    try:
        item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.chmod = real_chmod

    entry = state_of(api, item_id)
    assert entry["state"] == FAILED
    assert "server refused to set the target's permissions" in entry["error"]
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


def test_upload_refuses_when_target_reports_no_permissions(
        sftp_env, wait_for_queue_count, wait_for_drain, state_of):
    """A nonstandard server can answer a stat without a permissions field, so
    the target's mode comes back as None. There is nothing to preserve and
    guessing a default could widen a private file, so the upload is refused
    with the same clear message as an unreadable target, and the original
    stays in place. Without the guard the None mode would blow up S_IMODE with
    a bare TypeError instead of this clear refusal."""
    api, server_root, local_dir = sftp_env
    name = "no-perms.conf"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)
    (local_dir / name).write_bytes(b"new content that must never land")
    rp = f"/{name}"

    real_stat = paramiko.SFTPClient.stat

    def stubbed_stat(self, path):
        if path == rp:
            attr = paramiko.SFTPAttributes()
            attr.st_size = len(original)
            attr.st_mode = None  # server omitted the permissions field
            return attr
        return real_stat(self, path)

    paramiko.SFTPClient.stat = stubbed_stat
    try:
        item_id = _enqueue_one(api, "upload", local_dir, "/", name, "overwrite", wait_for_queue_count)
        wait_for_drain(api)
    finally:
        paramiko.SFTPClient.stat = real_stat

    entry = state_of(api, item_id)
    assert entry["state"] == FAILED
    assert "could not read the remote file's current permissions" in entry["error"]
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


# ───────────── listing hides scratch files ─────────────

def test_list_local_hides_temp_part_files(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "real.bin").write_bytes(b"data")
    (local_dir / ".real.bin.deadbeef.sxtpart").write_bytes(b"partial")

    result = api.list_local(str(local_dir))
    assert result["ok"] is True
    names = [e["name"] for e in result["entries"]]
    assert "real.bin" in names
    assert not any(is_temp_part(n) for n in names)


def test_list_remote_hides_temp_part_files(sftp_env):
    api, server_root, local_dir = sftp_env
    (server_root / "real.bin").write_bytes(b"data")
    (server_root / ".real.bin.deadbeef.sxtpart").write_bytes(b"partial")

    result = api.list_remote("/")
    assert result["ok"] is True
    names = [e["name"] for e in result["entries"]]
    assert "real.bin" in names
    assert not any(is_temp_part(n) for n in names)


def test_delete_remote_folder_removes_leftover_scratch_file(sftp_env):
    # A folder holding a leftover scratch file must still delete cleanly:
    # the delete removes the scratch too, so the final rmdir does not fail on
    # a non-empty directory. Hiding scratch files is for listings, never for
    # deletion.
    api, server_root, local_dir = sftp_env
    folder = server_root / "sub"
    folder.mkdir()
    (folder / "real.bin").write_bytes(b"data")
    (folder / ".real.bin.deadbeef.sxtpart").write_bytes(b"partial")

    result = api.delete("remote", "/", [{"name": "sub", "is_dir": True}])
    assert result["ok"] is True
    assert result["errors"] == []
    assert not folder.exists()


# ───────────── watch upload: same safe-replace guarantees ─────────────

def test_watch_upload_interrupted_leaves_prior_copy_and_cleans_scratch(sftp_env, wait_until):
    """A watch upload that dies partway through must never touch the existing
    remote copy, and must never leave its scratch file behind: it uses the
    same write-to-temp-then-atomic-swap publish as the transfer queue."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    name = "keep.bin"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)

    calls = {"n": 0}
    real_write = paramiko.SFTPFile.write

    def flaky_write(self, data):
        # Let the first chunk of the first attempt through so the scratch
        # file actually exists, then fail every write after that so no
        # attempt or retry ever completes.
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated dropped connection")
        return real_write(self, data)

    paramiko.SFTPFile.write = flaky_write
    try:
        assert api.start_watch(str(local_dir), "/")["ok"] is True
        (local_dir / name).write_bytes(os.urandom(64 * 1024))

        wait_until(lambda: calls["n"] >= 2, timeout=6)
        time.sleep(0.2)  # let any in-flight scratch cleanup finish
        api.stop_watch()
    finally:
        paramiko.SFTPFile.write = real_write

    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


def test_watch_upload_preserves_source_modification_time(sftp_env, wait_until):
    """A successful watch upload must stamp the remote file with the local
    source's modification time, the same as a queued upload."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    name = "stamped.txt"

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    f = local_dir / name
    f.write_bytes(b"content")
    os.utime(f, (time.time() - 86400, time.time() - 86400))  # a day old

    wait_until(lambda: (server_root / name).exists())
    time.sleep(0.2)
    api.stop_watch()

    src_mtime = int(os.stat(f).st_mtime)
    dst_mtime = int(api.sftp.stat(f"/{name}").st_mtime)
    assert abs(dst_mtime - src_mtime) <= 1


def test_watch_upload_refuses_and_keeps_original_when_posix_rename_unsupported(
        sftp_env_no_posix_rename, wait_until):
    """A watch upload against a server without the posix-rename extension must
    be refused rather than risking an unsafe replace: the prior remote copy
    stays intact and the change is left pending so it is retried."""
    api, server_root, local_dir = sftp_env_no_posix_rename
    api._watch_interval = 0.05
    name = "up.bin"
    original = b"ORIGINAL SERVER COPY"
    (server_root / name).write_bytes(original)

    events = []
    api._emit = lambda ev, payload: events.append((ev, payload))

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    (local_dir / name).write_bytes(b"new local content that must never land")

    wait_until(lambda: any(ev == "watch" and not p["ok"] for ev, p in events), timeout=6)
    time.sleep(0.2)
    api.stop_watch()

    watch_events = [p for ev, p in events if ev == "watch"]
    assert all(not p["ok"] for p in watch_events)  # never reported success
    assert (server_root / name).read_bytes() == original
    assert _remote_temp_files(server_root) == []


def test_compare_hides_temp_part_files_on_both_sides(sftp_env):
    api, server_root, local_dir = sftp_env
    (local_dir / "same.bin").write_bytes(b"AAAA")
    (server_root / "same.bin").write_bytes(b"AAAA")
    (local_dir / ".same.bin.deadbeef.sxtpart").write_bytes(b"scratch")
    (server_root / ".other.bin.cafebabe.sxtpart").write_bytes(b"scratch")

    data = api._compute_compare(api.sftp, str(local_dir), "/")
    assert data is not None
    assert not any(is_temp_part(n) for n in data["files"])
    assert data["files"]["same.bin"] == "same"
