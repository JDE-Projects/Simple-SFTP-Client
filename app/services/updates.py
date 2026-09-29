"""Service functions for the updates area."""

import json
import webbrowser
from urllib.request import Request, urlopen
from app.constants import GITHUB_REPO
from app.debug import debug
from app.errors import _update_error_reason, friendly_error


def check_update(api):
    """Compare the latest published release to the running version (the one
    the launcher hands to Api, kept as api._app_version). Quiet in the UI on
    failure (see _update_error_reason), but always logged when debug is on."""
    result = {"current": api._app_version, "version": None, "update": False, "offline": False}
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = Request(url, headers={"User-Agent": "Simple-SFTP-Client",
                                    "Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        latest = (data.get("tag_name") or "").lstrip("v")
        result["version"] = latest
        if latest and api._is_newer(latest, api._app_version):
            result["update"] = True
        debug.log(f"check_update: found v{latest}, current v{api._app_version}")
    except Exception as e:
        result["offline"] = True
        result["reason"] = _update_error_reason(e)
        debug.log(f"check_update failed: {type(e).__name__}: {e}")
    return result


def _is_newer(api, latest, current):
    def parts(v):
        out = []
        for x in v.split("."):
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out + [0] * (3 - len(out))
    try:
        return parts(latest) > parts(current)
    except Exception:
        return False


def open_url(api, url):
    try:
        webbrowser.open(url)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
