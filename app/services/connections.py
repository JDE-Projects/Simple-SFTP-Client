"""Service functions for the connections area."""

import os
import traceback
import socket
import paramiko
from app import paths
from app.constants import DISABLED_ALGORITHMS
from app.debug import debug
from app.errors import InvalidPort, KeyUnusable, KnownHostsUnreadable, UnknownHostKey, error_tips, friendly_error
from app.formatting import negotiated_summary
from app.hostkeys import _TofuPolicy, _known_hosts_readable_or_raise, _save_host_keys_atomic, fingerprint_sha256, hostkey_name, load_known_hosts
from app.paths import is_temp_part
from app.validation import INVALID_PORT_ERROR, missing_fields, parse_port


def _load_private_key(key_path, passphrase):
    """Load the private key for key login, or raise KeyUnusable saying why.

    paramiko's own key_filename login tries each key type in turn and reports
    only the last failure (a wrong passphrase on an RSA key reads "encountered
    RSA key, expected OPENSSH key"), so the key is loaded here, where the cases
    can be told apart. A wrong passphrase and a damaged key file raise the same error for
    some key types, so a damaged file with a passphrase given reads as
    bad_passphrase. A missing or unreadable file raises its OSError unchanged.
    Loads a matching "-cert.pub" certificate if present, the same as paramiko's key_filename login."""
    with open(key_path, "rb") as f:
        data = f.read()
    needs_passphrase = False
    last = None
    for cls in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
        try:
            key = cls.from_private_key_file(key_path, password=passphrase or None)
        except paramiko.PasswordRequiredException:
            needs_passphrase = True
            continue
        except (paramiko.SSHException, ValueError) as e:
            last = e
            continue
        cert_path = key_path + "-cert.pub"
        if os.path.isfile(cert_path):
            key.load_certificate(cert_path)
        return key
    debug.log("key load failed", f"{key_path}: {type(last).__name__}: {last}")
    if needs_passphrase:
        raise KeyUnusable("passphrase_needed", key_path)
    if passphrase and b"PRIVATE KEY-----" in data:
        raise KeyUnusable("bad_passphrase", key_path)
    raise KeyUnusable("not_a_key", key_path)


