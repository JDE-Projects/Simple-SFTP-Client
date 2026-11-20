"""Service functions for the browsing area."""

import contextlib
import os
import posixpath
import shutil
import stat
import time
import paramiko
import webview
from paramiko.sftp import CMD_CLOSE, CMD_HANDLE, CMD_NAME, CMD_OPENDIR, CMD_READDIR
from paramiko.sftp_attr import SFTPAttributes
from app.constants import BROWSE_ENTRY_LIMIT, BROWSE_EXAMINE_LIMIT
from app.errors import friendly_error
from app.formatting import human_size
from app.paths import remote_leaf_problem, safe_local_child
from app.scratch_journal import is_active_local
from app.services import connections
from app.services.scanning import _read_error


class UnsafeRemoteListing(ValueError):
    """A server listed a name that cannot safely be joined to its parent."""

    def __init__(self, name):
        self.name = name
        super().__init__(f"unsafe remote name {name!r}")


def _local_name_error(name):
    return (f"{name!r} can't be used here. Use one file or folder name, without \\ / : * ? \" < > | "
            "or a trailing dot or space, and not a reserved Windows name such as CON.")


def _remote_name_error(name):
    return f"{name!r} can't be used here. Use one file or folder name, without /, and not . or .."


def _validate_remote_leaf(name):
    if remote_leaf_problem(name):
        raise ValueError(f"unsafe remote name {name!r}")


