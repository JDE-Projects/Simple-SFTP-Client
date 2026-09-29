"""Service functions for the browsing area."""

import os
import stat
import time
import shutil
import posixpath
import webview
from app.errors import friendly_error
from app.formatting import human_size
from app.paths import is_temp_part


def ping(api):
    # latency for the health indicator
    if not api.connected:
        return {"ok": False}
    try:
        t0 = time.time()
        api.sftp.stat(".")
        return {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except Exception:
        api.connected = False
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
            if is_temp_part(name):
                continue
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
        # Remembered so a later connect() knows which real folder to
        # sweep for leftover scratch files (see _sweep_scratch_files).
        # Never set from the DRIVES branch above, which isn't one.
        api._local_cwd = path
        return {"ok": True, "cwd": path, "parent": parent, "entries": entries}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def list_remote(api, path):
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    try:
        path = api.sftp.normalize(path or ".")
        entries = []
        for a in api.sftp.listdir_attr(path):
            if is_temp_part(a.filename):
                continue
            entries.append({"name": a.filename, "is_dir": stat.S_ISDIR(a.st_mode),
                            "size": a.st_size, "mtime": int(a.st_mtime or 0)})
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        api._vlog(f"ls {path} → {len(entries)} item(s)")
        return {"ok": True, "cwd": path, "parent": parent, "entries": entries}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def make_dir(api, side, path, name):
    try:
        if side == "local":
            os.makedirs(os.path.join(path, name), exist_ok=False)
        else:
            target = posixpath.join(path, name)
            api.sftp.mkdir(target)
            api._vlog(f"mkdir {target}")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def rename(api, side, path, old, new):
    try:
        if side == "local":
            os.rename(os.path.join(path, old), os.path.join(path, new))
        else:
            api.sftp.rename(posixpath.join(path, old), posixpath.join(path, new))
            api._vlog(f"rename {posixpath.join(path, old)} → {new}")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def delete(api, side, path, items):
    errs = []
    for it in items:
        try:
            if side == "local":
                full = os.path.join(path, it["name"])
                shutil.rmtree(full) if it["is_dir"] else os.remove(full)
            else:
                full = posixpath.join(path, it["name"])
                api._rremove(full) if it["is_dir"] else api.sftp.remove(full)
                if not it["is_dir"]:
                    api._vlog(f"remove {full}")
        except Exception as e:
            errs.append(f"{it['name']}: {e}")
    return {"ok": True, "errors": errs}


def _rremove(api, path):
    # Deleting a folder removes everything in it, including any leftover
    # scratch file from an interrupted transfer: skipping those would
    # leave the directory non-empty and make the final rmdir fail.
    for a in api.sftp.listdir_attr(path):
        child = posixpath.join(path, a.filename)
        api._rremove(child) if stat.S_ISDIR(a.st_mode) else api.sftp.remove(child)
    api.sftp.rmdir(path)
    api._vlog(f"rmdir {path}")


def open_local(api, path, name):
    try:
        os.startfile(os.path.join(path, name))  # noqa (Windows)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}


def calc_remote_size(api, remote_dir, name):
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    target = posixpath.join(remote_dir, name)
    total = {"bytes": 0, "files": 0}
    api._cancel.clear()

    def walk(p):
        if api._cancel.is_set():
            return
        try:
            attrs = api.sftp.listdir_attr(p)
        except Exception:
            return
        for a in attrs:
            if api._cancel.is_set():
                return
            if is_temp_part(a.filename):
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
        walk(target)
        return {"ok": True, "bytes": total["bytes"], "human": human_size(total["bytes"]),
                "files": total["files"]}
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
