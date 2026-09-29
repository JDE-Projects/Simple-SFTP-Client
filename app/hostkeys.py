import base64
import hashlib
import os
import tempfile

import paramiko

from app.debug import debug
from app import paths
from app.errors import KnownHostsUnreadable, UnknownHostKey
from app.validation import parse_port


def hostkey_name(host, port):
    """The name paramiko stores a host key under (bracketed when not port 22)."""
    port = parse_port(port)
    return host if port == 22 else "[%s]:%d" % (host, port)


def fingerprint_sha256(key):
    """OpenSSH-style SHA256 fingerprint, e.g. 'SHA256:abc...' (no padding)."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _known_hosts_readable_or_raise():
    """Paramiko silently skips lines it can't parse, so a corrupt file would
    otherwise look empty and get treated as first contact with every host,
    which is exactly the swapped-server case host-key pinning exists to
    catch. Read the file ourselves line by line and raise if any non-blank,
    non-comment line fails to parse. A missing file is clean first contact,
    not corruption."""
    if not os.path.exists(paths.KNOWN_HOSTS_FILE):
        return
    try:
        with open(paths.KNOWN_HOSTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        raise KnownHostsUnreadable(paths.KNOWN_HOSTS_FILE) from e
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
        except Exception:
            entry = None
        if entry is None:
            raise KnownHostsUnreadable(paths.KNOWN_HOSTS_FILE)


def load_known_hosts():
    _known_hosts_readable_or_raise()
    hk = paramiko.HostKeys()
    if os.path.exists(paths.KNOWN_HOSTS_FILE):
        hk.load(paths.KNOWN_HOSTS_FILE)
    return hk


def _save_host_keys_atomic(hk) -> bool:
    """Save a paramiko HostKeys object atomically: write to a temp file in
    the same folder, then os.replace over the real known_hosts file so a
    reader never sees a half-written file. False result must surface a
    visible error, not silence."""
    folder = os.path.dirname(paths.KNOWN_HOSTS_FILE) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp_", suffix=".tmp")
        os.close(fd)
        hk.save(tmp)
        os.replace(tmp, paths.KNOWN_HOSTS_FILE)
        return True
    except Exception as e:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        try:
            debug.log(f"Could not write {os.path.basename(paths.KNOWN_HOSTS_FILE)}: {e}")
        except Exception:
            pass
        return False


class _TofuPolicy(paramiko.MissingHostKeyPolicy):
    """Do not auto-add. Surface the offered key so the UI can ask the user."""
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)

