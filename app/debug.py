import re

from app import debug_log, paths
from app.debug_log import describe_error

# ───────────── debug log (off by default) ─────────────
_URL_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)[^\s@]+(@)")

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


def _scrub(text):
    """Mask URL-embedded passwords and private-key blocks before they hit the log."""
    text = _URL_CREDS_RE.sub(r"\1[redacted]\2", text)
    text = _PRIVATE_KEY_RE.sub("[redacted]", text)
    return text


EVENTS = {
    "debug enabled": {}, "debug disabled": {}, "connect": {"auth": ("key", "password")},
    "connected": {}, "connect failed": {"error": "error"}, "error": {"error": "error"},
    "key load failed": {"error": "error"},
    "host key unknown": {}, "host key changed": {}, "known hosts unreadable": {"error": "error"},
    "host key trusted": {}, "test connection": {}, "disconnected": {},
    "scratch sweep list failed": {"error": "error"}, "scratch sweep remove failed": {"error": "error"},
    "scratch sweep": {"items": int}, "watch start": {}, "watch upload": {"outcome": ("ok", "failed"), "error": "error"},
    "watch stop delayed": {}, "update check": {"outcome": ("newer", "current", "failed"), "error": "error"},
    "open url refused": {}, "transfer": {"outcome": ("ok", "failed", "cancelled", "skipped"), "error": "error"},
    "transfer retry": {"attempt": int, "error": "error"}, "external upload": {"items": int},
    "external drop": {"items": int}, "external drop wired": {}, "external drop failed": {"error": "error"},
    "scan": {"outcome": ("complete", "stopped", "disconnected", "failed"), "items": int, "error": "error"},
    "list failed": {"error": "error"},
    "compare failed": {"error": "error"}, "sync": {"outcome": ("complete", "stopped", "disconnected", "failed"), "items": int, "error": "error"},
    "keygen": {"outcome": ("ok", "failed", "restore failed"), "error": "error"},
    "key protection failed": {"returncode": int, "error": "error"},
    "prefs unreadable": {"error": "error"}, "atomic write failed": {"error": "error"},
    "atomic unreadable": {"error": "error"}, "known hosts write failed": {"error": "error"},
    "session unreadable": {"error": "error"}, "sessions notice": {}, "session backup failed": {"error": "error"},
    "keyring failed": {"outcome": ("set", "rollback", "import", "delete"), "error": "error"},
    "shutdown workers delayed": {"items": int}, "shutdown close errors": {"items": int}, "shutdown complete": {"outcome": ("clean", "workers delayed")},
    "remote list": {"items": int}, "remote mkdir": {}, "remote rename": {}, "remote remove": {}, "remote rmdir": {},
    "connection lost": {"items": int}, "unknown event": {},
    "queue": {"outcome": ("paused", "resumed", "drained", "session failed"), "items": int},
    "unsafe remote name": {"error": "error"}, "symlink target": {},
    "mtime": {"outcome": ("missing", "local failed", "remote failed"), "error": "error"},
}

debug = debug_log.DebugLog(paths.exe_dir(), "Simple SFTP Client", redact=_scrub)


def log_event(event, **fields):
    """Write only schema-checked, non-user-controlled debug fields."""
    try:
        spec = EVENTS.get(event)
        if spec is None:
            debug._log("unknown event")
            return
        accepted = []
        for name, value in fields.items():
            allowed = spec.get(name)
            if allowed is int:
                valid = isinstance(value, int) and not isinstance(value, bool)
                rendered = str(value) if valid else "rejected"
            elif allowed is bool:
                valid = isinstance(value, bool)
                rendered = str(value).lower() if valid else "rejected"
            elif allowed == "error":
                valid = isinstance(value, BaseException)
                rendered = describe_error(value) if valid else "rejected"
            else:
                valid = isinstance(allowed, tuple) and isinstance(value, str) and value in allowed
                rendered = value if valid else "rejected"
            accepted.append(f"{name}={rendered}")
        debug._log(event, " ".join(accepted))
    except Exception:
        try:
            debug._log("unknown event")
        except Exception:
            pass
