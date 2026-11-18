"""Durable ownership records for local download scratch files.

Entries from an earlier run are retained for the recovery phase. Entries with
an invalid shape are also retained in the on-disk journal unchanged, but are
marked invalid in memory and are never considered owned or acted on here.
"""

import copy
import json
import msvcrt
import os
import re
import stat
import threading

from app import paths
from app.atomic import _atomic_write_json, _preserve_corrupt
from app.debug import log_event

_SCRATCH_NAME_RE = re.compile(r"^\.(?P<final>.+)\.(?P<token>[0-9a-f]{8})\.sxtpart$")


def _without_extended_prefix(path):
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def path_key(path):
    """Return the one canonical comparison key used by this journal."""
    return os.path.normcase(os.path.abspath(_without_extended_prefix(os.fspath(path))))


def _absolute_path(path):
    return os.path.abspath(_without_extended_prefix(os.fspath(path)))


class ScratchJournal:
    """Own one process's scratch journal while its separate lock is held."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lock_file = None
        self._entries = []
        self._owned = {}

    @staticmethod
    def path_key(path):
        return path_key(path)

    def open(self):
        """Lock and load the journal, returning False when it cannot be used."""
        with self._lock:
            if self._lock_file is not None:
                return True
            lock_file = None
            try:
                lock_file = open(paths.SCRATCH_LOCK_FILE, "a+b")  # noqa: SIM115
                lock_file.seek(0)
                if lock_file.read(1) == b"":
                    lock_file.seek(0)
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if lock_file is not None:
                    lock_file.close()
                log_event("scratch journal unavailable", error=error)
                return False
            self._lock_file = lock_file
            try:
                with open(paths.SCRATCH_JOURNAL_FILE, "rb") as journal_file:
                    raw = journal_file.read()
            except FileNotFoundError:
                self._entries = []
                self._owned = {}
                return True
            except OSError as error:
                log_event("scratch journal unavailable", error=error)
                self._close_locked()
                return False
            try:
                entries = json.loads(raw.decode("utf-8"))
                if not isinstance(entries, list):
                    raise TypeError("scratch journal must be a list")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
                _preserve_corrupt(paths.SCRATCH_JOURNAL_FILE, error)
                if os.path.exists(paths.SCRATCH_JOURNAL_FILE):
                    log_event("scratch journal unavailable", error=error)
                    self._close_locked()
                    return False
                self._entries = []
                self._owned = {}
                return True
            self._entries = entries
            self._owned = {}
            for entry in entries:
                if self._valid_entry(entry):
                    self._owned[path_key(entry["scratch"])] = entry
            return True

    def _valid_entry(self, entry):
        if not isinstance(entry, dict):
            return False
        if set(entry) != {"scratch", "final", "identity"}:
            return False
        if not isinstance(entry["scratch"], str) or not isinstance(entry["final"], str):
            return False
        if entry["identity"] is not None and (
                not isinstance(entry["identity"], list)
                or len(entry["identity"]) != 2
                or not all(isinstance(value, int) for value in entry["identity"])):
            return False
        try:
            self._checked_paths(entry["scratch"], entry["final"])
        except ValueError:
            return False
        return True

    @staticmethod
    def _checked_paths(scratch, final):
        scratch = _absolute_path(scratch)
        final = _absolute_path(final)
        if not os.path.isabs(scratch) or not os.path.isabs(final):
            raise ValueError("scratch and final paths must be absolute")
        if path_key(os.path.dirname(scratch)) != path_key(os.path.dirname(final)):
            raise ValueError("scratch and final paths must be siblings")
        match = _SCRATCH_NAME_RE.fullmatch(os.path.basename(scratch))
        if match is None or match.group("final") != os.path.basename(final):
            raise ValueError("scratch path does not match the final filename")
        return scratch, final

    def _write(self, durable):
        attempts = 3 if durable else 1
        for _ in range(attempts):
            if _atomic_write_json(paths.SCRATCH_JOURNAL_FILE, self._entries,
                                  durable=durable, indent=2):
                return True
        return False

    def register(self, scratch, final):
        scratch, final = self._checked_paths(scratch, final)
        entry = {"scratch": scratch, "final": final, "identity": None}
        with self._lock:
            self._entries.append(entry)
            self._owned[path_key(scratch)] = entry
            if self._write(durable=True):
                return
            self._entries.pop()
            self._owned.pop(path_key(scratch), None)
            raise OSError("Could not record the download scratch file.")

    def set_identity(self, scratch, identity):
        with self._lock:
            entry = self._owned.get(path_key(scratch))
            if entry is None:
                return
            entry["identity"] = [identity[0], identity[1]]
            if not self._write(durable=False):
                log_event("scratch journal write failed",
                          error=OSError("could not update scratch journal"))

    def retire(self, scratch):
        with self._lock:
            key = path_key(scratch)
            entry = self._owned.pop(key, None)
            if entry is None:
                return
            self._entries.remove(entry)
            if not self._write(durable=False):
                log_event("scratch journal write failed",
                          error=OSError("could not update scratch journal"))

    def owns(self, path):
        with self._lock:
            return path_key(path) in self._owned

    def recover(self):
        """Remove only leftovers whose recorded file identity still proves ownership."""
        deleted = 0
        left_in_place = []
        unreachable = []
        with self._lock:
            for entry in list(self._entries):
                if not self._valid_entry(entry):
                    continue
                scratch = entry["scratch"]
                try:
                    current_stat = os.lstat(scratch)
                except FileNotFoundError:
                    self._entries.remove(entry)
                    self._owned.pop(path_key(scratch), None)
                    continue
                except OSError as error:
                    unreachable.append(scratch)
                    log_event("scratch recovery unreachable", error=error)
                    continue
                identity = entry["identity"]
                current_identity = [current_stat.st_dev, current_stat.st_ino]
                if (identity is None or not stat.S_ISREG(current_stat.st_mode)
                        or current_identity != identity):
                    left_in_place.append(scratch)
                    self._entries.remove(entry)
                    self._owned.pop(path_key(scratch), None)
                    log_event("scratch recovery left in place")
                    continue
                try:
                    os.remove(scratch)
                except FileNotFoundError:
                    self._entries.remove(entry)
                    self._owned.pop(path_key(scratch), None)
                except OSError as error:
                    log_event("scratch recovery delete failed", error=error)
                else:
                    deleted += 1
                    self._entries.remove(entry)
                    self._owned.pop(path_key(scratch), None)
            if not self._write(durable=False):
                log_event("scratch journal write failed",
                          error=OSError("could not update scratch journal"))
        return {"deleted": deleted, "left_in_place": left_in_place,
                "unreachable": unreachable}

    def entries(self):
        with self._lock:
            return copy.deepcopy(self._entries)

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._lock_file is None:
            return
        try:
            self._lock_file.seek(0)
            msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._lock_file.close()
            self._lock_file = None


current = None


def is_active_local(path):
    """True only when the active journal has recorded this local scratch file."""
    return current is not None and current.owns(path)
