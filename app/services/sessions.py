"""Service functions for the sessions area."""

import json
from app import paths
from app.atomic import _atomic_write_json, _preserve_corrupt
from app.debug import debug
from app.errors import InvalidPort
from app.validation import INVALID_PORT_ERROR, _valid_session_entry, cred_key, parse_port


def _load_sessions(api):
    """Load saved sessions and validate every entry before it can reach
    the UI or a connect attempt. The file itself failing to parse is
    handled exactly as before (kept aside via _preserve_corrupt); this
    additionally drops individual entries that parsed fine as JSON but
    have the wrong shape, so nothing malformed or hostile (e.g. a bad
    port or an unsupported auth value) ever gets that far. Dropped
    entries are never rewritten back to servers.json - the file is left
    untouched either way."""
    try:
        with open(paths.SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        _preserve_corrupt(paths.SESSIONS_FILE, e)
        return []
    if not isinstance(data, dict):
        api._sessions_notice("Saved sessions file has an unexpected format and was ignored.")
        return []
    raw = data.get("sessions", [])
    if not isinstance(raw, list):
        api._sessions_notice("Saved sessions file has an unexpected format and was ignored.")
        return []
    valid = [x for x in raw if _valid_session_entry(x)]
    dropped = len(raw) - len(valid)
    if dropped:
        word = "entry" if dropped == 1 else "entries"
        api._sessions_notice(
            f"Skipped {dropped} saved session {word} that could not be read. "
            f"{paths.SESSIONS_FILE} was left unchanged.")
    return valid


def _sessions_notice(api, msg):
    """One-line visible notice plus a debug-log trace for a saved-session
    problem noticed at load time, without silently dropping data unseen."""
    try:
        api._vlog(msg, "warn")
    except Exception:
        debug.log(msg)


def _save_sessions(api, sessions):
    return _atomic_write_json(paths.SESSIONS_FILE,
                               {"_note": "Simple SFTP Client saved sessions (no passwords).",
                                "sessions": sessions}, indent=2)


def save_session(api, s):
    s = {k: s.get(k, "") for k in ("name", "host", "port", "username",
                                   "auth", "key_path", "start_path", "remember")}
    try:
        port = parse_port(s.get("port"))
    except InvalidPort:
        return {"ok": False,
                "error": INVALID_PORT_ERROR}
    sessions = api._load_sessions()
    # optional remembered password -> OS keychain. The session may only
    # claim a saved password when one was actually written, so a failed
    # write, or "remember" ticked with no password to save (key auth, or
    # saving before connecting), both leave remember off. A password is
    # only ever written when it was cached from a successful login
    # against these exact settings (see self._cred_identity), so saving
    # after editing a field or against the wrong server is refused
    # rather than misfiled under the new name.
    pw_saved = False
    pw_error = None
    had_prior = False
    prior = None
    proposed = (s.get("host", "").strip(), port,
                s.get("username", "").strip(), s.get("auth"))
    if s.get("remember") and s.get("auth") == "password":
        if api._cred_pass and api._cred_identity == proposed:
            try:
                import keyring
                cred_name = cred_key(s.get("host"), s.get("port"), s.get("username"))
                prior = keyring.get_password("SimpleSFTPClient", cred_name)
                had_prior = prior is not None
                keyring.set_password("SimpleSFTPClient", cred_name, api._cred_pass)
                pw_saved = True
            except Exception as e:
                debug.log("keyring set failed", str(e))
                pw_error = f"Could not save the password to Windows Credential Manager: {e}"
        else:
            pw_error = "Password not saved. Connect successfully with these exact settings first, then save."
    if s.get("remember") and not pw_saved:
        s["remember"] = False
    sessions = [x for x in sessions if x.get("name") != s["name"]]
    sessions.append(s)
    sessions.sort(key=lambda x: x.get("name", "").lower())
    saved_ok = api._save_sessions(sessions)
    if not saved_ok:
        rollback_failed = False
        if pw_saved:
            # The session file write failed, so undo the keychain write:
            # restore whatever was there before rather than erasing it,
            # or delete the newly created entry if nothing was there.
            try:
                import keyring
                cred_name = cred_key(s.get("host"), s.get("port"), s.get("username"))
                if had_prior:
                    keyring.set_password("SimpleSFTPClient", cred_name, prior)
                else:
                    keyring.delete_password("SimpleSFTPClient", cred_name)
            except Exception as e:
                debug.log("keyring rollback failed", str(e))
                rollback_failed = True
        if rollback_failed:
            return {"ok": False,
                    "error": "Could not save the session, and restoring the previous saved "
                             "password may have failed. Check the saved password for this server."}
        return {"ok": False,
                "error": f"Could not save the session to {paths.SESSIONS_FILE}. Nothing was changed."}
    result = {"ok": True, "sessions": sessions, "pw_saved": pw_saved}
    if pw_error:
        result["pw_error"] = pw_error
    return result


def delete_session(api, name):
    sessions = api._load_sessions()
    target = next((x for x in sessions if x.get("name") == name), None)
    sessions = [x for x in sessions if x.get("name") != name]
    saved_ok = api._save_sessions(sessions)
    if not saved_ok:
        # The session still references its keyring entry, so leave the
        # entry alone rather than orphan it.
        return {"ok": False,
                "error": f"Could not update {paths.SESSIONS_FILE}. The session was not removed."}
    failed_names = []
    if target and target.get("remember"):
        host = target.get("host")
        port = target.get("port")
        username = target.get("username")
        names = [cred_key(host, port, username)]
        if parse_port(port) != 22:
            names.append(f"{(host or '').strip()}|{(username or '').strip()}")
        try:
            import keyring
            import keyring.errors
        except Exception as e:
            debug.log("keyring import failed", str(e))
            failed_names = list(names)
        else:
            for cred_name in names:
                try:
                    keyring.delete_password("SimpleSFTPClient", cred_name)
                except keyring.errors.PasswordDeleteError:
                    # No matching credential was found, which is fine:
                    # it was either never saved or already removed.
                    pass
                except Exception as e:
                    debug.log("keyring delete failed", f"{cred_name}: {e}")
                    failed_names.append(cred_name)
    result = {"ok": True, "sessions": sessions}
    if failed_names:
        entries = "\n".join(
            f"Generic Credentials, entry for SimpleSFTPClient with user name {n}"
            for n in failed_names)
        result["pw_warning"] = (
            "The saved password could not be removed from Windows Credential "
            "Manager. Delete it by hand:\n" + entries)
    return result


def _remembered_password(api, host, username, port=22):
    try:
        import keyring
        pw = keyring.get_password("SimpleSFTPClient", cred_key(host, port, username))
        if not pw and parse_port(port) != 22:
            # Legacy entries were saved port-less; read them so a saved
            # password from before port-aware keys still loads.
            pw = keyring.get_password(
                "SimpleSFTPClient",
                f"{(host or '').strip()}|{(username or '').strip()}")
        return pw or ""
    except Exception:
        return ""


def get_remembered(api, host, username, port=22):
    return {"password": api._remembered_password(host, username, port)}
