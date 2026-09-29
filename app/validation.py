from app.errors import InvalidPort


def parse_port(port):
    """The one strict port parser, used everywhere a port is interpreted.
    Blank (empty string or None) means the default SFTP port, 22. Anything
    else must be a plain whole number from 1 to 65535, with no surrounding
    whitespace and no sign or decimal point, or it raises InvalidPort - it
    never silently falls back to 22 for a non-blank value."""
    if port is None:
        return 22
    if isinstance(port, bool):
        raise InvalidPort(port)
    if isinstance(port, int):
        value = port
    elif isinstance(port, str):
        stripped = port.strip()
        if not stripped:
            return 22
        # isdigit() alone accepts Unicode digits like superscripts that int()
        # then rejects, which would leak a raw ValueError past every caller's
        # InvalidPort guard; require plain ASCII 0-9 so int() below can't fail.
        if stripped != port or not (stripped.isascii() and stripped.isdigit()):
            raise InvalidPort(port)
        value = int(stripped)
    else:
        raise InvalidPort(port)
    if not (1 <= value <= 65535):
        raise InvalidPort(port)
    return value


def cred_key(host, port, username):
    """Credential Manager entry name for a saved password. Mirrors
    hostkey_name: bare host|username on port 22, host|port|username on any
    other port, so existing default-port entries keep working unchanged and
    two services on the same host/user but different ports never collide."""
    port = parse_port(port)
    host = (host or "").strip()
    username = (username or "").strip()
    if port == 22:
        return f"{host}|{username}"
    return f"{host}|{port}|{username}"


def _valid_session_entry(x):
    """A saved session entry is safe to hand to the UI (or a connect
    attempt) only once every field is the type the UI expects: this is what
    keeps a hand-edited or corrupted servers.json from injecting markup or
    an unsupported auth mode into the session manager. Anything that fails
    here gets dropped by the caller, not repaired."""
    if not isinstance(x, dict):
        return False
    name = x.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    for key in _SESSION_STR_FIELDS:
        if key in x and not isinstance(x[key], str):
            return False
    if x.get("auth") not in _SESSION_AUTH_VALUES:
        return False
    if "remember" in x and not isinstance(x["remember"], bool):
        return False
    try:
        parse_port(x.get("port"))
    except InvalidPort:
        return False
    return True


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


INVALID_PORT_ERROR = "Enter a valid port (1-65535), or leave it blank for the default (22)."

_SESSION_STR_FIELDS = ("host", "username", "key_path", "start_path")

_SESSION_AUTH_VALUES = ("password", "key")

