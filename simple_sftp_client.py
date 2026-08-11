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
import io
import stat
import ctypes
from ctypes import wintypes
import errno
import json
import logging
import base64
import hashlib
import ssl
import time
import shutil
import threading
import traceback
import webbrowser
import socket
import posixpath
import urllib.error
from datetime import datetime
from urllib.request import Request, urlopen

import webview
import paramiko

from transfer_queue import TransferQueue

APP_VERSION = "1.6.0"
GITHUB_REPO = "JDE-Projects/Simple-SFTP-Client"   # owner/repo for update checks
WORKER_COUNT = 2   # transfer queue workers by default, each its own SFTP session
WORKER_COUNT_MAX = 5   # ceiling for a batch of many small files (channels-per-connection headroom, see status.md)


def worker_target(sizes):
    """Decide the worker-pool size for a batch from its file sizes (bytes).
    Returns WORKER_COUNT_MAX when the batch has enough small files to make
    extra channels pay off, else the WORKER_COUNT default. Sizes below zero
    are unknown and never count as small."""
    small = sum(1 for s in sizes if 0 <= s < 1024 * 1024)
    return WORKER_COUNT_MAX if small >= 8 else WORKER_COUNT

# Weak / deprecated / CVE-prone algorithms we refuse (secure-or-fail).
DISABLED_ALGORITHMS = {
    "kex": ["diffie-hellman-group1-sha1", "diffie-hellman-group14-sha1",
            "diffie-hellman-group-exchange-sha1"],
    "ciphers": ["3des-cbc", "aes128-cbc", "aes192-cbc", "aes256-cbc",
                "blowfish-cbc", "cast128-cbc", "arcfour", "arcfour128", "arcfour256"],
    "macs": ["hmac-md5", "hmac-md5-96", "hmac-sha1-96", "hmac-sha1"],
    "keys": ["ssh-dss"],
}


# ───────────── paths ─────────────
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SESSIONS_FILE = os.path.join(exe_dir(), "servers.json")
KNOWN_HOSTS_FILE = os.path.join(exe_dir(), "known_hosts")


# ----------------------------------------------------------------------------
# Local prefs store. One JSON file next to the app holds EVERY persisted
# setting: theme, window geometry, and anything added later. Always read-
# merge-write through load_prefs / save_prefs. Never overwrite the file with
# a single key, or the next setting you add silently wipes the others.
# ----------------------------------------------------------------------------

def _pref_path() -> str:
    return os.path.join(exe_dir(), "simple_sftp_client.pref")


