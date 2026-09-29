"""Service functions for the transfer_io area."""

import os
import stat
import errno
import posixpath
from app.errors import friendly_error
from app.paths import local_temp_path, remote_temp_path


DOWNLOAD_READV_BATCH_SIZE = 16 * 1024 * 1024


def _put_file(api, sftp, lp, rp, cb, cancel_check):
    # Writes into a scratch file next to the real remote destination
    # (never rp itself), so the destination is only touched once the new
    # copy is proven complete. On success the scratch file is confirmed
    # to be the right size. If this upload is overwriting an existing
    # file, the scratch file is given that file's standard permission
    # bits before the swap, so the published file keeps the same
    # permissions instead of picking up the server's default for a new
    # file. A brand new destination (nothing to overwrite) just keeps
    # the server default. If the existing file's permissions cannot be
    # read, or the server refuses to set them on the scratch file, the
    # upload is refused rather than publishing a file with the wrong
    # permissions; the existing file is left in place. Once the
    # permissions are settled the scratch file is swapped in with
    # posix_rename, which is atomic: a cancel, dropped connection, or
    # exhausted retry can only ever leave the scratch file behind, never
    # a half-written rp.
    #
    # Check cancel_check() only once there is another chunk actually to
    # send, and only after confirming there is more file left (the read
    # came back non-empty). That way a cancel arriving in the instant
    # right after the last real chunk was already written finds nothing
    # left to abort: the next read hits EOF first and the loop exits with
    # finished=True, so a fully-sent file is never mislabeled cancelled.
    temp = remote_temp_path(rp)
    finished = True
    published = False
    try:
        with open(lp, "rb") as src:
            with sftp.open(temp, "w") as dst:
                dst.set_pipelined(True)
                sent = 0
                while True:
                    chunk = src.read(32768)
                    if not chunk:
                        break
                    if cancel_check():
                        finished = False
                        break
                    dst.write(chunk)
                    sent += len(chunk)
                    cb(sent, 0)
        if finished:
            src_size = os.path.getsize(lp)
            temp_size = sftp.stat(temp).st_size
            if temp_size != src_size:
                raise IOError(
                    f"upload incomplete: wrote {temp_size} of {src_size} bytes")
            try:
                existing_attr = sftp.stat(rp)
            except Exception as e:
                if getattr(e, "errno", None) == errno.ENOENT:
                    existing_attr = None
                else:
                    raise IOError(
                        "could not read the remote file's current "
                        f"permissions ({e}); existing file left in "
                        "place to protect its permissions") from e
            if existing_attr is not None:
                if existing_attr.st_mode is None:
                    raise IOError(
                        "could not read the remote file's current "
                        "permissions (the server did not report them); "
                        "existing file left in place to protect its "
                        "permissions")
                mode = stat.S_IMODE(existing_attr.st_mode) & 0o777
                try:
                    sftp.chmod(temp, mode)
                except Exception as e:
                    raise IOError(
                        "server refused to set the target's permissions "
                        "on the new file; upload refused to protect the "
                        "existing copy") from e
            try:
                sftp.posix_rename(temp, rp)
            except Exception as e:
                raise IOError(
                    "server does not support safe atomic replace; "
                    "file not written to protect the existing copy") from e
            published = True
            api._apply_upload_mtime(sftp, rp, lp)
        return finished
    finally:
        # Anything other than a proven, published swap leaves nothing
        # behind: delete the scratch file (best effort) and, on an
        # exception, let it propagate so the retry loop tries again with
        # a brand new scratch file.
        if not published:
            try:
                sftp.remove(temp)
            except Exception:
                pass


