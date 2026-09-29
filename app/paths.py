import ntpath
import os
import posixpath
import re
import sys


# ───────────── paths ─────────────
def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

# Match only the exact shape local_temp_path / remote_temp_path build:
# a leading dot, the original name, a dot, 8 lowercase hex characters from
# os.urandom(4).hex(), then the suffix. Matching a bare ".sxtpart" suffix
# would hide and later delete an ordinary user file that happened to end
# that way; the full convention is what marks a file as ours to remove.
# fullmatch (not match + "$") so a name ending in a literal newline before
# the suffix cannot slip through: "$" would anchor before that newline.
_TEMP_PART_RE = re.compile(r"\..+\.[0-9a-f]{8}" + re.escape(TEMP_PART_SUFFIX))


def is_temp_part(name: str) -> bool:
    """True for one of this app's own in-progress transfer scratch files, so
    every place that lists a folder can hide a file still being written
    (download or upload) instead of showing it, picking it up as a transfer
    target, or letting it skew a Compare/Sync. Recognizing our own reserved
    name shape is not a claim of ownership over any file matching it."""
    return bool(name) and _TEMP_PART_RE.fullmatch(name) is not None


def local_temp_path(final_path: str) -> str:
    """Build the local scratch path a download streams into before the
    atomic swap: a sibling of final_path in the same folder (so the final
    os.replace stays on one drive), named so is_temp_part recognizes it."""
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

KNOWN_HOSTS_FILE = os.path.join(exe_dir(), "known_hosts")