def load_prefs() -> dict:
    """Load the full prefs dict. Tolerant of a missing or corrupt file."""
    try:
        with open(_pref_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(prefs: dict) -> bool:
    try:
        with open(_pref_path(), "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        return True
    except Exception:
        return False


# Window geometry persistence. Save and restore the ABSOLUTE window frame
# rectangle via Win32, found by the window title. GetWindowRect (save) and
# SetWindowPos (restore) share one frame-based, physical-pixel coordinate
# space, so the rect round-trips exactly at any DPI or monitor layout. Do NOT
# pass x/y into create_window and do NOT use window.move: pywebview's Qt
# backend applies those pre-show and relative to the primary screen, so the
# window lands on the wrong monitor, drifts down by the title-bar height each
# launch, and slides sideways at non-100% scaling.

def _win32():
    u = ctypes.windll.user32
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    return u


def _own_window_handle(title):
    """HWND of our own top-level window with this title.

    FindWindowW matches by title across the whole desktop, so with a second
    instance open it can return the other copy's window. Enumerate instead and
    keep only a window owned by this process.
    """
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

        own_pid = os.getpid()
        found = {"hwnd": None}

        def _callback(hwnd, lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != own_pid:
                return True
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
        return found["hwnd"]
    except Exception:
        return None


def _save_geometry(win) -> None:
    """Save the absolute frame rect (physical px) via Win32. Wire to `closing`.
    Wrapped end to end so a failure here can never block the window from closing."""
    try:
        u = _win32()
        hwnd = _own_window_handle(win.title)
        if not hwnd:
            return
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return
        x, y, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
        if x <= -30000 or y <= -30000:   # minimized sentinel, not a real spot
            return
        if w <= 0 or h <= 0:
            return
        prefs = load_prefs()
        prefs["window"] = {"x": x, "y": y, "width": w, "height": h}
        save_prefs(prefs)
    except Exception:
        pass


def _restore_geometry(win) -> None:
    """Restore the saved frame rect via Win32. Wire to `shown` (after the OS
    window exists). Validate before applying; never raise."""
    try:
        geo = load_prefs().get("window")
        if not isinstance(geo, dict):
            return
        x, y, w, h = geo.get("x"), geo.get("y"), geo.get("width"), geo.get("height")
        for v in (x, y, w, h):
            if not isinstance(v, int) or isinstance(v, bool):
                return
        if w <= 0 or h <= 0:
            return
        # Is a point in the title bar still on a connected monitor?
        point = wintypes.POINT(x + 100, y + 30)
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        if not user32.MonitorFromPoint(point, 0):   # MONITOR_DEFAULTTONULL
            return
        u = _win32()
        hwnd = _own_window_handle(win.title)
        if not hwnd:
            return
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        u.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


def hostkey_name(host, port):
    """The name paramiko stores a host key under (bracketed when not port 22)."""
    port = int(port or 22)
    return host if port == 22 else "[%s]:%d" % (host, port)


def fingerprint_sha256(key):
    """OpenSSH-style SHA256 fingerprint, e.g. 'SHA256:abc...' (no padding)."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def load_known_hosts():
    hk = paramiko.HostKeys()
    if os.path.exists(KNOWN_HOSTS_FILE):
        try:
            hk.load(KNOWN_HOSTS_FILE)
        except Exception:
            pass
    return hk


class UnknownHostKey(Exception):
    """First contact with a host whose key is not yet pinned."""
    def __init__(self, hostname, key):
        super().__init__("unknown host key")
        self.hostname = hostname
        self.key = key


class _TofuPolicy(paramiko.MissingHostKeyPolicy):
    """Do not auto-add. Surface the offered key so the UI can ask the user."""
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


# ───────────── debug log (off by default) ─────────────
class _ParamikoBridge(logging.Handler):
    """Feed paramiko's protocol-level logging into the debug file when enabled."""
    def __init__(self, dbg):
        super().__init__()
        self._dbg = dbg

    def emit(self, record):
        try:
            self._dbg.log(f"{record.name}: {record.getMessage()}")
        except Exception:
            pass


class DebugLog:
    def __init__(self):
        self._on = False
        self._path = None
        self._lock = threading.Lock()
        self._bridge = None  # paramiko logging handler, attached only while on

    def set_enabled(self, on):
        on = bool(on)
        with self._lock:
            if on and not self._path:
                stamp = datetime.now().strftime("%m%d%Y_%H%M%S")
                self._path = os.path.join(exe_dir(), f"Debug_Log_{stamp}.txt")
                try:
                    with open(self._path, "w", encoding="utf-8") as f:
                        f.write("=== Simple SFTP Client debug log ===\n")
                        f.write(f"Started: {datetime.now().isoformat()}\n" + "=" * 60 + "\n\n")
                except Exception:
                    self._path = None
                    self._on = False
                    return False
            self._on = on
        self._set_paramiko(on)
        return True

    def _set_paramiko(self, on):
        """Capture paramiko's verbose transport/SFTP logging while debug is on."""
        plog = logging.getLogger("paramiko")
        try:
            if on and not self._bridge:
                self._bridge = _ParamikoBridge(self)
                plog.addHandler(self._bridge)
                plog.setLevel(logging.DEBUG)
            elif not on and self._bridge:
                plog.removeHandler(self._bridge)
                self._bridge = None
        except Exception:
            pass

    def is_enabled(self):
        return self._on

    def log(self, label, content=""):
        if not self._on or not self._path:
            return
        try:
            with self._lock, open(self._path, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                f.write(f"[{ts}] {label}\n")
                if content:
                    if isinstance(content, (dict, list)):
                        content = json.dumps(content, indent=2, default=str)
                    f.write(f"{content}\n")
                f.write("\n")
        except Exception:
            pass


debug = DebugLog()


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}")
        n /= 1024


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def friendly_error(e):
    """Plain-language message for the UI; full detail goes to the debug log."""
    try:
        debug.log("error detail", f"{type(e).__name__}: {e}")
    except Exception:
        pass
    if isinstance(e, paramiko.AuthenticationException):
        return "Authentication failed. Check the username, password, or key."
    if isinstance(e, paramiko.SSHException):
        m = str(e)
        if "negotiat" in m.lower() or "incompatible" in m.lower():
            return ("Could not negotiate a secure connection. This server may only "
                    "offer outdated algorithms, which this client refuses for safety.")
        return m or "SSH connection error."
    if isinstance(e, socket.gaierror):
        return "Could not resolve that host name. Check the address."
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "Connection timed out. Check the host, port, and network."
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused. Check the port and that the server is running."
    if isinstance(e, PermissionError):
        return "Permission denied. Choose a location you can write to."
    if isinstance(e, FileNotFoundError):
        return f"Not found: {e.filename or 'the requested path'}"
    if isinstance(e, IsADirectoryError):
        return "That path is a folder. Include a filename."
    if isinstance(e, OSError):
        base = e.strerror or "The operation failed"
        return f"{base}: {e.filename}" if getattr(e, "filename", None) else base
    return "Something went wrong. Turn on the debug log for details."


def error_tips(e):
    """Actionable, plain-language guidance shown in the failure popup."""
    if isinstance(e, (TimeoutError, socket.timeout)):
        return ("The server didn't respond in time. Common causes:\n"
                "• The host address or port number is wrong.\n"
                "• A firewall is blocking the attempt — on the server's network or in its operating system.\n"
                "• A missing NAT rule or port-forward means your connection never reaches the server.\n\n"
                "Ask the SFTP server's administrator to confirm that connections from your network are "
                "allowed on this port.")
    if isinstance(e, ConnectionRefusedError):
        return ("The server's machine answered, but nothing is listening on that port.\n"
                "• Double-check the port number.\n"
                "• Confirm the SFTP/SSH service is running on the server.")
    if isinstance(e, socket.gaierror):
        return ("The host name could not be looked up.\n"
                "• Check the spelling of the address.\n"
                "• Try the server's IP address instead of its name.")
    if isinstance(e, paramiko.AuthenticationException):
        return ("The server was reached but rejected your credentials.\n"
                "• Re-check the username and password.\n"
                "• If using a key, confirm the private key matches a public key installed on the server.")
    if isinstance(e, paramiko.SSHException):
        m = str(e).lower()
        if "negotiat" in m or "incompatible" in m:
            return ("The server was reached but no secure encryption method could be agreed on.\n"
                    "This client refuses outdated, insecure algorithms for safety. The server's SSH "
                    "configuration may need to be updated to offer modern algorithms.")
        return ("The secure connection could not be established.\n"
                "Turn on the debug log (bottom-left) and try again to capture the details.")
    return ("The connection could not be completed.\n"
            "Check the host, port, username, and credentials. Turn on the debug log (bottom-left) "
            "for more detail.")


def missing_fields(p):
    """Up-front field check shared by Connect and Test (returns '' when OK)."""
    host = (p.get("host") or "").strip()
    user = (p.get("username") or "").strip()
    key = (p.get("key_path") or "").strip()
    pw = p.get("password") or ""
    if not host or not user:
        return "Enter a host and a username before connecting."
    if not key and not pw:
        return "Enter a password, or choose a private key, before connecting."
    return ""


def _update_error_reason(exc: BaseException) -> str:
    """Turn a check_update exception into a short, plain-language reason to
    show in the UI. Pure and network-free: takes the already-raised exception,
    never touches the network itself.

    Each branch is specific to a failure that can actually cause it, and
    names a next step where there is a sensible one. Subclasses are checked
    before their parents: SSLCertVerificationError and SSLEOFError/
    SSLZeroReturnError before the generic ssl.SSLError, and the specific
    ConnectionError subclasses and socket.gaierror before the generic OSError
    branch (socket.timeout is an alias of TimeoutError, and both are OSError
    subclasses)."""
    # HTTPError is a URLError subclass but carries its own .code, so classify
    # it before unwrapping anything.
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            return (
                "GitHub is rate-limiting update checks from this network. "
                "Try again later."
            )
        if exc.code == 404:
            return "No published release was found."
        if 500 <= exc.code < 600:
            return f"GitHub is having trouble on its end (HTTP {exc.code})."
        return f"GitHub returned an error (HTTP {exc.code})."

    if isinstance(exc, json.JSONDecodeError):
        return (
            "GitHub returned something unexpected. This often means a proxy "
            "or a guest wifi sign-in page answered instead."
        )

    # A plain URLError wraps the underlying cause (ssl.SSLError, socket.timeout,
    # a DNS/socket OSError, ...) in its .reason; unwrap it to classify the
    # actual cause, but remember it came from a URLError for the fallback below.
    is_url_error = isinstance(exc, urllib.error.URLError)
    cause = exc.reason if is_url_error and exc.reason is not None else exc

    if isinstance(cause, ssl.SSLCertVerificationError):
        return (
            "GitHub's certificate could not be verified. This usually means "
            "antivirus or a network filter is inspecting HTTPS traffic."
        )
    if isinstance(cause, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return "The secure connection was cut off during the handshake with GitHub."
    if isinstance(cause, ssl.SSLError):
        return "The secure connection to GitHub failed."
    if isinstance(cause, socket.gaierror):
        return (
            "The address for api.github.com could not be looked up. Check "
            "DNS or the internet connection."
        )
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return "GitHub didn't respond in time."
    if isinstance(cause, (ConnectionRefusedError, ConnectionResetError)):
        return (
            "The connection was refused or reset. A firewall or proxy may "
            "be blocking it."
        )
    if isinstance(cause, OSError) and getattr(cause, "errno", None) == errno.ENETUNREACH:
        return "No network connection."
    if is_url_error:
        return "Couldn't reach GitHub. Check the internet connection."

    text = f"{type(exc).__name__}: {exc}"
    if len(text) > 120:
        text = text[:117] + "..."
    return text


class Api:
    def __init__(self):
        self._window = None
        self.connected = False
        self.client = None
        self.sftp = None
        self._cred_pass = ""
        self._pending_host_key = None  # (hostname, offered_key) awaiting user trust
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._watch_stop = None
        self._watch_thread = None
        # transfer queue: a pool of workers drains it, each over its own SFTP
        # session (never self.sftp, that stays reserved for the file browser).
        # Pool size is WORKER_COUNT (2) by default, up to WORKER_COUNT_MAX (5)
        # for a batch of many small files.
        self.queue = TransferQueue()
        self._workers = []  # live worker Thread objects, at most self._target_workers
        self._worker_lock = threading.Lock()
        # How many workers the pool tops up to. A fresh batch sets this (see
        # _enqueue_files); it resets to WORKER_COUNT once the pool empties.
        self._target_workers = WORKER_COUNT
        # set while sync/watch/external-drop run, so the queue worker knows to wait
        self._legacy_active = threading.Event()
        # Poll model: workers never touch evaluate_js. They write plain state
        # here, the window pulls it on a timer via poll_queue(). Progress is
        # keyed by queue item id since up to WORKER_COUNT_MAX items can be
        # active at once; guarded by its own lock since multiple workers write it.
        self._progress_by_id = {}
        self._progress_lock = threading.Lock()
        self._console_buffer = []
        self._console_lock = threading.Lock()
        self._poll_mode = False

    def set_window(self, w):
        self._window = w

    def get_meta(self):
        return {
            "version": APP_VERSION,
            "key_types": ["Ed25519", "RSA-4096"],
            "sessions": self._load_sessions(),
        }

    def get_theme(self):
        theme = load_prefs().get("theme")
        return theme if theme in ("dark", "light") else "dark"

    def save_theme(self, theme):
        if theme not in ("dark", "light"):
            return {"ok": False}
        prefs = load_prefs()
        prefs["theme"] = theme
        if save_prefs(prefs):
            return {"ok": True}
        return {"ok": False}

    def set_debug(self, on):
        ok = debug.set_enabled(on)
        debug.log("Debug enabled" if on and ok else "Debug disabled")
        return {"ok": ok, "enabled": debug.is_enabled()}

    def export_console(self, text):
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

    def _emit(self, event, payload):
        if self._window:
            try:
                self._window.evaluate_js(
                    f"window.appEvent && window.appEvent({json.dumps(event)},{json.dumps(payload)})")
            except Exception:
                pass

    def _vlog(self, msg, level="info"):
        """Verbose, FileZilla-style operation line: to the console and debug log."""
        self._emit("console", {"msg": msg, "level": level})
        debug.log(msg)

    def _worker_log(self, msg, level="info"):
        """Console line from the worker thread: never calls evaluate_js, just
        buffers for the next poll_queue() and writes the debug log."""
        with self._console_lock:
            self._console_buffer.append({"msg": msg, "level": level})
        debug.log(msg)

    # ───────────── sessions (servers.json, never passwords) ─────────────
    def _load_sessions(self):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("sessions", []) if isinstance(data, dict) else []
        except Exception:
            return []

    def _save_sessions(self, sessions):
        try:
            with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
                json.dump({"_note": "Simple SFTP Client saved sessions (no passwords).",
                           "sessions": sessions}, f, indent=2)
            return True
        except Exception as e:
            debug.log("save sessions failed", str(e))
            return False

    def save_session(self, s):
        sessions = self._load_sessions()
        s = {k: s.get(k, "") for k in ("name", "host", "port", "username",
                                       "auth", "key_path", "start_path", "remember")}
        # optional remembered password -> OS keychain. The session may only
        # claim a saved password when one was actually written, so a failed
        # write, or "remember" ticked with no password to save (key auth, or
        # saving before connecting), both leave remember off.
        pw_saved = False
        pw_error = None
        if s.get("remember") and self._cred_pass:
            try:
                import keyring
                keyring.set_password("SimpleSFTPClient", f"{s['host']}|{s['username']}", self._cred_pass)
                pw_saved = True
            except Exception as e:
                debug.log("keyring set failed", str(e))
                pw_error = f"Could not save the password to Windows Credential Manager: {e}"
        if s.get("remember") and not pw_saved:
            s["remember"] = False
        sessions = [x for x in sessions if x.get("name") != s["name"]]
        sessions.append(s)
        sessions.sort(key=lambda x: x.get("name", "").lower())
        self._save_sessions(sessions)
        result = {"ok": True, "sessions": sessions, "pw_saved": pw_saved}
        if pw_error:
            result["pw_error"] = pw_error
        return result

    def delete_session(self, name):
        sessions = self._load_sessions()
        target = next((x for x in sessions if x.get("name") == name), None)
        sessions = [x for x in sessions if x.get("name") != name]
        self._save_sessions(sessions)
        if target and target.get("remember"):
            try:
                import keyring
                keyring.delete_password("SimpleSFTPClient", f"{target.get('host')}|{target.get('username')}")
            except Exception as e:
                debug.log("keyring delete failed", str(e))
        return {"ok": True, "sessions": sessions}

    def _remembered_password(self, host, username):
        try:
            import keyring
            return keyring.get_password("SimpleSFTPClient", f"{host}|{username}") or ""
        except Exception:
            return ""

    def get_remembered(self, host, username):
        return {"password": self._remembered_password(host, username)}

    # ───────────── connect ─────────────
    def _open(self, host, port, username, password, key_path, passphrase):
        client = paramiko.SSHClient()
        if os.path.exists(KNOWN_HOSTS_FILE):
            try:
                client.load_host_keys(KNOWN_HOSTS_FILE)
            except Exception:
                pass
        # Trust on first use: unknown hosts raise UnknownHostKey (user is asked),
        # a changed key raises paramiko.BadHostKeyException (flagged, not trusted).
        client.set_missing_host_key_policy(_TofuPolicy())
        kwargs = dict(hostname=host, port=int(port or 22), username=username,
                      timeout=15, allow_agent=False, look_for_keys=False,
                      disabled_algorithms=DISABLED_ALGORITHMS)
        if key_path:
            kwargs["key_filename"] = key_path
            if passphrase:
                kwargs["passphrase"] = passphrase
        else:
            kwargs["password"] = password
        client.connect(**kwargs)
        return client

    def connect(self, p):
        miss = missing_fields(p)
        if miss:
            return {"ok": False, "error": miss}
        host = (p.get("host") or "").strip()
        username = (p.get("username") or "").strip()
        password = p.get("password") or ""
        key_path = (p.get("key_path") or "").strip()
        passphrase = p.get("passphrase") or ""
        self._cred_pass = password
        debug.log("CONNECT", {"host": host, "user": username, "auth": "key" if key_path else "password"})
        try:
            self.client = self._open(host, p.get("port", 22), username, password, key_path, passphrase)
            self.sftp = self.client.open_sftp()
            self.connected = True
            ti = self._transport_info()
            if ti:
                self._vlog(f"Negotiated: cipher {ti.get('cipher','?')} · "
                           f"kex {ti.get('kex','?')} · mac {ti.get('mac','?')}")
            home = self.sftp.normalize(".")
            self._vlog(f"SFTP session opened — home {home}", "ok")
            start = (p.get("start_path") or "").strip() or home
            try:
                self.sftp.stat(start)
            except Exception:
                start = home
            return {"ok": True, "home": home, "cwd": start, "transport": self._transport_info()}
        except UnknownHostKey as e:
            # Pin under the port-aware name paramiko actually checks against
            # (bracketed for non-standard ports), not e.hostname, whose format
            # differs between the unknown-key and changed-key paths.
            self._pending_host_key = (hostkey_name(host, p.get("port", 22)), e.key)
            debug.log(f"Unknown host key for {host} ({e.key.get_name()}).")
            return {"ok": False, "host_key_unknown": True, "host": host,
                    "key_type": e.key.get_name(), "fingerprint": fingerprint_sha256(e.key)}
        except paramiko.BadHostKeyException as e:
            # Same port-aware name as above. The changed-key path hands back a
            # bare host in e.hostname, so pinning by that would store the new
            # key under a name the library never rechecks, and the warning would
            # loop forever on non-standard ports.
            self._pending_host_key = (hostkey_name(host, p.get("port", 22)), e.key)
            debug.log(f"HOST KEY CHANGED for {host} - refused.")
            return {"ok": False, "host_key_changed": True, "host": host,
                    "key_type": e.key.get_name(),
                    "new_fingerprint": fingerprint_sha256(e.key),
                    "old_fingerprint": fingerprint_sha256(e.expected_key)}
        except Exception as e:
            debug.log("CONNECT failed", traceback.format_exc())
            return {"ok": False, "error": friendly_error(e), "tips": error_tips(e)}

    def trust_host_key(self):
        """Pin the host key the user just confirmed, then they may reconnect."""
        pending = self._pending_host_key
        self._pending_host_key = None
        if not pending:
            return {"ok": False, "error": "No host key is waiting to be trusted."}
        name, key = pending
        try:
            hk = load_known_hosts()
            if hk.lookup(name):          # replace any prior key for this host
                del hk[name]
            hk.add(name, key.get_name(), key)
            hk.save(KNOWN_HOSTS_FILE)
            debug.log(f"Trusted host key for {name} ({key.get_name()}).")
            return {"ok": True, "fingerprint": fingerprint_sha256(key)}
        except Exception as e:
            return {"ok": False, "error": f"Could not save the host key: {e}"}

    def get_host_key(self, host, port=22):
        """Return the pinned key(s) for a host so the UI can show them."""
        host = (host or "").strip()
        name = hostkey_name(host, port) if host else ""
        sub = load_known_hosts().lookup(name) if name else None
        if not sub:
            return {"known": False, "host": host}
        entries = [{"key_type": kt, "fingerprint": fingerprint_sha256(k)}
                   for kt, k in sub.items()]
        return {"known": True, "host": host, "entries": entries}

    def forget_host_key(self, host, port=22):
        """Remove a pinned host key (e.g. before deliberately re-trusting)."""
        host = (host or "").strip()
        name = hostkey_name(host, port) if host else ""
        try:
            hk = load_known_hosts()
            if name and hk.lookup(name):
                del hk[name]
                hk.save(KNOWN_HOSTS_FILE)
                debug.log(f"Forgot host key for {name}.")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Could not remove the host key: {e}"}

    def _transport_info(self):
        try:
            t = self.client.get_transport()
            return {"cipher": t.remote_cipher, "kex": getattr(t, "kex_engine", ""),
                    "mac": t.remote_mac}
        except Exception:
            return {}

    def test_connection(self, p):
        """Reachability check only: open a TCP socket and read the SSH banner.
        Confirms host/port reachable and that an SSH server answers — no host
        key check and no authentication (that is Connect's job)."""
        host = (p.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "Enter a host to test."}
        try:
            port = int(p.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        debug.log("TEST", {"host": host, "port": port})
        try:
            with socket.create_connection((host, port), timeout=10) as sock:
                sock.settimeout(4)
                try:
                    banner = sock.recv(256)
                except (socket.timeout, OSError):
                    banner = b""
        except Exception as e:
            return {"ok": False, "error": friendly_error(e), "tips": error_tips(e)}
        if banner.startswith(b"SSH-"):
            ident = banner.decode("ascii", "replace").splitlines()[0].strip()
            self._vlog(f"Test: {host}:{port} reachable — {ident}", "ok")
            return {"ok": True, "msg": f"{host}:{port} reachable — {ident}"}
        return {"ok": False, "warn": True,
                "error": f"Something is listening on {host}:{port}, but it didn't identify as an "
                         "SSH/SFTP server.",
                "tips": ("Confirm this is the SFTP/SSH port (often 22). A different service may be "
                         "answering on it.")}

    def disconnect(self):
        self.stop_watch()
        try:
            if self.sftp:
                self.sftp.close()
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.connected = False
        self.client = self.sftp = None
        self._cred_pass = ""
        debug.log("DISCONNECTED")
        return {"ok": True}

    def ping(self):
        # latency for the health indicator
        if not self.connected:
            return {"ok": False}
        try:
            t0 = time.time()
            self.sftp.stat(".")
            return {"ok": True, "ms": int((time.time() - t0) * 1000)}
        except Exception:
            self.connected = False
            return {"ok": False}

    # ───────────── listing ─────────────
    def list_local(self, path):
        if path in ("", "DRIVES") and os.name == "nt":
            import string
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return {"ok": True, "cwd": "DRIVES", "parent": None,
                    "entries": [{"name": d, "is_dir": True, "size": 0, "mtime": 0} for d in drives]}
        path = path or os.path.expanduser("~")
        try:
            entries = []
            for name in os.listdir(path):
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                    entries.append({"name": name, "is_dir": os.path.isdir(full),
                                    "size": st.st_size, "mtime": int(st.st_mtime)})
                except Exception:
                    continue
            parent = os.path.dirname(path.rstrip("\\/")) or ("DRIVES" if os.name == "nt" else "/")
            if os.name == "nt" and len(path.rstrip("\\/")) <= 2:
                parent = "DRIVES"
            return {"ok": True, "cwd": path, "parent": parent, "entries": entries}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def list_remote(self, path):
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        try:
            path = self.sftp.normalize(path or ".")
            entries = []
            for a in self.sftp.listdir_attr(path):
                entries.append({"name": a.filename, "is_dir": stat.S_ISDIR(a.st_mode),
                                "size": a.st_size, "mtime": int(a.st_mtime or 0)})
            parent = posixpath.dirname(path.rstrip("/")) or "/"
            self._vlog(f"ls {path} → {len(entries)} item(s)")
            return {"ok": True, "cwd": path, "parent": parent, "entries": entries}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    # ───────────── file ops ─────────────
    def make_dir(self, side, path, name):
        try:
            if side == "local":
                os.makedirs(os.path.join(path, name), exist_ok=False)
            else:
                target = posixpath.join(path, name)
                self.sftp.mkdir(target)
                self._vlog(f"mkdir {target}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def rename(self, side, path, old, new):
        try:
            if side == "local":
                os.rename(os.path.join(path, old), os.path.join(path, new))
            else:
                self.sftp.rename(posixpath.join(path, old), posixpath.join(path, new))
                self._vlog(f"rename {posixpath.join(path, old)} → {new}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def delete(self, side, path, items):
        errs = []
        for it in items:
            try:
                if side == "local":
                    full = os.path.join(path, it["name"])
                    shutil.rmtree(full) if it["is_dir"] else os.remove(full)
                else:
                    full = posixpath.join(path, it["name"])
                    self._rremove(full) if it["is_dir"] else self.sftp.remove(full)
                    if not it["is_dir"]:
                        self._vlog(f"remove {full}")
            except Exception as e:
                errs.append(f"{it['name']}: {e}")
        return {"ok": True, "errors": errs}

    def _rremove(self, path):
        for a in self.sftp.listdir_attr(path):
            child = posixpath.join(path, a.filename)
            self._rremove(child) if stat.S_ISDIR(a.st_mode) else self.sftp.remove(child)
        self.sftp.rmdir(path)
        self._vlog(f"rmdir {path}")

    def open_local(self, path, name):
        try:
            os.startfile(os.path.join(path, name))  # noqa (Windows)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    # ───────────── transfers (queue + progress + resume + retry) ─────────────
    def cancel(self):
        """Cancel-all, wired to the footer Cancel button: cancels every waiting
        queue item and flags every active one so each worker's byte loop stops
        and finalizes it as cancelled. self._cancel is still set for the legacy
        path (sync/watch/external-drop), which is not on the per-item flag."""
        self.queue.cancel_all()
        self._cancel.set()
        return {"ok": True}

    def poll_queue(self):
        """Pulled by the window on a timer (~200ms) while a queue is active.
        This is the only channel from the worker threads to the UI: it never
        calls evaluate_js, so it cannot deadlock the window."""
        with self._console_lock:
            lines = self._console_buffer
            self._console_buffer = []
        items, pending = self.queue.snapshot_and_pending()
        active_ids = [it["id"] for it in items if it["state"] == "active"]
        with self._progress_lock:
            progress = {str(k): v for k, v in self._progress_by_id.items()}
        return {
            "items": items,
            "pending": pending,
            "active_ids": active_ids,
            "progress": progress,
            "console": lines,
            "paused": self.queue.is_paused(),
        }

    def cancel_item(self, item_id):
        """Cancel a single queued item. Waiting items go straight to cancelled;
        an active item is flagged (TransferItem.cancel_requested) so whichever
        worker owns it interrupts its byte loop and finalizes it."""
        self.queue.cancel(item_id)
        return {"ok": True}

    def clear_finished(self):
        """Remove completed/failed/cancelled/skipped items so the window can
        re-render the queue without the clutter of finished transfers."""
        self.queue.clear_finished()
        items, pending = self.queue.snapshot_and_pending()
        return {"items": items, "pending": pending}

    def retry_item(self, item_id):
        """One-click retry: put a failed or cancelled queue item back in line and
        wake the worker pool. Wired to the ↻ control on failed/cancelled rows."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if not self.queue.requeue(item_id):
            return {"ok": False, "error": "That item can't be retried."}
        self._ensure_worker()
        return {"ok": True}

    def pause_queue(self):
        """Pause the queue: stop claiming new items. Files already mid-transfer
        are left to finish; the worker pool winds down once they do. Wired to the
        footer Pause control."""
        self.queue.pause()
        self._worker_log("Pausing queue…")
        return {"ok": True}

    def resume_queue(self):
        """Resume a paused queue and wake the worker pool to drain the waiting
        items in order."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        self.queue.resume()
        self._worker_log("Resuming queue")
        self._ensure_worker()
        return {"ok": True}

    def enqueue(self, jobs, direction, local_dir, remote_dir, on_conflict="overwrite"):
        """New entry point for pane transfers: expands jobs (same enumeration
        as transfer()) into per-file queue items and returns immediately. Folder
        enumeration below runs once, up front, over the shared self.sftp session
        (the same one the file browser uses); the per-file transfers that follow
        are drained by the worker pool (_ensure_worker), each over its own
        SFTP session opened when that worker starts."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._legacy_active.is_set():
            return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
        files = []   # (local_path, remote_path, size)
        try:
            for j in jobs:
                if direction == "upload":
                    lp = os.path.join(local_dir, j["name"])
                    rp = posixpath.join(remote_dir, j["name"])
                    if j["is_dir"]:
                        files += self._walk_local(lp, rp)
                    else:
                        try:
                            size = os.path.getsize(lp)
                        except OSError:
                            size = 0
                        files.append((lp, rp, size))
                else:
                    rp = posixpath.join(remote_dir, j["name"])
                    lp = os.path.join(local_dir, j["name"])
                    files += self._walk_remote(rp, lp) if j["is_dir"] else \
                        [(lp, rp, self._rsize(self.sftp, rp))]
        except Exception as e:
            return {"ok": False, "error": f"Could not enumerate: {e}"}
        return {"ok": True, "queued": self._enqueue_files(files, direction, on_conflict)}

    def _enqueue_files(self, files, direction, on_conflict):
        """Append (local_path, remote_path, size) triples to the queue as
        per-file items and wake the worker pool. Returns the count enqueued.
        Shared by pane transfers (enqueue) and external drops (upload_paths).
        Sizes are carried from enumeration (real local size for uploads, real
        remote size for downloads), never re-stat here.

        Also decides the worker-pool size for this batch (worker_target) and
        raises the pool's target under _worker_lock: only ever up, never torn
        down under a still-draining pool, so a mid-batch top-up never shrinks
        workers already running."""
        sizes = []
        for lp, rp, size in files:
            name = os.path.basename(lp)
            sizes.append(size)
            queue_size = size if size and size > 0 else 0
            self.queue.append(direction, lp, rp, name, size=queue_size, on_conflict=on_conflict)
        batch_target = worker_target(sizes)
        with self._worker_lock:
            if not self._workers:
                self._target_workers = batch_target
            else:
                self._target_workers = max(self._target_workers, batch_target)
        self._ensure_worker()
        return len(files)

    def _ensure_worker(self):
        """Top the worker pool up to self._target_workers live threads
        whenever there is waiting work. Poll mode is turned on here, under the
        same lock a worker retires itself with, so starting and stopping
        workers can never overlap and leave poll mode in the wrong state."""
        with self._worker_lock:
            if self.queue.is_paused():
                return
            self._workers = [w for w in self._workers if w.is_alive()]
            while len(self._workers) < self._target_workers and self.queue.waiting() > 0:
                self._poll_mode = True
                w = threading.Thread(target=self._worker_loop, daemon=True)
                self._workers.append(w)
                w.start()

    def _worker_loop(self):
        """Drains the queue, one item at a time, as one of up to
        self._target_workers (WORKER_COUNT by default, up to WORKER_COUNT_MAX
        for a batch of many small files) workers running concurrently. Each
        worker opens its own SFTP session
        here and closes it on exit; workers never touch self.sftp, that stays
        reserved for the file browser. done_count/total are only for the
        progress display, an approximation is fine there (each worker only
        knows its own done_count).

        Runs entirely off the pywebview bridge thread, so it must never call
        evaluate_js (that is what deadlocked the window). While any worker in
        the pool is running, _poll_mode is True: _progress() only updates
        in-memory state instead of emitting, and _worker_log() buffers console
        lines for the window to pull via poll_queue()."""
        me = threading.current_thread()
        try:
            sftp = self.client.open_sftp()
        except Exception as e:
            # No silent failure: surface it in the console log and let this
            # worker retire; the other worker (if any) keeps draining the queue.
            self._worker_log(f"could not open a transfer session: {e}", "error")
            with self._worker_lock:
                if me in self._workers:
                    self._workers.remove(me)
                if not self._workers:
                    self._poll_mode = False
                    self._target_workers = WORKER_COUNT
                    with self._progress_lock:
                        self._progress_by_id = {}
                    # Last worker out and none could open a session: don't leave
                    # queued items sitting as WAITING with nothing to drain them.
                    # Mark them failed so the failure is visible in the queue.
                    stranded = self.queue.fail_waiting(f"transfer session unavailable: {e}")
                    if stranded:
                        self._worker_log(
                            f"{stranded} queued item(s) marked failed: no transfer session",
                            "error")
            return
        try:
            done_count = 0
            while True:
                item = self.queue.claim()
                if item is None:
                    # Decide whether to stop under the same lock _ensure_worker
                    # starts workers with, and re-check the queue while holding
                    # it. Without this, a file enqueued in the instant this
                    # worker finds the queue empty would see a still-alive
                    # worker (this one) and _ensure_worker would skip starting
                    # one, yet this worker has already left the loop, and the
                    # file would wait forever. waiting() (not pending()) is the
                    # right check: pending() also counts items ACTIVE on the
                    # other worker, which would wrongly keep this one alive.
                    # Paused also retires here: claim() returns None while
                    # paused even with items still WAITING, and without this
                    # the worker would just busy-loop on those items instead
                    # of winding down.
                    with self._worker_lock:
                        paused = self.queue.is_paused()
                        if self.queue.waiting() == 0 or paused:
                            if me in self._workers:
                                self._workers.remove(me)
                            # Only the last worker to leave clears pool-wide
                            # state and logs the drain summary; the others just
                            # retire quietly.
                            if not self._workers:
                                self._poll_mode = False
                                with self._progress_lock:
                                    self._progress_by_id = {}
                                if paused and self.queue.waiting() > 0:
                                    # Paused with items still held: leave the target
                                    # alone so a resume drains the rest at the count
                                    # this batch was scaled to, not the default.
                                    self._worker_log(
                                        f"Queue paused ({self.queue.waiting()} still waiting)")
                                else:
                                    # Reached the empty queue (paused with nothing
                                    # left, or a normal drain). Reset the pool target
                                    # so a later lone retry does not inherit a stale
                                    # scaled-up count, and clear a stale pause flag so
                                    # the next batch is not held back by a pause that
                                    # belonged to a queue now emptied.
                                    self._target_workers = WORKER_COUNT
                                    self.queue.resume()
                                    counts = self.queue.counts()
                                    self._worker_log(
                                        f"Queue drained: {counts.get('completed', 0)} completed, "
                                        f"{counts.get('failed', 0)} failed, {counts.get('cancelled', 0)} cancelled, "
                                        f"{counts.get('skipped', 0)} skipped")
                            break
                    continue
                total = done_count + self.queue.pending()
                if item.cancel_requested:
                    # cancel() on a waiting item goes straight to CANCELLED, so this
                    # should not happen in practice, but guard anyway.
                    self.queue.mark_cancelled(item.id)
                    self._worker_log(f"cancelled {item.name}", "warn")
                    done_count += 1
                    continue
                arrow = "↑" if item.direction == "upload" else "↓"
                ok = False
                res = None
                last_err = ""
                for attempt in range(3):
                    if item.cancel_requested:
                        break
                    try:
                        res = self._one(item.direction, item.local_path, item.remote_path,
                                         item.name, done_count, total, item.on_conflict,
                                         sftp, cancel_check=lambda it=item: it.cancel_requested,
                                         progress_key=item.id)
                        ok = True
                        break  # success (including "skip"/"cancelled"): stop retrying
                    except Exception as e:
                        last_err = str(e)
                        debug.log(f"transfer retry {attempt+1}", f"{item.name}: {e}")
                        if item.cancel_requested:
                            break
                        time.sleep(0.6)
                if res == "cancelled" or (not ok and item.cancel_requested):
                    # res == "cancelled": the byte loop itself broke early, so
                    # the transfer did not finish; that is the one true source
                    # of truth here, not a flag re-check after the fact, which
                    # could mislabel a file that finished sending its very last
                    # byte the instant cancel arrived.
                    self.queue.mark_cancelled(item.id)
                    self._worker_log(f"cancelled {item.name}", "warn")
                elif res == "skip":
                    self.queue.mark_skipped(item.id)
                    self._worker_log(f"skip {item.name} (already up to date)")
                elif ok:
                    self.queue.mark_completed(item.id)
                    self._worker_log(f"{arrow} {(item.remote_path if item.direction == 'upload' else item.name)}", "ok")
                else:
                    self.queue.mark_failed(item.id, last_err or "transfer failed")
                    self._worker_log(f"failed {item.name}", "error")
                done_count += 1
                with self._progress_lock:
                    self._progress_by_id.pop(item.id, None)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            # Safety net for an exit by exception rather than the clean
            # empty-queue path above: remove this worker if it is still in the
            # pool, and only clear pool-wide state if it was the last one.
            with self._worker_lock:
                if me in self._workers:
                    self._workers.remove(me)
                if not self._workers:
                    self._poll_mode = False
                    # Leave the target alone during a pause-hold so a resume drains
                    # the rest at the scaled count; any real drain resets it.
                    if not (self.queue.is_paused() and self.queue.waiting() > 0):
                        self._target_workers = WORKER_COUNT
                    with self._progress_lock:
                        self._progress_by_id = {}

    def transfer(self, jobs, direction, local_dir, remote_dir, on_conflict="overwrite"):
        """jobs: list of {name, is_dir}. Expands dirs, transfers files with
        progress, resume (partial), skip-if-identical, and per-file retry."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        self._cancel.clear()
        files = []   # (local_path, remote_path, size)
        try:
            for j in jobs:
                if direction == "upload":
                    lp = os.path.join(local_dir, j["name"])
                    rp = posixpath.join(remote_dir, j["name"])
                    files += self._walk_local(lp, rp) if j["is_dir"] else [(lp, rp, 0)]
                else:
                    rp = posixpath.join(remote_dir, j["name"])
                    lp = os.path.join(local_dir, j["name"])
                    files += self._walk_remote(rp, lp) if j["is_dir"] else [(lp, rp, 0)]
        except Exception as e:
            return {"ok": False, "error": f"Could not enumerate: {e}"}
        return self._run_transfer(files, direction, on_conflict)

    def upload_paths(self, paths, remote_dir, on_conflict="overwrite"):
        """Upload absolute local paths (files or folders) dragged in from outside
        the app, into remote_dir. Enumerated up front over the shared self.sftp
        session, then enqueued as ordinary queue items and drained by the
        worker pool, the same as a pane transfer (enqueue)."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self._legacy_active.is_set():
            return {"ok": False, "error": "A sync or watch operation is running. Wait for it to finish."}
        files = []
        try:
            for raw in paths or []:
                lp = self._normalize_drop_path(raw)
                if not lp:
                    continue
                name = os.path.basename(lp.rstrip("\\/"))
                rp = posixpath.join(remote_dir, name)
                if os.path.isdir(lp):
                    files += self._walk_local(lp, rp)
                elif os.path.isfile(lp):
                    try:
                        size = os.path.getsize(lp)
                    except OSError:
                        size = 0
                    files.append((lp, rp, size))
        except Exception as e:
            return {"ok": False, "error": f"Could not read the dropped items: {e}"}
        if not files:
            return {"ok": False, "error": "No files were found in the dropped items."}
        debug.log("EXTERNAL UPLOAD", {"items": len(files), "remote": remote_dir})
        return {"ok": True, "queued": self._enqueue_files(files, "upload", on_conflict)}

    @staticmethod
    def _normalize_drop_path(p):
        """pywebview yields file-URL style paths (e.g. '/C:/Users/..') on Windows."""
        p = (p or "").strip()
        if os.name == "nt":
            if len(p) >= 3 and p[0] == "/" and p[2] == ":":
                p = p[1:]
            p = p.replace("/", "\\")
        return p

    def on_external_drop(self, event):
        """pywebview Qt drop handler: hand the real file paths back to the UI,
        which uploads them into the currently open remote folder."""
        try:
            files = ((event or {}).get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            debug.log("EXTERNAL DROP", {"paths": paths})
            if paths:
                self._emit("external_drop", {"paths": paths})
        except Exception as e:
            debug.log("external drop handler failed", str(e))

    def _run_transfer(self, files, direction, on_conflict):
        total = len(files)
        done = 0
        errors = []
        skipped = 0
        for lp, rp, _size in files:
            if self._cancel.is_set():
                break
            name = os.path.basename(lp)
            arrow = "↑" if direction == "upload" else "↓"
            ok = False
            res = None
            for attempt in range(3):
                try:
                    res = self._one(direction, lp, rp, name, done, total, on_conflict,
                                     self.sftp, cancel_check=lambda: self._cancel.is_set())
                    if res == "skip":
                        skipped += 1
                    ok = True
                    break
                except Exception as e:
                    debug.log(f"transfer retry {attempt+1}", f"{name}: {e}")
                    time.sleep(0.6)
            if ok:
                if res == "skip":
                    self._vlog(f"skip {name} (already up to date)")
                else:
                    self._vlog(f"{arrow} {(rp if direction == 'upload' else name)}", "ok")
            else:
                errors.append(name)
                self._vlog(f"failed {name}", "error")
            done += 1
        self._emit("transfer_done", {"total": total, "errors": errors, "skipped": skipped,
                                     "cancelled": self._cancel.is_set()})
        return {"ok": True, "total": total, "errors": errors, "skipped": skipped,
                "cancelled": self._cancel.is_set()}

    def _one(self, direction, lp, rp, name, idx, total, on_conflict, sftp,
             cancel_check=None, progress_key=None):
        """sftp is the session to transfer over: self.sftp for the legacy
        path, a worker's own session for the queue path. cancel_check is a
        callable returning True to stop the byte loop early; defaults to the
        legacy self._cancel flag. progress_key is what _progress() files this
        transfer's progress under (a queue item id, or the file name for the
        legacy path, which does not read it back by key)."""
        if cancel_check is None:
            def cancel_check():
                return self._cancel.is_set()
        if progress_key is None:
            progress_key = name
        # size-aware: skip identical when asked, otherwise resend the whole file
        if direction == "upload":
            src_size = os.path.getsize(lp)
            dst_size = self._rsize(sftp, rp)
        else:
            src_size = self._rsize(sftp, rp)
            dst_size = os.path.getsize(lp) if os.path.exists(lp) else -1
        if dst_size == src_size and src_size >= 0 and on_conflict == "skip":
            # user chose skip and the other side is the same size -> leave it.
            # overwrite deliberately falls through and resends, even on equal
            # size, since size alone does not prove the contents match.
            self._progress(name, idx, total, src_size, src_size, 0, progress_key)
            return "skip"
        # Always rewrite from the start. A smaller destination is not proof of
        # an interrupted transfer worth resuming: it is just as likely an older,
        # different file. Appending onto it would splice the old head onto the
        # new tail, and because that mangled file often ends up the same size as
        # the source, a later compare would read it as "same" and never fix it.
        # Sending fresh every time is the only safe rule size alone supports.
        offset = 0
        start = time.time()

        def cb(done_b, _t, base=offset):
            self._progress(name, idx, total, base + done_b, src_size, time.time() - start, progress_key)

        os.makedirs(os.path.dirname(lp), exist_ok=True) if direction == "download" else None
        if direction == "upload":
            finished = self._put_resume(sftp, lp, rp, offset, cb, cancel_check)
        else:
            finished = self._get_resume(sftp, rp, lp, offset, cb, cancel_check)
        # finished is False only if the byte loop broke early on cancel_check;
        # a transfer that sent every byte is "ok" even if cancel arrived a
        # moment later, so the caller must not re-check a cancel flag itself.
        return "ok" if finished else "cancelled"

    def _put_resume(self, sftp, lp, rp, offset, cb, cancel_check):
        # Check cancel_check() only once there is another chunk actually to
        # send, and only after confirming there is more file left (the read
        # came back non-empty). That way a cancel arriving in the instant
        # right after the last real chunk was already written finds nothing
        # left to abort: the next read hits EOF first and the loop exits with
        # finished=True, so a fully-sent file is never mislabeled cancelled.
        finished = True
        with open(lp, "rb") as src:
            src.seek(offset)
            with sftp.open(rp, "a" if offset else "w") as dst:
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
        return finished

    def _get_resume(self, sftp, rp, lp, offset, cb, cancel_check):
        # Same ordering as _put_resume, for the same reason: only treat a
        # cancel as having interrupted the transfer if there was still more
        # to read when it was observed.
        finished = True
        with sftp.open(rp, "r") as src:
            src.prefetch()
            src.seek(offset)
            with open(lp, "ab" if offset else "wb") as dst:
                got = 0
                while True:
                    chunk = src.read(32768)
                    if not chunk:
                        break
                    if cancel_check():
                        finished = False
                        break
                    dst.write(chunk)
                    got += len(chunk)
                    cb(got, 0)
        return finished

    def _progress(self, name, idx, total, sent, size, elapsed, progress_key=None):
        speed = (sent / elapsed) if elapsed > 0 else 0
        eta = ((size - sent) / speed) if speed > 0 and size > 0 else 0
        payload = {"name": name, "index": idx, "total": total,
                   "pct": int(sent * 100 / size) if size else 100,
                   "speed": human_size(speed) + "/s" if speed else "",
                   "eta": int(eta)}
        # Worker threads (queue path): keyed by item id, in-memory only, the
        # window polls instead. Legacy bridge-thread path (_run_transfer):
        # emit as before, nothing to key by since only one file is active.
        if self._poll_mode:
            with self._progress_lock:
                self._progress_by_id[progress_key] = payload
        else:
            self._emit("progress", payload)

    def _rsize(self, sftp, rp):
        try:
            return sftp.stat(rp).st_size
        except Exception:
            return -1

    def _walk_local(self, lp, rp):
        out = []
        for root, _dirs, fnames in os.walk(lp):
            rel = os.path.relpath(root, lp)
            rbase = rp if rel == "." else posixpath.join(rp, rel.replace("\\", "/"))
            try:
                self.sftp.mkdir(rbase)
            except Exception:
                pass
            for fn in fnames:
                full = os.path.join(root, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                out.append((full, posixpath.join(rbase, fn), size))
        return out

    def _walk_remote(self, rp, lp):
        out = []
        try:
            attrs = self.sftp.listdir_attr(rp)
        except Exception:
            return out
        os.makedirs(lp, exist_ok=True)
        for a in attrs:
            rchild = posixpath.join(rp, a.filename)
            lchild = os.path.join(lp, a.filename)
            if stat.S_ISDIR(a.st_mode):
                out += self._walk_remote(rchild, lchild)
            else:
                out.append((lchild, rchild, a.st_size))
        return out

    # ───────────── compare / sync plan / download-changed ─────────────
    def _scan_pair(self, local_dir, remote_dir):
        """Lists both sides of a folder pair into name -> (size, mtime) maps.
        Shared by compare() and sync_plan() so the two never drift apart."""
        loc = {}
        for n in os.listdir(local_dir):
            full = os.path.join(local_dir, n)
            if os.path.isfile(full):
                st = os.stat(full)
                loc[n] = (st.st_size, int(st.st_mtime))
        rem = {}
        for a in self.sftp.listdir_attr(remote_dir):
            if not stat.S_ISDIR(a.st_mode):
                rem[a.filename] = (a.st_size, int(a.st_mtime or 0))
        return loc, rem

    @staticmethod
    def _classify(name, loc, rem):
        if name in loc and name not in rem:
            return "local_only"
        if name in rem and name not in loc:
            return "remote_only"
        if loc[name][0] == rem[name][0]:
            return "same"
        return "newer_local" if loc[name][1] >= rem[name][1] else "newer_remote"

    def compare(self, local_dir, remote_dir):
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        try:
            loc, rem = self._scan_pair(local_dir, remote_dir)
            out = {n: self._classify(n, loc, rem) for n in set(loc) | set(rem)}
            return {"ok": True, "result": out}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def sync_plan(self, local_dir, remote_dir, direction, changed_only=True):
        """Computes what a sync would transfer, without transferring anything.
        The UI shows this plan and, on confirm, enqueues it onto the normal
        transfer queue (see enqueue())."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        try:
            loc, rem = self._scan_pair(local_dir, remote_dir)
            wanted = ("local_only", "newer_local") if direction == "upload" else ("remote_only", "newer_remote")
            plan = []
            for name in set(loc) | set(rem):
                status = self._classify(name, loc, rem)
                if status in wanted or (not changed_only and status != "same"):
                    l = {"size": loc[name][0], "mtime": loc[name][1]} if name in loc else None
                    r = {"size": rem[name][0], "mtime": rem[name][1]} if name in rem else None
                    plan.append({"name": name, "status": status, "local": l, "remote": r})
            return {"ok": True, "plan": plan}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def calc_remote_size(self, remote_dir, name):
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        target = posixpath.join(remote_dir, name)
        total = {"bytes": 0, "files": 0}
        self._cancel.clear()

        def walk(p):
            if self._cancel.is_set():
                return
            try:
                attrs = self.sftp.listdir_attr(p)
            except Exception:
                return
            for a in attrs:
                if self._cancel.is_set():
                    return
                if stat.S_ISDIR(a.st_mode):
                    walk(posixpath.join(p, a.filename))
                else:
                    total["bytes"] += a.st_size or 0
                    total["files"] += 1
                    if total["files"] % 50 == 0:
                        self._emit("size_progress", {"files": total["files"],
                                                     "bytes": human_size(total["bytes"])})
        try:
            walk(target)
            return {"ok": True, "bytes": total["bytes"], "human": human_size(total["bytes"]),
                    "files": total["files"]}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    # ───────────── keygen / install key ─────────────
    def default_key_path(self, key_type):
        name = "id_ed25519" if (key_type or "").startswith("Ed25519") else "id_rsa"
        return os.path.join(os.path.expanduser("~"), ".ssh", name)

    def browse_save_key(self, suggested):
        if not self._window:
            return ""
        try:
            dlg = webview.FileDialog.SAVE
        except AttributeError:  # older pywebview
            dlg = webview.SAVE_DIALOG
        res = self._window.create_file_dialog(
            dlg, save_filename=suggested or "id_ed25519")
        if not res:
            return ""
        return res if isinstance(res, str) else res[0]

    def browse_folder(self):
        if not self._window:
            return ""
        try:
            dlg = webview.FileDialog.FOLDER
        except AttributeError:  # older pywebview
            dlg = webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(dlg)
        if not res:
            return ""
        return res if isinstance(res, str) else res[0]

    def generate_key(self, key_type, out_path, passphrase):
        out_path = (out_path or "").strip().strip('"')
        if not out_path:
            return {"ok": False, "error": "Enter a save location for the key."}
        default_name = "id_ed25519" if key_type.startswith("Ed25519") else "id_rsa"
        # if a folder (or trailing slash) was given, drop the default key name in it
        if os.path.isdir(out_path) or out_path.endswith(("\\", "/")):
            out_path = os.path.join(out_path, default_name)
        created_dir = None
        parent = os.path.dirname(out_path) or "."
        if not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
                created_dir = parent
            except OSError:
                return {"ok": False, "error": f"Couldn't create the folder {parent} \u2014 choose a location you can write to."}
        try:
            if key_type.startswith("Ed25519"):
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                from cryptography.hazmat.primitives import serialization
                k = Ed25519PrivateKey.generate()
                enc = (serialization.BestAvailableEncryption(passphrase.encode())
                       if passphrase else serialization.NoEncryption())
                priv = k.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.OpenSSH, enc)
                pub = k.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                   serialization.PublicFormat.OpenSSH)
            else:
                key = paramiko.RSAKey.generate(4096)
                buf = io.StringIO()
                key.write_private_key(buf, password=passphrase or None)
                priv = buf.getvalue().encode()
                pub = f"ssh-rsa {key.get_base64()}".encode()
            with open(out_path, "wb") as f:
                f.write(priv)
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
            pubtext = pub.decode().strip() + " simple-sftp-client"
            with open(out_path + ".pub", "w", encoding="utf-8") as f:
                f.write(pubtext + "\n")
            debug.log("KEYGEN", {"type": key_type, "path": out_path})
            return {"ok": True, "public": pubtext, "private_path": out_path,
                    "public_path": out_path + ".pub", "created_dir": created_dir}
        except PermissionError:
            return {"ok": False, "error": "Couldn't write there (permission denied). Choose a folder you can write to, such as your user's .ssh folder."}
        except OSError as e:
            return {"ok": False, "error": f"Couldn't save the key: {e.strerror or 'write failed'}. Try a different location."}
        except Exception:
            return {"ok": False, "error": "Key generation failed. Check the type and passphrase and try again."}

    def install_pubkey(self, pubtext):
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        try:
            home = self.sftp.normalize(".")
            ssh_dir = posixpath.join(home, ".ssh")
            try:
                self.sftp.stat(ssh_dir)
            except Exception:
                self.sftp.mkdir(ssh_dir)
                self.sftp.chmod(ssh_dir, 0o700)
            ak = posixpath.join(ssh_dir, "authorized_keys")
            existing = ""
            try:
                with self.sftp.open(ak, "r") as f:
                    existing = f.read().decode()
            except Exception:
                pass
            if pubtext.split()[1] in existing:
                return {"ok": True, "already": True}
            with self.sftp.open(ak, "a") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + pubtext + "\n")
            self.sftp.chmod(ak, 0o600)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    # ───────────── upload watcher ─────────────
    def start_watch(self, local_dir, remote_dir):
        """Kept out of the queue this phase (roadmap Phase 2). _legacy_active is
        set only while a watch-triggered upload is actually running (not for the
        whole watch session), so a pane transfer can still be queued while
        watch is merely idling between its 2-second polls."""
        self.stop_watch()
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        if self.queue.pending() > 0:
            return {"ok": False, "error": "A transfer queue is active. Wait for it to finish."}
        self._watch_stop = threading.Event()

        def snapshot():
            snap = {}
            for root, _d, files in os.walk(local_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        snap[fp] = os.stat(fp).st_mtime
                    except Exception:
                        pass
            return snap

        def loop():
            last = snapshot()
            while not self._watch_stop.is_set():
                time.sleep(2)
                if self._watch_stop.is_set():
                    break
                cur = snapshot()
                changed = [fp for fp, m in cur.items() if last.get(fp) != m]
                # the queue worker owns self.sftp while it runs; never upload in
                # parallel over the same session. Hold these changes for a later
                # idle poll by leaving `last` unadvanced.
                if changed and self.queue.pending() > 0:
                    continue
                for fp in changed:
                    rel = os.path.relpath(fp, local_dir).replace("\\", "/")
                    rp = posixpath.join(remote_dir, rel)
                    self._legacy_active.set()
                    try:
                        rdir = posixpath.dirname(rp)
                        self._ensure_remote_dir(rdir)
                        self.sftp.put(fp, rp)
                        self._emit("watch", {"file": rel, "ok": True})
                    except Exception as e:
                        self._emit("watch", {"file": rel, "ok": False, "error": friendly_error(e)})
                    finally:
                        self._legacy_active.clear()
                last = cur

        self._watch_thread = threading.Thread(target=loop, daemon=True)
        self._watch_thread.start()
        debug.log("WATCH start", {"local": local_dir, "remote": remote_dir})
        return {"ok": True}

    def _ensure_remote_dir(self, path):
        parts = path.strip("/").split("/")
        cur = "/"
        for part in parts:
            cur = posixpath.join(cur, part)
            try:
                self.sftp.stat(cur)
            except Exception:
                try:
                    self.sftp.mkdir(cur)
                except Exception:
                    pass

    def stop_watch(self):
        if self._watch_stop:
            self._watch_stop.set()
        self._watch_stop = None
        return {"ok": True}

    # ───────────── update check ─────────────
    def check_update(self):
        """Compare the latest published release to APP_VERSION. Quiet in the UI on
        failure (see _update_error_reason), but always logged when debug is on."""
        result = {"current": APP_VERSION, "version": None, "update": False, "offline": False}
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = Request(url, headers={"User-Agent": "Simple-SFTP-Client",
                                        "Accept": "application/vnd.github+json"})
            with urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            latest = (data.get("tag_name") or "").lstrip("v")
            result["version"] = latest
            if latest and self._is_newer(latest, APP_VERSION):
                result["update"] = True
            debug.log(f"check_update: found v{latest}, current v{APP_VERSION}")
        except Exception as e:
            result["offline"] = True
            result["reason"] = _update_error_reason(e)
            debug.log(f"check_update failed: {type(e).__name__}: {e}")
        return result

    def _is_newer(self, latest, current):
        def parts(v):
            out = []
            for x in v.split("."):
                try:
                    out.append(int(x))
                except ValueError:
                    out.append(0)
            return out + [0] * (3 - len(out))
        try:
            return parts(latest) > parts(current)
        except Exception:
            return False

    def open_url(self, url):
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}


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

IS_SECOND_INSTANCE = False   # set True in main() when the user chooses to run a second copy

def _prompt_second_instance(app_title: str) -> bool:
    # Native message box only: runs before pywebview/Qt exists, so no Qt dialog is available yet.
    try:
        text = f"{app_title} is already running.\n\nOpen a second instance?"
        MB_YESNO_ICONQUESTION = 0x00000024
        result = ctypes.windll.user32.MessageBoxW(None, text, app_title, MB_YESNO_ICONQUESTION)
        return result == 6   # IDYES
    except Exception:
        return True   # fail open: if the box can't be shown, launch proceeds


def main():
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

    global IS_SECOND_INSTANCE
    if not _acquire_single_instance("JDE_SimpleSFTPClient_SingleInstance"):
        if not _prompt_second_instance("Simple SFTP Client"):
            sys.exit(0)
        IS_SECOND_INSTANCE = True

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JDEProjects.SimpleSFTPClient")
        except Exception:
            pass
    api = Api()
    window = webview.create_window(
        "Simple SFTP Client", url=resource_path("simple_sftp_client-UI.html"),
        js_api=api, width=1480, height=980, min_size=(1000, 700),
        background_color="#0a0e14")
    api.set_window(window)

    def _wire_external_drop():
        # Let users drag files in from Windows Explorer onto the remote pane.
        try:
            pane = window.dom.get_element("#paneRemote")
            if pane:
                pane.events.drop += api.on_external_drop
                debug.log("External drop wired on remote pane")
        except Exception as e:
            debug.log("wire external drop failed", str(e))
    window.events.loaded += _wire_external_drop

    # Geometry save/restore locates the window by enumerating this process's
    # own windows, so the lookup itself can't cross instances. Even so, a
    # second instance should not adopt or overwrite the first instance's
    # saved position. Only the first instance restores or saves geometry.
    if not IS_SECOND_INSTANCE:
        window.events.shown += lambda: _restore_geometry(window)

        def _on_closing():
            _save_geometry(window)
            return True
        window.events.closing += _on_closing

    try:
        webview.start(gui="qt", icon=resource_path("simple_sftp_client.png"))
    except TypeError:
        webview.start(gui="qt")


if __name__ == "__main__":
    main()
