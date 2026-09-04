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


def test_watch_retries_a_failed_upload_instead_of_forgetting_it(sftp_env, wait_until):
    """A failed upload must be retried on a later poll, not dropped. The first
    put raises; the file must still make it to the server on a retry."""
    api, server_root, local_dir = sftp_env
    api._watch_interval = 0.05
    calls = {"n": 0}
    real_put = api.sftp.put

    def flaky_put(localpath, remotepath, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient upload failure")
        return real_put(localpath, remotepath, *a, **k)

    api.sftp.put = flaky_put

    assert api.start_watch(str(local_dir), "/")["ok"] is True
    (local_dir / "retry.txt").write_bytes(b"payload")

    wait_until(lambda: (server_root / "retry.txt").exists())
    api.stop_watch()
    assert calls["n"] >= 2  # failed once, retried
    assert (server_root / "retry.txt").read_bytes() == b"payload"


def test_watch_waits_for_a_file_to_stop_changing_before_uploading(sftp_env, wait_until):
    """A file still being written must not be uploaded as a partial snapshot.
    The file changes again before the stability gate clears, so only its final
    content is ever sent, and only once."""
    api, server_root, local_dir = sftp_env
    uploaded = []
    real_put = api.sftp.put

    def recording_put(localpath, remotepath, *a, **k):
        with open(localpath, "rb") as f:
            uploaded.append(f.read())
        return real_put(localpath, remotepath, *a, **k)

    api.sftp.put = recording_put
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
