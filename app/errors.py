import errno
import json
import socket
import ssl
import urllib.error

import paramiko

from app.debug import debug


class InvalidPort(ValueError):
    """A non-blank port value that is not a whole number from 1 to 65535."""
    def __init__(self, value):
        self.value = value
        super().__init__(f"Invalid port: {value!r}")


class UnknownHostKey(Exception):
    """First contact with a host whose key is not yet pinned."""
    def __init__(self, hostname, key):
        super().__init__("unknown host key")
        self.hostname = hostname
        self.key = key


class KnownHostsUnreadable(Exception):
    """The known_hosts file exists but could not be parsed. Connections are
    refused rather than treating it as empty, since that would silently drop
    protection against a swapped server."""
    def __init__(self, path):
        super().__init__(f"known_hosts file unreadable: {path}")
        self.path = path


class KeyUnusable(Exception):
    """The private key file chosen for key login could not be loaded, so the
    server was never contacted with it. reason is "passphrase_needed",
    "bad_passphrase", or "not_a_key"."""
    def __init__(self, reason, path):
        super().__init__(f"key unusable ({reason}): {path}")
        self.reason = reason
        self.path = path


class ScanIncomplete(Exception):
    """Raised by _compute_pair_maps when part of the tree could not be read
    during the walk (an unreadable folder, an unreadable file's metadata, or a
    lost connection mid-listing), so compare/sync refuse to report a result
    built on a tree that was not fully seen. It is also raised when a tree is
    larger than COMPARE_SYNC_ENTRY_LIMIT entries on either side, since compare
    and sync hold both sides in memory at once and refuse rather than risk
    running out of it partway through. An unsafe remote name is not one of
    these: it is skipped and logged, since it can never be represented
    locally and refusing would block a whole folder over one such name."""


def friendly_error(e):
    """Plain-language message for the UI; full detail goes to the debug log."""
    try:
        debug.log("error detail", f"{type(e).__name__}: {e}")
    except Exception:
        pass
    if isinstance(e, KeyUnusable):
        if e.reason == "passphrase_needed":
            return "This key is protected by a passphrase. Enter it and try again."
        if e.reason == "bad_passphrase":
            return "Couldn't unlock the key. Check the passphrase."
        return "That file isn't an SSH private key this app can read."
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
    if isinstance(e, KeyUnusable):
        if e.reason == "passphrase_needed":
            return ("The key file is encrypted with a passphrase.\n"
                    "• Type the key's passphrase in the Key passphrase box and connect again.")
        if e.reason == "bad_passphrase":
            return ("The key file could not be unlocked with the passphrase given.\n"
                    "• Re-type the passphrase, checking Caps Lock.\n"
                    "• Confirm this is the key the passphrase belongs to.\n"
                    "• If the passphrase is right, the key file may be damaged.")
        return ("The chosen file is not a private key in a format this app reads.\n"
                "• Choose the private key, not the .pub public key.\n"
                "• A PuTTY .ppk key must first be exported to OpenSSH format with PuTTYgen.")
    if isinstance(e, (TimeoutError, socket.timeout)):
        return ("The server didn't respond in time. Common causes:\n"
                "• The host address or port number is wrong.\n"
                "• A firewall is blocking the attempt, either on the server's network or in its operating system.\n"
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