def ping(api):
    # latency for the health indicator
    if not api._connected:
        return {"ok": False}
    try:
        t0 = time.time()
        api._sftp.stat(".")
        return {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except Exception:
        api._connected = False
        return {"ok": False}


def _iter_remote_dir(api, path):
    """Yield remote attributes with one request in flight and always close it."""
    sftp = api._sftp
    handle = None
    try:
        # Paramiko 5.0.0's listdir_iter prefetches 50 READDIR requests. A
        # bounded browser must stop early, so use its private synchronous
        # listdir_attr protocol loop instead and close the handle immediately.
        adjusted_path = sftp._adjust_cwd(path)
        response_type, message = sftp._request(CMD_OPENDIR, adjusted_path)
        if response_type != CMD_HANDLE:
            raise paramiko.SFTPError("Expected handle")
        handle = message.get_binary()
        while True:
            try:
                response_type, message = sftp._request(CMD_READDIR, handle)
            except EOFError:
                return
            if response_type != CMD_NAME:
                raise paramiko.SFTPError("Expected name response")
            for _ in range(message.get_int()):
                filename = message.get_text()
                longname = message.get_text()
                if filename not in (".", ".."):
                    yield SFTPAttributes._from_msg(message, filename, longname)
                else:
                    SFTPAttributes._from_msg(message, filename, longname)
    finally:
        if handle is not None:
            try:
                sftp._request(CMD_CLOSE, handle)
            except Exception as close_error:
                # Logged, not raised: the entries already read are valid, and
                # raising here would hide an error that ended the read.
                api._vlog(f"could not close remote directory {path}: {close_error}", "warn",
                          event="remote list close failed")


def list_local(api, path):
    if path in ("", "DRIVES") and os.name == "nt":
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        return {"ok": True, "cwd": "DRIVES", "parent": None,
                "entries": [{"name": d, "is_dir": True, "size": 0, "mtime": 0} for d in drives],
                "truncated": False, "limit": BROWSE_ENTRY_LIMIT}
    path = path or os.path.expanduser("~")
    try:
        entries = []
        truncated = False
        examined = 0
        with os.scandir(path) as scan:
            for item in scan:
                examined += 1
                full = item.path
                if is_active_local(full):
                    if examined >= BROWSE_EXAMINE_LIMIT:
                        truncated = True
                        break
                    continue
                try:
                    st = os.stat(full)
                except Exception:
                    if examined >= BROWSE_EXAMINE_LIMIT:
                        truncated = True
                        break
                    continue
                if len(entries) >= BROWSE_ENTRY_LIMIT:
                    truncated = True
                    break
                entries.append({"name": item.name, "is_dir": os.path.isdir(full),
                                "size": st.st_size, "mtime": int(st.st_mtime)})
                if examined >= BROWSE_EXAMINE_LIMIT:
                    truncated = True
                    break
        parent = os.path.dirname(path.rstrip("\\/")) or ("DRIVES" if os.name == "nt" else "/")
        if os.name == "nt" and len(path.rstrip("\\/")) <= 2:
            parent = "DRIVES"
        return {"ok": True, "cwd": path, "parent": parent, "entries": entries,
                "truncated": truncated, "limit": BROWSE_ENTRY_LIMIT}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def list_remote(api, path):
    if not api._connected:
        return {"ok": False, "error": "Not connected."}
    try:
        path = api._sftp.normalize(path or ".")
        entries = []
        dropped_count = 0
        dropped_examples = []
        examined = 0
        truncated = False
        with contextlib.closing(_iter_remote_dir(api, path)) as remote_entries:
            for a in remote_entries:
                examined += 1
                if remote_leaf_problem(a.filename):
                    dropped_count += 1
                    if len(dropped_examples) < 3:
                        dropped_examples.append(a.filename)
                elif api._is_active_remote_scratch(posixpath.join(path, a.filename)):
                    pass
                elif len(entries) >= BROWSE_ENTRY_LIMIT:
                    truncated = True
                    break
                else:
                    entries.append({"name": a.filename, "is_dir": stat.S_ISDIR(a.st_mode),
                                    "size": a.st_size, "mtime": int(a.st_mtime or 0)})
                if examined >= BROWSE_EXAMINE_LIMIT:
                    truncated = True
                    break
        if dropped_count:
            examples = ", ".join(repr(name) for name in dropped_examples)
            api._vlog(f"skipped {dropped_count} unsafe remote name(s): {examples}", "warn",
                      event="unsafe remote list", items=dropped_count)
        if truncated:
            api._vlog(f"remote list truncated for {path}: {len(entries)} item(s) loaded", "warn",
                      event="remote list truncated", items=len(entries))
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        api._vlog(f"ls {path} → {len(entries)} item(s)", event="remote list", items=len(entries))
        return {"ok": True, "cwd": path, "parent": parent, "entries": entries,
                "truncated": truncated, "limit": BROWSE_ENTRY_LIMIT}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def make_dir(api, side, path, name):
    try:
        if side == "local":
            os.makedirs(safe_local_child(path, name, path), exist_ok=False)
        else:
            _validate_remote_leaf(name)
            target = posixpath.join(path, name)
            api._sftp.mkdir(target)
            api._vlog(f"mkdir {target}", event="remote mkdir")
        return {"ok": True}
    except ValueError:
        return {"ok": False, "error": _local_name_error(name) if side == "local" else _remote_name_error(name)}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def rename(api, side, path, old, new):
    try:
        if side == "local":
            os.rename(safe_local_child(path, old, path), safe_local_child(path, new, path))
        else:
            _validate_remote_leaf(old)
            _validate_remote_leaf(new)
            api._sftp.rename(posixpath.join(path, old), posixpath.join(path, new))
            api._vlog(f"rename {posixpath.join(path, old)} → {new}", event="remote rename")
        return {"ok": True}
    except ValueError:
        bad_name = old
        if side == "local":
            try:
                safe_local_child(path, old, path)
            except ValueError:
                pass
            else:
                bad_name = new
            return {"ok": False, "error": _local_name_error(bad_name)}
        if not remote_leaf_problem(old):
            bad_name = new
        return {"ok": False, "error": _remote_name_error(bad_name)}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def delete(api, side, path, items):
    errs = []
    for it in items:
        try:
            if side == "local":
                full = safe_local_child(path, it["name"], path)
                shutil.rmtree(full) if it["is_dir"] else os.remove(full)
            else:
                _validate_remote_leaf(it["name"])
                full = posixpath.join(path, it["name"])
                api._rremove(full) if it["is_dir"] else api._sftp.remove(full)
                if not it["is_dir"]:
                    api._vlog(f"remove {full}", event="remote remove")
        except UnsafeRemoteListing as e:
            errs.append(f"{it['name']}: stopped, the server listed an unsafe name inside it "
                        f"({e.name!r}); some items in it may already be deleted")
        except ValueError:
            message = _local_name_error(it["name"]) if side == "local" else _remote_name_error(it["name"])
            errs.append(f"{it['name']}: {message}")
        except Exception as e:
            errs.append(f"{it['name']}: {e}")
    return {"ok": True, "errors": errs}


def _rremove(api, path):
    # Deleting a folder removes everything in it, including any leftover
    # scratch file from an interrupted transfer: skipping those would
    # leave the directory non-empty and make the final rmdir fail.
    for a in api._sftp.listdir_attr(path):
        if remote_leaf_problem(a.filename):
            raise UnsafeRemoteListing(a.filename)
        child = posixpath.join(path, a.filename)
        api._rremove(child) if stat.S_ISDIR(a.st_mode) else api._sftp.remove(child)
    api._sftp.rmdir(path)
    api._vlog(f"rmdir {path}", event="remote rmdir")


def open_local(api, path, name):
    try:
        os.startfile(safe_local_child(path, name, path))
        return {"ok": True}
    except ValueError:
        return {"ok": False, "error": _local_name_error(name)}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def calc_remote_size(api, remote_dir, name):
    if not api._connected:
        return {"ok": False, "error": "Not connected."}
    total = {"bytes": 0, "files": 0}
    problems = []
    problem_count = 0
    api._cancel.clear()

    def add_problem(path, reason):
        nonlocal problem_count
        problem_count += 1
        if len(problems) < 10_000:
            problems.append({"path": path, "reason": reason})

    def cancelled():
        if api._cancel.is_set():
            return {"ok": False, "cancelled": True, "error": "Size calculation cancelled."}

    def connection_lost():
        return {"ok": False, "error": "Connection lost while calculating size.",
                "connection_lost": True}

    def walk(p, top=False):
        stopped = cancelled()
        if stopped:
            return stopped
        try:
            attrs = api._sftp.listdir_attr(p)
        except Exception as e:
            dead = isinstance(e, (paramiko.SSHException, EOFError))
            if connections._connection_dead(api, api._sftp):
                dead = True
            if dead:
                return connection_lost()
            if top:
                return {"ok": False, "error": f"Could not list {p}: {_read_error(e, remote=True)}"}
            add_problem(p, _read_error(e, remote=True))
            return None
        stopped = cancelled()
        if stopped:
            return stopped
        for a in attrs:
            stopped = cancelled()
            if stopped:
                return stopped
            if remote_leaf_problem(a.filename):
                api._vlog(f"skipped unsafe remote name while calculating size: {a.filename!r}", "warn",
                          event="unsafe remote size")
                add_problem(f"{p.rstrip('/')}/{a.filename!r}", "Unsafe server name.")
                continue
            if api._is_active_remote_scratch(posixpath.join(p, a.filename)):
                continue
            if a.st_mode is None:
                add_problem(posixpath.join(p, a.filename),
                            "Cannot tell whether this entry is a file or folder.")
            elif stat.S_ISDIR(a.st_mode):
                stopped = walk(posixpath.join(p, a.filename))
                if stopped:
                    return stopped
            elif a.st_size is None:
                add_problem(posixpath.join(p, a.filename), "File size is unavailable.")
            else:
                total["bytes"] += a.st_size
                total["files"] += 1
                if total["files"] % 50 == 0:
                    api._emit("size_progress", {"files": total["files"],
                                                 "bytes": human_size(total["bytes"])})
        return None
    try:
        _validate_remote_leaf(name)
        target = posixpath.join(remote_dir, name)
        stopped = walk(target, top=True)
        if stopped:
            return stopped
        stopped = cancelled()
        if stopped:
            return stopped
        if problem_count:
            api._vlog(f"remote size incomplete for {target}: {human_size(total['bytes'])} in "
                      f"{total['files']} file(s), {problem_count} problem(s)", "warn",
                      event="remote size incomplete", items=problem_count)
        return {"ok": True, "bytes": total["bytes"], "human": human_size(total["bytes"]),
                "files": total["files"], "complete": problem_count == 0,
                "problem_count": problem_count, "problems": problems,
                "more": problem_count - len(problems)}
    except ValueError:
        return {"ok": False, "error": _remote_name_error(name)}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def browse_folder(api):
    if not api._window:
        return ""
    try:
        dlg = webview.FileDialog.FOLDER
    except AttributeError:  # older pywebview
        dlg = webview.FOLDER_DIALOG
    res = api._window.create_file_dialog(dlg)
    if not res:
        return ""
    return res if isinstance(res, str) else res[0]
