"""
Concurrency safety for the shared browsing SFTP session and the upload watcher.

Paramiko's synchronous SFTP is not safe for two callers on one SFTPClient at
once, and pywebview runs each bridge call on its own thread, so browsing calls
(listing, health ping, navigation) must take turns on the shared session. These
tests prove they do, that transfer workers stay independent of that lock, and
that the watcher stops cleanly, retains failed changes, and waits for a file to
stop changing before uploading it.

Built on the same in-process paramiko SFTP server and sftp_env fixture as the
other integration tests.
"""
import os
import threading
import time

import paramiko

from simple_sftp_client import is_temp_part


# ───────────── shared browsing session serialization ─────────────
def test_browsing_calls_never_overlap_on_the_shared_session(sftp_env):
    """Listing and health-ping calls fired from many threads at once must never
    run their underlying SFTP operations concurrently on the one browsing
    session. Instruments the two low-level ops the calls reach (listdir_attr for
    listing, stat for ping) and asserts they are never active at the same time.
    Without the lock, the widened window below would let them overlap."""
    api, server_root, local_dir = sftp_env
    (server_root / "sub").mkdir()
    (server_root / "a.txt").write_bytes(b"a")

    active = {"n": 0, "max": 0}
    track_lock = threading.Lock()
    orig_list = api.sftp.listdir_attr
    orig_stat = api.sftp.stat

    def instrument(orig):
        def wrapped(*a, **k):
            with track_lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            try:
                time.sleep(0.005)  # widen the window so a real overlap is seen
                return orig(*a, **k)
            finally:
                with track_lock:
                    active["n"] -= 1
        return wrapped

    api.sftp.listdir_attr = instrument(orig_list)
    api.sftp.stat = instrument(orig_stat)

    threads = []
    for _ in range(4):
        threads.append(threading.Thread(target=api.list_remote, args=("/",)))
        threads.append(threading.Thread(target=api.list_remote, args=("/sub",)))
        threads.append(threading.Thread(target=api.ping))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert active["max"] == 1


def test_transfers_run_while_the_browsing_lock_is_held(sftp_env, wait_for_drain):
    """A transfer must complete even while another thread holds the browsing
    session lock the whole time, proving workers use their own sessions and are
    not serialized behind browsing. If a worker needed the browsing lock, this
    would deadlock and the drain wait would fail."""
    api, server_root, local_dir = sftp_env
    (local_dir / "w.bin").write_bytes(os.urandom(256 * 1024))

    with api._sftp_lock:
        res = api.enqueue([{"name": "w.bin", "is_dir": False}], "upload",
                          str(local_dir), "/", "overwrite")
        assert res["ok"] is True
        wait_for_drain(api)

    assert (server_root / "w.bin").read_bytes() == (local_dir / "w.bin").read_bytes()


# ───────────── watcher lifecycle ─────────────
def test_watch_start_stop_restart_joins_each_thread(sftp_env):
    """stop_watch must actually retire the running thread and clear state, and a
    restart must be a fresh thread, so an old thread can never act on a new
    run's stop signal."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    t1 = api._watch_thread
    assert t1 is not None and t1.is_alive()

    assert api.stop_watch()["ok"] is True
    assert not t1.is_alive()
    assert api._watch_thread is None and api._watch_stop is None

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    t2 = api._watch_thread
    assert t2 is not t1 and t2.is_alive()

    api.stop_watch()
    assert not t2.is_alive()


def test_rapid_start_stop_never_leaves_a_thread_running(sftp_env):
    """Hammering start/stop must not leave a lingering thread or crash one, which
    the old shared-stop-event teardown could do."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    for _ in range(10):
        assert api.start_watch(str(local_dir), "/")["ok"] is True
        api.stop_watch()
    assert api._watch_thread is None
    assert threading.active_count() < 20  # no pile-up of watcher threads


# ───────────── watcher upload behavior ─────────────
def test_watch_uploads_a_file_that_appears_after_start(sftp_env, wait_until):
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    assert api.start_watch(str(local_dir), "/")["ok"] is True
    (local_dir / "new.txt").write_bytes(b"hello")

    wait_until(lambda: (server_root / "new.txt").exists())
    api.stop_watch()
    assert (server_root / "new.txt").read_bytes() == b"hello"


