"""Service functions for the updates area."""

import json
import urllib.parse
import webbrowser
from urllib.request import Request, urlopen
from app.constants import GITHUB_REPO
from app.debug import log_event
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
        log_event("update check", outcome="newer" if result["update"] else "current")
    except Exception as e:
        result["offline"] = True
        result["reason"] = _update_error_reason(e)
        log_event("update check", outcome="failed", error=e)
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


# The only site the page may open in the system browser (the header byline).
ALLOWED_URL_HOST = "jde-projects.com"


def _is_allowed_url(url):
    """True only for a plain https address on the JDE-Projects website: no
    other host, scheme, port, login part, or whitespace/control characters."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    # Comparing the whole netloc rules out ports and user@host tricks.
    return parts.scheme == "https" and parts.netloc == ALLOWED_URL_HOST


def open_url(api, url):
    """Open a link in the system browser, never by navigating the app window.
    Only the JDE-Projects website is allowed (see _is_allowed_url), because any
    script that gets into the page can call this bridge."""
    if not _is_allowed_url(url):
        log_event("open url refused")
        return {"ok": False, "error": "That link isn't allowed."}
    try:
        webbrowser.open(url)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
