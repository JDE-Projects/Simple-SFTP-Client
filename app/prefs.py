import json
import os

from app.atomic import _atomic_write_json, _preserve_corrupt
from app.debug import debug
from app.paths import exe_dir

# ----------------------------------------------------------------------------
# Local prefs store. One JSON file next to the app holds EVERY persisted
# setting: theme, window geometry, and anything added later. Always read-
# merge-write through load_prefs / save_prefs. Never overwrite the file with
# a single key, or the next setting you add silently wipes the others.
# ----------------------------------------------------------------------------


def _pref_path() -> str:
    return os.path.join(exe_dir(), "simple_sftp_client.pref")


def load_prefs() -> dict:
    """Full prefs dict. Missing file: first run. Damaged file: kept aside,
    logged. A file that cannot be opened (locked, no access) is not damaged,
    so it is left in place and logged."""
    path = _pref_path()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        debug.log(f"Settings file not read, left in place: "
                  f"{e.strerror or type(e).__name__}")
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
        raise ValueError("prefs root is not an object")
    except Exception as e:
        _preserve_corrupt(path, e)
        return {}


def save_prefs(prefs: dict) -> bool:
    """Write atomically. False result must surface a visible error, not silence."""
    return _atomic_write_json(_pref_path(), prefs)

