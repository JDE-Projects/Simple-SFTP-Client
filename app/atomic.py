import json
import os
import tempfile
import time

from app.debug import debug


def _atomic_write_json(path, obj, **dump_kwargs) -> bool:
    """Write obj as JSON to path atomically: write to a temp file in the same
    folder, fsync it, then os.replace over the real path so a reader never
    sees a half-written file and a crash mid-write never corrupts it. False
    result must surface a visible error, not silence."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=".tmp_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        try:
            debug.log(f"Could not write {os.path.basename(path)}: {e}")
        except Exception:
            pass
        return False


def _preserve_corrupt(path, error) -> None:
    """A file failed to parse: move it aside with a timestamped suffix instead
    of silently discarding it, and log what happened. Never raises."""
    try:
        if os.path.exists(path):
            aside = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(path, aside)
            debug.log(f"{os.path.basename(path)} unreadable, kept as {aside}: {error}")
        else:
            debug.log(f"{os.path.basename(path)} unreadable: {error}")
    except Exception:
        pass

