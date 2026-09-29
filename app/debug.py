import logging
import re

from app import debug_log, paths

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


class AppDebugLog(debug_log.DebugLog):
    """Adds the paramiko protocol-log bridge on top of the shared DebugLog:
    attached whenever logging is on, detached the moment it goes off."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bridge = None  # paramiko logging handler, attached only while on

    def set_enabled(self, on):
        ok = super().set_enabled(on)
        self._set_paramiko(self.is_enabled())
        return ok

    def log(self, label, content=""):
        super().log(label, content)
        if self._bridge is not None and not self.is_enabled():
            self._set_paramiko(False)

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


debug = AppDebugLog(paths.exe_dir(), "Simple SFTP Client", redact=_scrub)