def _get_file(api, sftp, rp, lp, cb, cancel_check):
    # Same idea as _put_file: stream into a local scratch file next to
    # the real destination, confirm its size once the loop finishes, then
    # publish with os.replace, which is atomic on the same drive. A
    # cancel, dropped connection, or exhausted retry only ever leaves the
    # scratch file behind; the real destination is never opened for
    # writing until the new copy is proven complete.
    #
    # Downloads use bounded readv batches instead of unlimited prefetch.
    # Batching keeps a cancel's drain-and-close latency bounded: a bigger
    # batch needs fewer idle round trips, but drains more data on cancel.
    # As with _put_file, a cancel only interrupts if a real chunk remains
    # to be written when it is observed.
    temp = local_temp_path(lp)
    finished = True
    published = False
    try:
        # Read the size once before downloading so every readv request is an
        # exact 32 KB chunk within the known remote extent.
        try:
            expected_size = sftp.stat(rp).st_size
        except Exception as e:
            raise IOError(
                f"download not verified: could not read the remote "
                f"file size ({e}); existing file left in place") from e
        with sftp.open(rp, "r") as src:
            with open(temp, "wb") as dst:
                got = 0
                batch_start = 0
                while batch_start < expected_size:
                    if cancel_check():
                        finished = False
                        break
                    batch_end = min(
                        batch_start + DOWNLOAD_READV_BATCH_SIZE, expected_size)
                    chunks = [
                        (chunk_start, min(32768, batch_end - chunk_start))
                        for chunk_start in range(batch_start, batch_end, 32768)
                    ]
                    cancelled = False
                    for chunk in src.readv(chunks):
                        if not cancelled and chunk and cancel_check():
                            finished = False
                            cancelled = True
                        if not cancelled and chunk:
                            dst.write(chunk)
                            got += len(chunk)
                            cb(got, 0)
                    # readv starts Paramiko's prefetch thread on its first
                    # next(). Drain the generator after a cancel so that
                    # thread has stopped issuing reads before src closes.
                    if cancelled:
                        break
                    batch_start = batch_end
        if finished:
            # Confirm the remote size directly rather than through _rstat,
            # which hides a failed lookup as -1. A -1 there would skip the
            # size check entirely and publish an unverified download. Any
            # failure to read the size (source gone, permission denied,
            # connection dropped) must raise so the retry loop retries and,
            # if it keeps failing, leaves the existing local file untouched.
            # A genuine zero-byte file still stats fine and publishes.
            try:
                src_stat = sftp.stat(rp)
            except Exception as e:
                raise IOError(
                    f"download not verified: could not read the remote "
                    f"file size ({e}); existing file left in place") from e
            src_size = src_stat.st_size
            src_mtime = int(src_stat.st_mtime or 0)
            temp_size = os.path.getsize(temp)
            if temp_size != src_size:
                raise IOError(
                    f"download incomplete: wrote {temp_size} of {src_size} bytes")
            os.replace(temp, lp)
            published = True
            api._apply_download_mtime(lp, rp, src_mtime, src_size)
        return finished
    finally:
        if not published:
            try:
                os.remove(temp)
            except Exception:
                pass


def _mtime_fallback_key(api, lp, rp):
    """Build the one normalized key every record/lookup/clear of
    self._mtime_fallback must use, so the two sides never drift apart and
    silently stop matching. Local paths are normalized for case and made
    absolute; remote paths are normalized as posix paths."""
    return (os.path.normcase(os.path.abspath(lp)), posixpath.normpath(rp))


def _record_mtime_fallback(api, lp, rp, size):
    key = api._mtime_fallback_key(lp, rp)
    with api._mtime_fallback_lock:
        api._mtime_fallback[key] = size


def _clear_mtime_fallback(api, lp, rp):
    key = api._mtime_fallback_key(lp, rp)
    with api._mtime_fallback_lock:
        api._mtime_fallback.pop(key, None)


def _mtime_fallback_matches(api, lp, rp, size):
    """True only if this (local, remote) pair was remembered as having
    failed its time stamp on this connection, and the size given (the
    caller has already confirmed both sides' current sizes are equal)
    still matches the size recorded at transfer time. A size mismatch
    means the file has genuinely changed since, so the stale entry is
    dropped rather than kept around to answer False forever."""
    key = api._mtime_fallback_key(lp, rp)
    with api._mtime_fallback_lock:
        recorded = api._mtime_fallback.get(key)
        if recorded is None:
            return False
        if recorded == size:
            return True
        del api._mtime_fallback[key]
        return False


def _apply_download_mtime(api, lp, rp, mtime, size):
    """Stamp a freshly downloaded local file with the remote file's
    modification time, so a later size+mtime compare reads an
    unchanged file as 'same'. A mtime of 0 means the server's reply
    carried no modification time; the file keeps its own time. If there
    is no time to set, or setting it fails, the file is remembered for
    the rest of this connection: a later compare or skip check that
    finds a matching size will still treat it as unchanged, even though
    its time does not match. This memory does not survive a
    disconnect/reconnect."""
    if not mtime:
        api._worker_log(f"server reported no modification time for {rp}; "
                         "this file will be treated as unchanged by size "
                         "for the rest of this connection", "warn")
        api._record_mtime_fallback(lp, rp, size)
        return
    try:
        os.utime(lp, (mtime, mtime))
    except OSError as e:
        api._worker_log(f"could not set modification time on {lp}: {e}; "
                         "this file will be treated as unchanged by size "
                         "for the rest of this connection", "warn")
        api._record_mtime_fallback(lp, rp, size)
    else:
        api._clear_mtime_fallback(lp, rp)


def _apply_upload_mtime(api, sftp, rp, lp):
    """Stamp an uploaded remote file with the local source's modification
    time. If the server refuses to set the time, the file is
    remembered for the rest of this connection: a later compare or skip
    check that finds a matching size will still treat it as unchanged,
    even though its time does not match. This memory does not survive a
    disconnect/reconnect."""
    try:
        local_stat = os.stat(lp)
        mtime = int(local_stat.st_mtime)
    except OSError:
        return
    try:
        sftp.utime(rp, (mtime, mtime))
    except Exception as e:
        api._worker_log(f"server refused to set modification time on {rp}: "
                         f"{friendly_error(e)}; this file will be treated "
                         "as unchanged by size for the rest of this "
                         "connection", "warn")
        api._record_mtime_fallback(lp, rp, local_stat.st_size)
    else:
        api._clear_mtime_fallback(lp, rp)
