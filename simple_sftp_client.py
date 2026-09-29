"""
Simple SFTP Client
A clean dual-pane SFTP client: connect to a server, browse local and remote
side by side, transfer with a background queue, compare/sync folders, watch a
local folder for auto-upload, generate keys, and manage saved sessions.

Secure algorithms only (no weak/CVE'd fallbacks): if a server cannot negotiate
a modern algorithm set, the connection fails with a clear message rather than
downgrading.

Backend: paramiko. Saved sessions: servers.json next to the exe (no passwords).
Optional "remember password" uses the OS keychain via keyring. Window:
pywebview on the Qt backend, UI in simple_sftp_client-UI.html.

Built with AI assistance, directed by JDE-Projects.
"""

import os
import sys
import shlex
import ctypes
from ctypes import wintypes
import threading

import webview

from app.api import Api
from app.debug import debug
from app.geometry import _restore_geometry, _save_geometry
from app.paths import resource_path


APP_VERSION = "1.9.3"


def _is_remote_debugging_switch(token):
    # Chromium on Windows accepts "--", "-" or "/" before a switch name and
    # ignores its case, so every spelling is matched.
    for prefix in ("--", "-", "/"):
        if token.startswith(prefix):
            return token[len(prefix):].lower().startswith("remote-debugging-")
    return False


def _drop_remote_debugging(tokens):
    """Return tokens without remote-debugging switches, including a value
    given as the following token (--remote-debugging-port 9222)."""
    kept = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if _is_remote_debugging_switch(token):
            if "=" not in token and index < len(tokens) and not tokens[index].startswith(("-", "/")):
                index += 1
        else:
            kept.append(token)
    return kept


def strip_remote_debugging(environ, argv, frozen):
    """Remove Qt remote-debugging controls from frozen application launches,
    so the built app never opens Qt's remote-control port. Source runs are
    left alone: the real-window smoke check depends on that port."""
    if not frozen:
        return

    environ.pop("QTWEBENGINE_REMOTE_DEBUGGING", None)

    flags = environ.get("QTWEBENGINE_CHROMIUM_FLAGS")
    if flags is not None:
        try:
            lexer = shlex.shlex(flags, posix=False)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            # Unbalanced quote: split on spaces rather than fail at startup.
            tokens = flags.split()
        kept = _drop_remote_debugging(tokens)
        if kept:
            environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(kept)
        else:
            del environ["QTWEBENGINE_CHROMIUM_FLAGS"]

    argv[1:] = _drop_remote_debugging(argv[1:])




# ───────────── main ─────────────
_mutex_handle = None   # module-level: must live for the process lifetime


def _acquire_single_instance(mutex_name: str) -> bool:
    # Name convention: "JDE_Simple{Thing}Tool_SingleInstance"
    # Session-local (no "Global\" prefix): each Windows session (e.g. RDP,
    # fast user switching) gets its own instance instead of colliding across users.
    global _mutex_handle
    try:
        # use_last_error=True: ctypes.windll's GetLastError() can be clobbered
        # by ctypes-internal calls, so read the error via ctypes.get_last_error() instead.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return True   # fail open: never block launch over a mutex error


def _focus_existing_window(title: str) -> None:
    """Best-effort: bring an already-running instance's window to the
    foreground when a second launch is refused. Enumerates top-level windows
    (the mirror image of _own_window_handle: this one keeps only a window
    NOT owned by this process), restores it if minimized, then asks Windows
    to foreground it. SetForegroundWindow can silently no-op under Windows'
    foreground-lock rules if this process never had focus; that is accepted
    as-is rather than fought with input-attach hacks. Wrapped end to end so
    any failure just means the second process exits quietly with no window."""
    try:
        u = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        # Type the handle-taking calls too: a window handle can exceed a
        # signed 32-bit int, and ctypes' untyped default would raise on it,
        # silently defeating the focus. Same convention as _save_geometry.
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL

        own_pid = os.getpid()
        found = {"hwnd": None}

        def _callback(hwnd, lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return True   # this process, not the already-running one
            length = u.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value != title:
                return True
            found["hwnd"] = hwnd
            return False   # stop enumerating, we found it

        proc = WNDENUMPROC(_callback)   # kept alive for the duration of the call below
        u.EnumWindows(proc, 0)
        hwnd = found["hwnd"]
        if not hwnd:
            return
        SW_RESTORE = 9
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main():
    strip_remote_debugging(os.environ, sys.argv, getattr(sys, "frozen", False))

    # Use the Windows certificate store for TLS instead of the bundled CA list,
    # so antivirus/network filters that inject their own root cert (common on
    # managed laptops) don't break the GitHub update check. Runs before the
    # Api object exists, so there's no logger yet to record a fallback; if
    # truststore is missing or fails, urllib silently keeps using its default
    # bundled CA list instead.
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass

    if not _acquire_single_instance("JDE_SimpleSFTPClient_SingleInstance"):
        # Already running: never open a second window. Best-effort bring the
        # existing one to the front, then exit quietly either way.
        _focus_existing_window("Simple SFTP Client")
        sys.exit(0)

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JDEProjects.SimpleSFTPClient")
        except Exception:
            pass
    api = Api(APP_VERSION)
    debug.on_warning = api._on_debug_warning
    debug.prune()  # catches anything already over the cap from a previous run
    window = webview.create_window(
        "Simple SFTP Client", url=resource_path("simple_sftp_client-UI.html"),
        js_api=api, width=1480, height=980, min_size=(1000, 700),
        background_color="#0a0e14")
    api._set_window(window)

    def _wire_external_drop():
        # Let users drag files in from Windows Explorer onto the remote pane.
        try:
            pane = window.dom.get_element("#paneRemote")
            if pane:
                pane.events.drop += api._on_external_drop
                debug.log("External drop wired on remote pane")
        except Exception as e:
            debug.log("wire external drop failed", str(e))
    window.events.loaded += _wire_external_drop

    # Geometry save/restore locates the window by enumerating this process's
    # own windows. With exactly one instance ever running, that window
    # always owns the saved position, so this is always wired.
    window.events.shown += lambda: _restore_geometry(window)

    def _on_closing():
        # Runs synchronously on the Qt GUI thread (pywebview's "closing"
        # event locks and calls handlers inline). Calling evaluate_js
        # from here directly deadlocks: the async JS call needs the GUI
        # thread's event loop to complete, and that thread is this one,
        # blocked waiting on it. So when a batch is running and the user
        # hasn't confirmed yet, veto the close and hand the "ask the
        # page" step to a background thread instead of doing it inline;
        # that thread's evaluate_js call completes normally once this
        # handler returns and the GUI thread's event loop is free again.
        # The page answers via confirm_quit(), which sets
        # _quit_confirmed and calls window.destroy() to re-fire this
        # same event, now allowed through.
        if api._transfers_active() and not api._quit_confirmed:
            threading.Thread(target=lambda: api._emit("quit-confirm", {}), daemon=True).start()
            return False
        api._shutdown()
        _save_geometry(window)
        return True
    window.events.closing += _on_closing

    try:
        webview.start(gui="qt", icon=resource_path("simple_sftp_client.png"))
    except TypeError:
        webview.start(gui="qt")


if __name__ == "__main__":
    main()
