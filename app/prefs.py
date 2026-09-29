import json
import os

from app.atomic import _atomic_write_json, _preserve_corrupt
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
    """Full prefs dict. Missing file: first run. Corrupt file: kept aside, logged."""
    path = _pref_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        raise ValueError("prefs root is not an object")
    except FileNotFoundError:
        return {}
    except Exception as e:
        _preserve_corrupt(path, e)
        return {}


def save_prefs(prefs: dict) -> bool:
    """Write atomically. False result must surface a visible error, not silence."""
    return _atomic_write_json(_pref_path(), prefs)

