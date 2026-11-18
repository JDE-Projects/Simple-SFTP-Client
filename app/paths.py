import ntpath
import os
import posixpath
import sys


# ───────────── paths ─────────────
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def windows_name_problem(name: str) -> str | None:
    """Return why one path component cannot be a distinct Windows name."""
    if ntpath.isreserved(name):
        return "reserved Windows filename"
    if ":" in name:
        return "contains ':'"
    return None


def windows_destination_key(path: str) -> str:
    """Return a conservative Windows destination comparison key for path.

    The folding comes from Python's Unicode tables, not Windows' own table.
    Against Windows' table over the Basic Multilingual Plane it misses no pair
    Windows treats as one file, and treats roughly 260 rare letters as
    matching when Windows would keep them apart, so it errs toward reporting
    a clash. Unicode normalization is deliberately not applied because NTFS
    keeps composed and decomposed forms as different files.
    """
    normalized = ntpath.normpath(ntpath.abspath(path))
    key = []
    for c in normalized:
        lower = c.lower()
        folded = lower if len(lower) == 1 else c
        upper = folded.upper()
        key.append(upper if len(upper) == 1 else folded)
    return "".join(key)


def safe_local_child(parent: str, name: str, root: str) -> str:
    """Validate a single server-supplied filename before it becomes part of a
    local path, and confirm the result stays under root (the local folder the
    user selected for this transfer). Rejects anything that looks like path
    traversal, or an absolute/UNC/drive path smuggled in as a "filename" by a
    hostile or broken server, by raising ValueError. Does not resolve
    symlinks (abspath + commonpath only); parent must already be under root.
    A symlink or junction the user created locally is followed: the server
    cannot create one, and a download into it is the user's own choice."""
    if not name or name in (".", ".."):
        raise ValueError(f"unsafe name {name!r}")
    if "/" in name or "\\" in name or os.sep in name or (os.altsep and os.altsep in name):
        raise ValueError(f"unsafe name {name!r}")
    if os.path.isabs(name) or os.path.splitdrive(name)[0] or ntpath.splitdrive(name)[0]:
        raise ValueError(f"unsafe name {name!r}")
    problem = windows_name_problem(name)
    if problem:
        raise ValueError(f"unsafe name {name!r}: {problem}")
    candidate = os.path.join(parent, name)
    root_abs = os.path.abspath(root)
    try:
        common = os.path.commonpath([root_abs, os.path.abspath(candidate)])
    except ValueError:
        raise ValueError(f"unsafe name {name!r}") from None
    if common != root_abs:
        raise ValueError(f"unsafe name {name!r}")
    return candidate


def local_link_target(path: str):
    """Return the real location of path when it is a symlink or junction,
    else None. Used to warn before a download is written through one."""
    try:
        if os.path.islink(path) or os.path.isjunction(path):
            return os.path.realpath(path)
    except OSError:
        pass
    return None


TEMP_PART_SUFFIX = ".sxtpart"

def local_temp_path(final_path: str) -> str:
    """Build the local scratch path a download streams into before the
    atomic swap: a sibling of final_path in the same folder (so the final
    os.replace stays on one drive)."""
    folder = os.path.dirname(final_path)
    name = os.path.basename(final_path)
    return os.path.join(folder, f".{name}.{os.urandom(4).hex()}{TEMP_PART_SUFFIX}")


def remote_temp_path(final_path: str) -> str:
    """Same idea as local_temp_path, but posix-style for the remote side."""
    folder = posixpath.dirname(final_path)
    name = posixpath.basename(final_path)
    temp_name = f".{name}.{os.urandom(4).hex()}{TEMP_PART_SUFFIX}"
    return posixpath.join(folder, temp_name) if folder else temp_name


SESSIONS_FILE = os.path.join(exe_dir(), "servers.json")

SCRATCH_JOURNAL_FILE = os.path.join(exe_dir(), "scratch-journal.json")

SCRATCH_LOCK_FILE = os.path.join(exe_dir(), "scratch-journal.lock")

KNOWN_HOSTS_FILE = os.path.join(exe_dir(), "known_hosts")

