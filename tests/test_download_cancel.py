"""Regression tests for bounded, cancellable SFTP downloads."""
import os
import threading
import time

import paramiko

from app.paths import is_temp_part
from app.services import transfer_io

THROTTLED_BYTES_PER_SECOND = 5 * 1024 * 1024
CANCEL_AFTER_BYTES = 5 * 1024 * 1024


def _throttle_sftp_data(monkeypatch, sftp):
    """Limit SFTP DATA replies on one real client connection to 5 MB/s."""
    real_read_packet = paramiko.SFTPClient._read_packet

    def throttled_read_packet(self):
        packet_type, data = real_read_packet(self)
        if self is sftp and packet_type == paramiko.sftp.CMD_DATA:
            time.sleep(max(0, len(data) - 8) / THROTTLED_BYTES_PER_SECOND)
        return packet_type, data

    monkeypatch.setattr(paramiko.SFTPClient, "_read_packet", throttled_read_packet)


def _cancelled_download(api, local_dir, name="cancel.bin"):
    destination = local_dir / name
    progress = []

    def cancel_check():
        return bool(progress) and progress[-1] >= CANCEL_AFTER_BYTES

    started = time.monotonic()
    finished = transfer_io._get_file(
        api, api.sftp, f"/{name}", str(destination),
        lambda got, _total: progress.append(got), cancel_check)
    return finished, time.monotonic() - started, progress, destination


def _prefetch_threads():
    return [
        thread for thread in threading.enumerate()
        if "_prefetch_thread" in thread.name and thread.is_alive()
    ]


def test_cancel_over_throttled_link_finishes_within_bound(sftp_env, monkeypatch):
    api, server_root, local_dir = sftp_env
    name = "cancel.bin"
    (server_root / name).write_bytes(os.urandom(48 * 1024 * 1024))
    _throttle_sftp_data(monkeypatch, api.sftp)

    finished, elapsed, progress, destination = _cancelled_download(api, local_dir, name)

    assert finished is False
    assert progress[-1] >= CANCEL_AFTER_BYTES
    assert elapsed < 6.5
    assert not destination.exists()
    assert not any(is_temp_part(path.name) for path in local_dir.iterdir())


def test_immediate_cancel_then_session_close_leaves_no_prefetch_thread_or_crash(sftp_env):
    # Unthrottled and cancelled on the first chunk, so the download returns
    # while paramiko would still be issuing read-ahead requests for the rest
    # of a large file. The session then closes at once, which is when a
    # still-running read-ahead thread dies with "Socket is closed".
    api, server_root, local_dir = sftp_env
    name = "cancel.bin"
    (server_root / name).write_bytes(os.urandom(64 * 1024 * 1024))
    exceptions = []
    old_excepthook = threading.excepthook
    threading.excepthook = exceptions.append
    try:
        finished = transfer_io._get_file(
            api, api.sftp, f"/{name}", str(local_dir / name),
            lambda _got, _total: None, lambda: True)
        leftover = _prefetch_threads()
        api.sftp.close()
        api.client.close()
        for thread in leftover:
            thread.join(2)
    finally:
        threading.excepthook = old_excepthook

    assert finished is False
    assert leftover == []
    assert exceptions == []


def test_multi_batch_download_is_byte_identical(sftp_env):
    api, server_root, local_dir = sftp_env
    name = "multi-batch.bin"
    data = os.urandom(2 * transfer_io.DOWNLOAD_READV_BATCH_SIZE + 123)
    (server_root / name).write_bytes(data)
    destination = local_dir / name

    finished = transfer_io._get_file(
        api, api.sftp, f"/{name}", str(destination),
        lambda _got, _total: None, lambda: False)

    assert finished is True
    assert destination.read_bytes() == data


def test_zero_byte_download_publishes(sftp_env):
    api, server_root, local_dir = sftp_env
    name = "empty.bin"
    destination = local_dir / name
    destination.write_bytes(b"old content")
    (server_root / name).write_bytes(b"")

    finished = transfer_io._get_file(
        api, api.sftp, f"/{name}", str(destination),
        lambda _got, _total: None, lambda: False)

    assert finished is True
    assert destination.read_bytes() == b""