def _open(api, host, port, username, password, key_path, passphrase):
    client = paramiko.SSHClient()
    _known_hosts_readable_or_raise()
    if os.path.exists(paths.KNOWN_HOSTS_FILE):
        client.load_host_keys(paths.KNOWN_HOSTS_FILE)
    # Trust on first use: unknown hosts raise UnknownHostKey (user is asked),
    # a changed key raises paramiko.BadHostKeyException (flagged, not trusted).
    client.set_missing_host_key_policy(_TofuPolicy())
    kwargs = dict(hostname=host, port=parse_port(port), username=username,
                  timeout=15, allow_agent=False, look_for_keys=False,
                  disabled_algorithms=DISABLED_ALGORITHMS)
    if key_path:
        kwargs["pkey"] = _load_private_key(key_path, passphrase)
    else:
        kwargs["password"] = password
    try:
        client.connect(**kwargs)
    except Exception:
        # Any failure here, including the host-key exceptions the TOFU
        # policy raises back through connect(), can still leave a
        # partially opened transport on this freshly created client.
        # Close it before the exception reaches connect(), which never
        # touches self._client/self._sftp on failure (see _close_partial).
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def _close_partial(api, client, sftp):
    """Close a client/sftp pair from a connect attempt that did not fully
    succeed, without ever touching self._client/self._sftp. Used so a
    failed connect (or a failed reconnect while a prior attempt's
    resources are still being torn down) can never leak a socket or
    clobber a still-good prior connection's state."""
    if sftp is not None:
        try:
            sftp.close()
        except Exception:
            pass
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def connect(api, p):
    miss = missing_fields(p)
    if miss:
        return {"ok": False, "error": miss}
    try:
        port = parse_port(p.get("port"))
    except InvalidPort:
        return {"ok": False,
                "error": INVALID_PORT_ERROR}
    host = (p.get("host") or "").strip()
    username = (p.get("username") or "").strip()
    password = p.get("password") or ""
    key_path = (p.get("key_path") or "").strip()
    passphrase = p.get("passphrase") or ""
    debug.log("CONNECT", {"host": host, "user": username, "auth": "key" if key_path else "password"})
    new_client = None
    new_sftp = None
    try:
        new_client = api._open(host, port, username, password, key_path, passphrase)
        new_sftp = new_client.open_sftp()
        # Run every remaining fallible setup step on the new, still-
        # unpublished objects. Home normalization can fail (a dropped
        # transport, a server that refuses the request); if it does, the
        # except handlers below close new_client/new_sftp and never touch
        # self.*, so a failure here can neither report a closed session as
        # connected nor clobber a prior good connection during a reconnect.
        ti = api._transport_info(new_client)
        home = new_sftp.normalize(".")
        start = (p.get("start_path") or "").strip() or home
        try:
            new_sftp.stat(start)
        except Exception:
            start = home
        # Commit only now that all setup has fully succeeded.
        api._client = new_client
        api._sftp = new_sftp
        api._connected = True
        api._shutdown_done = False
        api._conn_dead_reported = False
        # Cache the password only now that the login has fully succeeded,
        # and only for password auth (key auth has no password to remember).
        # Stamp the identity it authenticated against so save_session can
        # confirm a later save matches this exact server.
        if key_path:
            api._cred_pass = ""
            api._cred_identity = None
        else:
            api._cred_pass = password
            api._cred_identity = (host, port, username, "password")
        negotiated = negotiated_summary(ti)
        if negotiated:
            api._vlog(f"Negotiated: {negotiated}")
        api._vlog(f"SFTP session opened, home folder {home}", "ok")
        api._sweep_scratch_files()
        return {"ok": True, "home": home, "cwd": start, "transport": ti}
    except UnknownHostKey as e:
        api._cred_pass = ""
        api._cred_identity = None
        api._close_partial(new_client, new_sftp)
        # Pin under the port-aware name paramiko actually checks against
        # (bracketed for non-standard ports), not e.hostname, whose format
        # differs between the unknown-key and changed-key paths.
        api._pending_host_key = (hostkey_name(host, port), e.key)
        debug.log(f"Unknown host key for {host} ({e.key.get_name()}).")
        return {"ok": False, "host_key_unknown": True, "host": host,
                "key_type": e.key.get_name(), "fingerprint": fingerprint_sha256(e.key)}
    except paramiko.BadHostKeyException as e:
        api._cred_pass = ""
        api._cred_identity = None
        api._close_partial(new_client, new_sftp)
        # Same port-aware name as above. The changed-key path hands back a
        # bare host in e.hostname, so pinning by that would store the new
        # key under a name the library never rechecks, and the warning would
        # loop forever on non-standard ports.
        api._pending_host_key = (hostkey_name(host, port), e.key)
        debug.log(f"HOST KEY CHANGED for {host} - refused.")
        return {"ok": False, "host_key_changed": True, "host": host,
                "key_type": e.key.get_name(),
                "new_fingerprint": fingerprint_sha256(e.key),
                "old_fingerprint": fingerprint_sha256(e.expected_key)}
    except KnownHostsUnreadable as e:
        api._cred_pass = ""
        api._cred_identity = None
        api._close_partial(new_client, new_sftp)
        debug.log(f"known_hosts file unreadable, refusing to connect: {e}")
        return {"ok": False,
                "error": f"Your saved host-key file could not be read, so the connection was "
                         f"refused to protect against a swapped server. File: {paths.KNOWN_HOSTS_FILE}. "
                         "You can delete it to start fresh, which just means confirming your "
                         "hosts again on the next connect."}
    except Exception as e:
        api._cred_pass = ""
        api._cred_identity = None
        api._close_partial(new_client, new_sftp)
        debug.log("CONNECT failed", traceback.format_exc())
        return {"ok": False, "error": friendly_error(e), "tips": error_tips(e)}


def _sweep_scratch_files(api):
    """Remove leftover .sxtpart scratch files (task 3's is_temp_part /
    local_temp_path) from the local folder currently browsed. Scoped to
    that one folder, not a recursive or whole-drive sweep: it is the
    only place this app's own scratch files are ever written (a sibling
    of the real file mid-download), so a narrower sweep can't be right
    and a wider one risks touching a folder the user never asked about.
    Runs after every successful connect in case the app was killed or
    lost power mid-transfer and never reached its own atomic-swap
    cleanup or the delete in the download failure path."""
    folder = api._local_cwd or os.path.expanduser("~")
    if not folder or folder == "DRIVES" or not os.path.isdir(folder):
        return
    swept = []
    try:
        names = os.listdir(folder)
    except Exception as e:
        debug.log("scratch sweep: could not list folder", f"{folder}: {e}")
        return
    for name in names:
        if not is_temp_part(name):
            continue
        full = os.path.join(folder, name)
        try:
            os.remove(full)
            swept.append(name)
        except Exception as e:
            debug.log("scratch sweep: could not remove", f"{full}: {e}")
    if swept:
        api._vlog(f"Swept {len(swept)} leftover scratch file(s) from {folder}", "warn")
        debug.log("scratch sweep removed", swept)


