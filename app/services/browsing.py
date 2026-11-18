"""Service functions for the browsing area."""

import os
import posixpath
import shutil
import stat
import time
import webview
from app.errors import friendly_error
from app.formatting import human_size
from app.paths import remote_leaf_problem, safe_local_child
from app.scratch_journal import is_active_local


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


def list_local(api, path):
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
            if is_active_local(full):
                continue
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


def list_remote(api, path):
    if not api._connected:
        return {"ok": False, "error": "Not connected."}
    try:
        path = api._sftp.normalize(path or ".")
        entries = []
        dropped = []
        for a in api._sftp.listdir_attr(path):
            if remote_leaf_problem(a.filename):
                dropped.append(a.filename)
                continue
            if api._is_active_remote_scratch(posixpath.join(path, a.filename)):
                continue
            entries.append({"name": a.filename, "is_dir": stat.S_ISDIR(a.st_mode),
                            "size": a.st_size, "mtime": int(a.st_mtime or 0)})
        if dropped:
            examples = ", ".join(repr(name) for name in dropped[:3])
            api._vlog(f"skipped {len(dropped)} unsafe remote name(s): {examples}", "warn",
                      event="unsafe remote list", items=len(dropped))
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        api._vlog(f"ls {path} → {len(entries)} item(s)", event="remote list", items=len(entries))
        return {"ok": True, "cwd": path, "parent": parent, "entries": entries}
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
    api._cancel.clear()

    def walk(p):
        if api._cancel.is_set():
            return
        try:
            attrs = api._sftp.listdir_attr(p)
        except Exception:
            return
        for a in attrs:
            if api._cancel.is_set():
                return
            if remote_leaf_problem(a.filename):
                api._vlog(f"skipped unsafe remote name while calculating size: {a.filename!r}", "warn",
                          event="unsafe remote size")
                continue
            if api._is_active_remote_scratch(posixpath.join(p, a.filename)):
                continue
            if stat.S_ISDIR(a.st_mode):
                walk(posixpath.join(p, a.filename))
            else:
                total["bytes"] += a.st_size or 0
                total["files"] += 1
                if total["files"] % 50 == 0:
                    api._emit("size_progress", {"files": total["files"],
                                                 "bytes": human_size(total["bytes"])})
    try:
        _validate_remote_leaf(name)
        target = posixpath.join(remote_dir, name)
        walk(target)
        return {"ok": True, "bytes": total["bytes"], "human": human_size(total["bytes"]),
                "files": total["files"]}
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