def test_watch_upload_is_buffered_with_its_remote_folder(sftp_env, wait_until):
    """A completed watch upload reaches the page through poll_queue(), once."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    assert api.poll_queue()["watching"] is True
    (local_dir / "queued.txt").write_bytes(b"queued")

    # The file lands on the server a moment before the watcher records its
    # message and refresh folder, so keep polling until both arrive.
    uploaded = {"msg": "Watch: uploaded queued.txt", "level": "ok"}
    console, refresh = [], []

    def delivered():
        status = api.poll_queue()
        console.extend(status["console"])
        refresh.extend(status["watch_refresh"])
        return uploaded in console and refresh

    wait_until(delivered)
    api.stop_watch()

    assert console.count(uploaded) == 1
    assert refresh == ["/"]
    assert api.poll_queue()["watch_refresh"] == []
    assert api.poll_queue()["watching"] is False


def test_watch_refresh_deduplicates_folders_from_one_pass(sftp_env, wait_until):
    """One watcher pass reports each affected remote folder only once."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    (local_dir / "sub").mkdir()

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    (local_dir / "first.txt").write_bytes(b"first")
    (local_dir / "second.txt").write_bytes(b"second")
    (local_dir / "sub" / "nested.txt").write_bytes(b"nested")

    wait_until(lambda: (server_root / "first.txt").exists()
               and (server_root / "second.txt").exists()
               and (server_root / "sub" / "nested.txt").exists())
    # Refresh folders are recorded at the end of a pass, after the files
    # land, so keep polling until that pass's report arrives. Earlier empty
    # polls are fine; the one report must name each folder once.
    polls = []

    def reported():
        polls.append(api.poll_queue()["watch_refresh"])
        return bool(polls[-1])

    wait_until(reported)
    api.stop_watch()

    assert polls[-1] == ["/", "/sub"]


def test_failed_watch_upload_logs_error_without_refresh(
        sftp_env_no_posix_rename, wait_until):
    """A failed watch upload never tells the page to refresh a remote folder."""
    api, server_root, local_dir = sftp_env_no_posix_rename
    api._watch_interval = 0.05
    (server_root / "failed.txt").write_bytes(b"old")

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    (local_dir / "failed.txt").write_bytes(b"new")

    status = None
    deadline = time.time() + 6
    while time.time() < deadline:
        candidate = api.poll_queue()
        if any(line["msg"].startswith("Watch error: failed.txt - ")
               and line["level"] == "error" for line in candidate["console"]):
            status = candidate
            break
        time.sleep(0.02)
    api.stop_watch()

    assert status is not None
    assert status["watch_refresh"] == []


def test_shutdown_discards_pending_watch_refresh(sftp_env, wait_until):
    """A refresh generated before disconnect cannot reach a later connection."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    (local_dir / "disconnect.txt").write_bytes(b"done")
    wait_until(lambda: (server_root / "disconnect.txt").exists())

    assert api.shutdown()["ok"] is True
    assert api.poll_queue()["watch_refresh"] == []


def test_shutdown_blocks_a_straggling_watcher_from_readding_refresh(
        sftp_env, wait_until):
    """A watcher that misses stop_watch's join timeout cannot re-add a refresh."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    gate_entered = threading.Event()
    release_gate = threading.Event()
    real_lock = api._watch_refresh_lock

    class GateLock:
        def __enter__(self):
            if threading.current_thread() is api._watch_thread:
                gate_entered.set()
                release_gate.wait(5)
            real_lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            real_lock.release()

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    api._watch_refresh_lock = GateLock()
    (local_dir / "late-refresh.txt").write_bytes(b"done")
    wait_until(gate_entered.is_set)
    thread = api._watch_thread

    assert api.shutdown()["ok"] is True
    release_gate.set()
    thread.join(5)

    assert not thread.is_alive()
    assert api.poll_queue()["watch_refresh"] == []


def test_watch_retries_a_failed_upload_instead_of_forgetting_it(sftp_env, wait_until):
    """A failed upload must be retried on a later poll, not dropped. The
    watch upload writes through the safe scratch-file publish, so the
    injected failure raises out of the scratch file's first write; the file
    must still make it to the server on a retry."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    calls = {"n": 0}
    real_write = paramiko.SFTPFile.write

    def flaky_write(self, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient upload failure")
        return real_write(self, data)

    paramiko.SFTPFile.write = flaky_write
    try:
        assert api.start_watch(str(local_dir), "/")["ok"] is True
        (local_dir / "retry.txt").write_bytes(b"payload")

        wait_until(lambda: (server_root / "retry.txt").exists())
        api.stop_watch()
    finally:
        paramiko.SFTPFile.write = real_write
    assert calls["n"] >= 2  # failed once, retried
    assert (server_root / "retry.txt").read_bytes() == b"payload"


def test_watch_waits_for_a_file_to_stop_changing_before_uploading(sftp_env, wait_until):
    """A file still being written must not be uploaded as a partial snapshot.
    The file changes again before the stability gate clears, so only its final
    content is ever sent, and only once. Records the scratch file's bytes right
    before the atomic swap publishes it, since that is the moment the safe
    publish path commits to a given version of the file."""
    api, server_root, local_dir = sftp_env
    uploaded = []
    real_rename = api.sftp.posix_rename

    def recording_rename(oldpath, newpath):
        with api.sftp.open(oldpath, "rb") as f:
            uploaded.append(f.read())
        return real_rename(oldpath, newpath)

    api.sftp.posix_rename = recording_rename
    api._watch_interval = 0.1  # slower than local_watch so we can change it mid-gate

    f = local_dir / "grow.txt"
    assert api.start_watch(str(local_dir), "/")["ok"] is True
    f.write_bytes(b"v1")            # first content: one poll will see it, then wait
    time.sleep(0.15)               # let one poll observe v1 without uploading
    f.write_bytes(b"v2-final-bytes")  # change again before the gate clears

    wait_until(lambda: bool(uploaded), timeout=6)
    time.sleep(0.3)                # give any wrong extra upload a chance to appear
    api.stop_watch()

    assert uploaded == [b"v2-final-bytes"]  # v1 was never sent; sent once, final only
    assert (server_root / "grow.txt").read_bytes() == b"v2-final-bytes"


def test_watch_uploads_a_large_batch_of_changed_files(sftp_env, wait_until):
    """A whole batch of files that changed on the same poll must all make it
    to the server, not just the first one."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    names = [f"batch{i}.txt" for i in range(40)]

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    for n in names:
        (local_dir / n).write_bytes(f"content-{n}".encode())

    wait_until(lambda: all((server_root / n).exists() for n in names), timeout=10)
    api.stop_watch()

    for n in names:
        assert (server_root / n).read_bytes() == f"content-{n}".encode()


