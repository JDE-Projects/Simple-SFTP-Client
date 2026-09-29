"""Service functions for the window area."""

import os
import json
import time
from datetime import datetime
from app.debug import debug
from app.errors import friendly_error
from app.paths import exe_dir
from app.prefs import load_prefs, save_prefs


def set_window(api, w):
    api._window = w


def get_meta(api):
    return {
        "version": api._app_version,
        "key_types": ["Ed25519", "RSA-4096"],
        "sessions": api._load_sessions(),
    }


def get_theme(api):
    theme = load_prefs().get("theme")
    return theme if theme in ("dark", "light") else "dark"


def save_theme(api, theme):
    if theme not in ("dark", "light"):
        return {"ok": False}
    prefs = load_prefs()
    prefs["theme"] = theme
    if save_prefs(prefs):
        return {"ok": True}
    return {"ok": False}


def set_debug(api, on):
    ok = debug.set_enabled(on)
    debug.log("Debug enabled" if on and ok else "Debug disabled")
    return {"ok": ok, "enabled": debug.is_enabled(), "warnings": api._drain_debug_warnings()}


def _on_debug_warning(api, msg):
    """debug.on_warning callback: can fire from any thread (a worker's
    log() call, main()'s launch-time prune()...), so it must never touch
    the window. It only buffers; poll_queue()/set_debug()/
    drain_debug_warnings() are what deliver it to the page. The template
    already writes the warning into the log itself, so this must not call
    debug.log() again, or evaluate_js, or it could deadlock or loop."""
    with api._debug_warnings_lock:
        api._debug_warnings.append(msg)


def _drain_debug_warnings(api):
    with api._debug_warnings_lock:
        warnings = api._debug_warnings
        api._debug_warnings = []
    return warnings


def drain_debug_warnings(api):
    # "enabled" lets the page untick the Debug switch after a write
    # failure turned logging off in the background.
    return {"warnings": api._drain_debug_warnings(), "enabled": debug.is_enabled()}


def export_console(api, text):
    """Save the on-screen console to a text file next to the exe."""
    try:
        stamp = datetime.now().strftime("%m%d%Y_%H%M%S")
        path = os.path.join(exe_dir(), f"Console_Log_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("=== Simple SFTP Client console export ===\n")
            f.write(f"Exported: {datetime.now().isoformat()}\n" + "=" * 60 + "\n\n")
            f.write(text or "")
            if text and not text.endswith("\n"):
                f.write("\n")
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def _emit(api, event, payload):
    if api._window:
        try:
            api._window.evaluate_js(
                f"window.appEvent && window.appEvent({json.dumps(event)},{json.dumps(payload)})")
        except Exception:
            pass


def _vlog(api, msg, level="info"):
    """Verbose, FileZilla-style operation line: to the console and debug log."""
    api._emit("console", {"msg": msg, "level": level})
    debug.log(msg)


def _worker_log(api, msg, level="info"):
    """Console line from the worker thread: never calls evaluate_js, just
    buffers for the next poll_queue() and writes the debug log."""
    with api._console_lock:
        api._console_buffer.append({"msg": msg, "level": level})
    debug.log(msg)


def shutdown(api):
    """One idempotent teardown path for Disconnect and window/app close
    alike: stop scans and the watcher, cancel the transfer queue, wait
    (bounded) for worker threads to retire, close the browsing session
    and transport, and clear connection state. Safe to call more than
    once; a second call is a no-op. Reset again by the next successful
    connect() so a later disconnect runs the full sequence again."""
    with api._shutdown_lock:
        if api._shutdown_done:
            return {"ok": True}
        api._shutdown_done = True

    # Halt any background scan promptly: it also checks self._connected on
    # its own, but the explicit stop makes it exit immediately rather than
    # waiting for its next loop check.
    api._stop_all_scans()

    # Same for any background compare/sync job, and drop every stashed
    # sync token: a plan computed against a connection that's gone is no
    # longer safe to hand to start_sync().
    api._stop_all_compares()
    with api._compare_lock:
        api._compares.clear()

    # Stop the watcher; stop_watch joins its thread within a bounded time
    # and logs if it does not retire, rather than just setting the event.
    api.stop_watch()
    with api._watch_refresh_lock:
        api._watch_refresh.clear()

    # Cancel every queued/active transfer so the worker pool drains
    # promptly instead of grinding through retries against a session
    # about to be closed.
    api._queue.cancel_all()
    api._cancel.set()

    # Snapshot the worker threads under the lock, then join outside it:
    # a retiring worker needs this same lock to remove itself from
    # self._workers, so joining while holding it would deadlock.
    with api._worker_lock:
        workers_snapshot = list(api._workers)
    deadline = time.time() + 5  # bounded total wait, not per-thread
    still_alive = []
    for w in workers_snapshot:
        remaining = max(0.0, deadline - time.time())
        w.join(remaining)
        if w.is_alive():
            still_alive.append(w)
    if still_alive:
        # No silent failure: this is not a clean shutdown if threads are
        # still running, so say so instead of claiming otherwise.
        msg = f"Shutdown: {len(still_alive)} transfer thread(s) did not stop in time."
        debug.log("SHUTDOWN: worker thread(s) still running after 5s wait", str(len(still_alive)))
        api._worker_log(msg, "warn")

    # Close the browsing session and transport. Each worker already
    # closes its own SFTP session in its own finally block once
    # cancelled above; this closes the shared session/transport that
    # workers open new sessions against and that the file browser uses.
    close_errors = []
    if api._sftp is not None:
        try:
            api._sftp.close()
        except Exception as e:
            close_errors.append(str(e))
    if api._client is not None:
        try:
            api._client.close()
        except Exception as e:
            close_errors.append(str(e))
    if close_errors:
        debug.log("SHUTDOWN: close error(s)", close_errors)

    api._connected = False
    api._client = None
    api._sftp = None
    api._cred_pass = ""
    api._cred_identity = None
    api._pending_host_key = None
    # This connection's memory of unstamped files does not survive a
    # disconnect: a reconnect starts clean.
    with api._mtime_fallback_lock:
        api._mtime_fallback.clear()
    debug.log("SHUTDOWN complete" if not still_alive else
              "SHUTDOWN complete (worker thread(s) still running)")
    return {"ok": True}


def confirm_quit(api):
    """Called by the page once the user answers yes to the "transfers
    are still running, quit anyway?" prompt raised from main()'s closing
    veto. Marks the close as approved and re-fires the native close,
    which this time goes through (see main()'s _on_closing)."""
    api._quit_confirmed = True
    if api._window is not None:
        try:
            api._window.destroy()
        except Exception:
            pass
    return {"ok": True}