def trust_host_key(api):
    """Pin the host key the user just confirmed, then they may reconnect."""
    pending = api._pending_host_key
    api._pending_host_key = None
    if not pending:
        return {"ok": False, "error": "No host key is waiting to be trusted."}
    name, key = pending
    try:
        hk = load_known_hosts()
    except KnownHostsUnreadable as e:
        return {"ok": False,
                "error": f"The saved host-key file is unreadable, so it was left untouched. "
                         f"File: {e.path}. Delete it to start fresh."}
    try:
        if hk.lookup(name):          # replace any prior key for this host
            del hk[name]
        hk.add(name, key.get_name(), key)
        if not _save_host_keys_atomic(hk):
            return {"ok": False, "error": "Could not save the host key: write failed."}
        debug.log(f"Trusted host key for {name} ({key.get_name()}).")
        return {"ok": True, "fingerprint": fingerprint_sha256(key)}
    except Exception as e:
        return {"ok": False, "error": f"Could not save the host key: {e}"}


def get_host_key(api, host, port=22):
    """Return the pinned key(s) for a host so the UI can show them."""
    host = (host or "").strip()
    try:
        name = hostkey_name(host, port) if host else ""
    except InvalidPort:
        return {"known": False, "host": host,
                "error": INVALID_PORT_ERROR}
    try:
        sub = load_known_hosts().lookup(name) if name else None
    except KnownHostsUnreadable as e:
        return {"known": False, "unreadable": True, "host": host,
                "error": f"The saved host-key file is unreadable, so it was left untouched. "
                         f"File: {e.path}. Delete it to start fresh."}
    if not sub:
        return {"known": False, "host": host}
    entries = [{"key_type": kt, "fingerprint": fingerprint_sha256(k)}
               for kt, k in sub.items()]
    return {"known": True, "host": host, "entries": entries}


def _transport_info(api, client=None):
    # Key exchange is not reported: paramiko discards the agreed method,
    # and the server offer it was chosen from, once the handshake ends.
    try:
        t = (client or api._client).get_transport()
        return {"cipher": t.remote_cipher, "mac": t.remote_mac}
    except Exception:
        return {}


def test_connection(api, p):
    """Reachability check only: open a TCP socket and read the SSH banner.
    Confirms host/port reachable and that an SSH server answers. No host
    key check and no authentication (that is Connect's job)."""
    host = (p.get("host") or "").strip()
    if not host:
        return {"ok": False, "error": "Enter a host to test."}
    try:
        port = parse_port(p.get("port"))
    except InvalidPort:
        return {"ok": False,
                "error": INVALID_PORT_ERROR}
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
        api._vlog(f"Test: {host}:{port} reachable ({ident})", "ok")
        return {"ok": True, "msg": f"{host}:{port} reachable ({ident})"}
    return {"ok": False, "warn": True,
            "error": f"Something is listening on {host}:{port}, but it didn't identify as an "
                     "SSH/SFTP server.",
            "tips": ("Confirm this is the SFTP/SSH port (often 22). A different service may be "
                     "answering on it.")}


def disconnect(api):
    """Wired to the Disconnect button. shutdown() is the one teardown
    path; this just runs it and gives the UI the return shape it
    expects."""
    api.shutdown()
    debug.log("DISCONNECTED")
    return {"ok": True}


def _connection_dead(api, sftp):
    """True when the shared transport, or this worker's own SFTP
    channel, is actually gone, as distinct from a single item's normal
    error (missing file, permission denied, a bad remote path), which
    leaves the connection perfectly fine. Used by the retry loop in
    _worker_loop to stop a whole batch at once instead of retrying every
    remaining item into a wall of per-file errors."""
    try:
        transport = api._client.get_transport() if api._client else None
        if transport is None or not transport.is_active():
            return True
    except Exception:
        return True
    try:
        chan = sftp.get_channel() if sftp else None
        if chan is None or chan.closed or not chan.active:
            return True
    except Exception:
        return True
    return False


def _report_dead_connection(api):
    """First worker to notice the connection died mid-batch logs one
    clear console line and fails every still-WAITING queue item
    immediately (skipping their retries), instead of letting each
    worker's current item and every remaining item grind through three
    retries each. Guarded so multiple workers hitting the same dead
    transport only report this once; cleared again on the next
    successful connect()."""
    with api._conn_dead_lock:
        if api._conn_dead_reported:
            return
        api._conn_dead_reported = True
    api._stop_all_scans()
    api._stop_all_compares()
    stranded = api._queue.fail_waiting("Connection lost")
    suffix = f" ({stranded} queued item(s) failed)" if stranded else ""
    api._worker_log(f"Connection lost. Remaining transfers stopped.{suffix}", "error")