def test_watch_does_not_upload_its_own_scratch_files(sftp_env, wait_until):
    """A leftover scratch file dropped into the watched folder must never be
    picked up as a change to upload: the watch snapshot excludes anything
    matching this app's own in-progress-transfer naming shape."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    scratch = local_dir / ".hidden.deadbeef.sxtpart"
    assert is_temp_part(scratch.name)

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    scratch.write_bytes(b"partial data, never meant to be sent")
    # Also upload a real file on the same poll, so we know the watcher is
    # actually running and would have picked up the scratch file if it were
    # going to.
    (local_dir / "real.txt").write_bytes(b"real")

    wait_until(lambda: (server_root / "real.txt").exists())
    time.sleep(0.3)  # give a wrongly-included scratch file a chance to appear
    api.stop_watch()

    assert not (server_root / scratch.name).exists()
    assert [n for n in os.listdir(server_root) if is_temp_part(n)] == []


def test_start_watch_refused_while_previous_watch_thread_still_alive(sftp_env):
    """If a watch thread ignores its stop signal long enough that stop_watch's
    bounded join times out, a following start_watch must be refused rather
    than launching a second thread over the same folder; once the straggler
    finally finishes, start_watch must work again."""
    api, server_root, local_dir = sftp_env

    release = threading.Event()
    stuck_stop = threading.Event()

    def stuck_loop():
        stuck_stop.set()
        release.wait(10)  # ignores the stop signal until told to let go

    stuck_thread = threading.Thread(target=stuck_loop, daemon=True)
    with api._watch_lock:
        api._watch_stop = threading.Event()
        api._watch_thread = stuck_thread
    stuck_thread.start()
    stuck_stop.wait(2)

    # stop_watch's bounded join (3s) will time out against the stuck thread,
    # so use a short one here to keep the test fast: patch join to a tiny
    # timeout for this call only.
    real_join = threading.Thread.join

    def short_join(self, timeout=None):
        return real_join(self, 0.1 if self is stuck_thread else timeout)

    threading.Thread.join = short_join
    try:
        result = api.start_watch(str(local_dir), "/")
    finally:
        threading.Thread.join = real_join

    assert result["ok"] is False
    assert api._watch_thread is stuck_thread  # straggler left recorded, not cleared

    release.set()
    stuck_thread.join(5)
    assert not stuck_thread.is_alive()

    # The straggler has retired now; the next start_watch should notice and
    # proceed normally.
    api._watch_interval = 0.05
    assert api.start_watch(str(local_dir), "/")["ok"] is True
    assert api._watch_thread is not None and api._watch_thread is not stuck_thread
    api.stop_watch()


def test_watch_stop_fired_mid_batch_halts_remaining_items(sftp_env):
    """A stop that fires while a batch of changed files is being uploaded must
    halt the rest of that batch rather than pushing every file through first."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    names = [f"stopme{i}.txt" for i in range(20)]

    started = threading.Event()
    real_ensure = api._ensure_remote_dir

    def slow_ensure(*a, **k):
        started.set()
        time.sleep(0.05)  # give the test time to fire stop mid-batch
        return real_ensure(*a, **k)

    api._ensure_remote_dir = slow_ensure

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    for n in names:
        (local_dir / n).write_bytes(f"content-{n}".encode())
    assert started.wait(5)
    api.stop_watch()

    uploaded = [n for n in names if (server_root / n).exists()]
    assert len(uploaded) < len(names)
