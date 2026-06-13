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
import json
import logging
import base64
import hashlib
import time
import shutil
import threading
import traceback
import webbrowser
import socket
import posixpath
from datetime import datetime
from urllib.request import Request, urlopen

import webview
import paramiko

APP_VERSION = "1.0.0"
GITHUB_REPO = "JDE-Projects/Simple-SFTP-Client"   # owner/repo for update checks

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

    def set_window(self, w):
        self._window = w

    def get_meta(self):
        return {
            "version": APP_VERSION,
            "key_types": ["Ed25519", "RSA-4096"],
            "sessions": self._load_sessions(),
        }

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
        sessions = [x for x in sessions if x.get("name") != s["name"]]
        sessions.append(s)
        sessions.sort(key=lambda x: x.get("name", "").lower())
        # optional remembered password -> OS keychain
        if s.get("remember") and self._cred_pass:
            try:
                import keyring
                keyring.set_password("SimpleSFTPClient", f"{s['host']}|{s['username']}", self._cred_pass)
            except Exception as e:
                debug.log("keyring set failed", str(e))
        self._save_sessions(sessions)
        return {"ok": True, "sessions": sessions}

    def delete_session(self, name):
        sessions = [x for x in self._load_sessions() if x.get("name") != name]
        self._save_sessions(sessions)
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
            self._pending_host_key = (e.hostname, e.key)
            debug.log(f"Unknown host key for {host} ({e.key.get_name()}).")
            return {"ok": False, "host_key_unknown": True, "host": host,
                    "key_type": e.key.get_name(), "fingerprint": fingerprint_sha256(e.key)}
        except paramiko.BadHostKeyException as e:
            self._pending_host_key = (e.hostname, e.key)
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
        self._cancel.set()
        return {"ok": True}

    def transfer(self, jobs, direction, local_dir, remote_dir, on_conflict="overwrite"):
        """jobs: list of {name, is_dir}. Expands dirs, transfers files with
        progress, resume (partial), skip-if-identical, and per-file retry."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        self._cancel.clear()
        files = []   # (local_path, remote_path)
        try:
            for j in jobs:
                if direction == "upload":
                    lp = os.path.join(local_dir, j["name"])
                    rp = posixpath.join(remote_dir, j["name"])
                    files += self._walk_local(lp, rp) if j["is_dir"] else [(lp, rp)]
                else:
                    rp = posixpath.join(remote_dir, j["name"])
                    lp = os.path.join(local_dir, j["name"])
                    files += self._walk_remote(rp, lp) if j["is_dir"] else [(lp, rp)]
        except Exception as e:
            return {"ok": False, "error": f"Could not enumerate: {e}"}
        return self._run_transfer(files, direction, on_conflict)

    def upload_paths(self, paths, remote_dir, on_conflict="overwrite"):
        """Upload absolute local paths (files or folders) dragged in from outside
        the app, into remote_dir, reusing the standard transfer pipeline."""
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        self._cancel.clear()
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
                    files.append((lp, rp))
        except Exception as e:
            return {"ok": False, "error": f"Could not read the dropped items: {e}"}
        if not files:
            return {"ok": False, "error": "No files were found in the dropped items."}
        debug.log("EXTERNAL UPLOAD", {"items": len(files), "remote": remote_dir})
        return self._run_transfer(files, "upload", on_conflict)

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
        for lp, rp in files:
            if self._cancel.is_set():
                break
            name = os.path.basename(lp)
            arrow = "↑" if direction == "upload" else "↓"
            ok = False
            res = None
            for attempt in range(3):
                try:
                    res = self._one(direction, lp, rp, name, done, total, on_conflict)
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

    def _one(self, direction, lp, rp, name, idx, total, on_conflict):
        # size-aware: skip identical, resume partial, else fresh
        if direction == "upload":
            src_size = os.path.getsize(lp)
            dst_size = self._rsize(rp)
        else:
            src_size = self._rsize(rp)
            dst_size = os.path.getsize(lp) if os.path.exists(lp) else -1
        if dst_size == src_size and src_size >= 0:
            if on_conflict == "skip" or on_conflict == "overwrite":
                # identical size -> treat as already transferred
                self._progress(name, idx, total, src_size, src_size, 0)
                return "skip"
        offset = dst_size if (0 < dst_size < src_size) else 0
        start = time.time()

        def cb(done_b, _t, base=offset):
            self._progress(name, idx, total, base + done_b, src_size, time.time() - start)

        os.makedirs(os.path.dirname(lp), exist_ok=True) if direction == "download" else None
        if direction == "upload":
            self._put_resume(lp, rp, offset, cb)
        else:
            self._get_resume(rp, lp, offset, cb)
        return "ok"

    def _put_resume(self, lp, rp, offset, cb):
        mode = "ab" if offset else "wb"
        with open(lp, "rb") as src:
            src.seek(offset)
            with self.sftp.open(rp, "a" if offset else "w") as dst:
                dst.set_pipelined(True)
                sent = 0
                while True:
                    if self._cancel.is_set():
                        break
                    chunk = src.read(32768)
                    if not chunk:
                        break
                    dst.write(chunk)
                    sent += len(chunk)
                    cb(sent, 0)

    def _get_resume(self, rp, lp, offset, cb):
        with self.sftp.open(rp, "r") as src:
            src.prefetch()
            src.seek(offset)
            with open(lp, "ab" if offset else "wb") as dst:
                got = 0
                while True:
                    if self._cancel.is_set():
                        break
                    chunk = src.read(32768)
                    if not chunk:
                        break
                    dst.write(chunk)
                    got += len(chunk)
                    cb(got, 0)

    def _progress(self, name, idx, total, sent, size, elapsed):
        speed = (sent / elapsed) if elapsed > 0 else 0
        eta = ((size - sent) / speed) if speed > 0 and size > 0 else 0
        self._emit("progress", {"name": name, "index": idx, "total": total,
                                "pct": int(sent * 100 / size) if size else 100,
                                "speed": human_size(speed) + "/s" if speed else "",
                                "eta": int(eta)})

    def _rsize(self, rp):
        try:
            return self.sftp.stat(rp).st_size
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
                out.append((os.path.join(root, fn), posixpath.join(rbase, fn)))
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
                out.append((lchild, rchild))
        return out

    # ───────────── compare / sync / download-changed ─────────────
    def compare(self, local_dir, remote_dir):
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
        try:
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
            out = {}
            for n in set(loc) | set(rem):
                if n in loc and n not in rem:
                    out[n] = "local_only"
                elif n in rem and n not in loc:
                    out[n] = "remote_only"
                elif loc[n][0] == rem[n][0]:
                    out[n] = "same"
                else:
                    out[n] = "newer_local" if loc[n][1] >= rem[n][1] else "newer_remote"
            return {"ok": True, "result": out}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def sync(self, local_dir, remote_dir, direction, changed_only=True):
        cmp = self.compare(local_dir, remote_dir)
        if not cmp.get("ok"):
            return cmp
        res = cmp["result"]
        jobs = []
        for name, status in res.items():
            if direction == "upload":
                if status in ("local_only", "newer_local") or (not changed_only and status != "same"):
                    jobs.append({"name": name, "is_dir": False})
            else:
                if status in ("remote_only", "newer_remote") or (not changed_only and status != "same"):
                    jobs.append({"name": name, "is_dir": False})
        if not jobs:
            return {"ok": True, "total": 0, "errors": [], "skipped": 0, "cancelled": False}
        return self.transfer(jobs, direction, local_dir, remote_dir)

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
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=suggested or "id_ed25519")
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
        self.stop_watch()
        if not self.connected:
            return {"ok": False, "error": "Not connected."}
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
                for fp in changed:
                    rel = os.path.relpath(fp, local_dir).replace("\\", "/")
                    rp = posixpath.join(remote_dir, rel)
                    try:
                        rdir = posixpath.dirname(rp)
                        self._ensure_remote_dir(rdir)
                        self.sftp.put(fp, rp)
                        self._emit("watch", {"file": rel, "ok": True})
                    except Exception as e:
                        self._emit("watch", {"file": rel, "ok": False, "error": friendly_error(e)})
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
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = Request(url, headers={"User-Agent": "Simple-SFTP-Client",
                                        "Accept": "application/vnd.github+json"})
            with urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            tag = (data.get("tag_name") or "").lstrip("v")
            newer = self._is_newer(tag, APP_VERSION)
            return {"ok": True, "current": APP_VERSION, "latest": tag, "update": newer,
                    "notes": (data.get("body") or "")[:1500], "url": data.get("html_url", "")}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

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


# ───────────── splash + main ─────────────
try:
    import pyi_splash  # type: ignore
    HAS_SPLASH = True
except Exception:
    HAS_SPLASH = False

_splash_lock = threading.Lock()
_splash_done = False
_start = time.time()


def _close_splash():
    global _splash_done
    with _splash_lock:
        if _splash_done:
            return
        _splash_done = True
    if HAS_SPLASH:
        try:
            pyi_splash.close()
        except Exception:
            pass


def _on_loaded():
    threading.Timer(max(0.0, 5.0 - (time.time() - _start)), _close_splash).start()


def main():
    if HAS_SPLASH:
        threading.Timer(30.0, _close_splash).start()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JDEProjects.SimpleSFTPClient")
        except Exception:
            pass
    api = Api()
    window = webview.create_window(
        "Simple SFTP Client", url=resource_path("simple_sftp_client-UI.html"),
        js_api=api, width=1480, height=980, min_size=(1180, 800),
        background_color="#0a0e14")
    api.set_window(window)
    window.events.loaded += _on_loaded

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
    try:
        webview.start(gui="qt", icon=resource_path("simple_sftp_client.png"))
    except TypeError:
        webview.start(gui="qt")


if __name__ == "__main__":
    main()
