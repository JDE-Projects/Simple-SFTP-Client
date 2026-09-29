import getpass
import os
import subprocess
import sys

from app.debug import debug

_PROTECT_WARNING = ("Saved, but the private key's file permissions couldn't be locked down on "
                     "this location. Store it somewhere only you can read, such as your user "
                     "profile's .ssh folder.")


def _protect_private_key(path) -> str | None:
    """Lock a private key file down to the current user only.

    On Windows this disables inherited permissions and grants the current
    user full control via icacls, so other accounts on the machine can't
    read the file. Returns None on success, or a plain-language warning
    string if the lockdown couldn't be applied (never raises)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if sys.platform != "win32":
        return _PROTECT_WARNING
    try:
        user = os.environ.get("USERNAME") or getpass.getuser()
        if not user:
            return _PROTECT_WARNING
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for cmd in (["icacls", path, "/inheritance:r"],
                    ["icacls", path, "/grant:r", f"{user}:(F)"]):
            res = subprocess.run(cmd, capture_output=True, shell=False,
                                  startupinfo=startupinfo, creationflags=creationflags)
            if res.returncode != 0:
                debug.log("KEYGEN protect failed", res.stderr.decode(errors="replace") if res.stderr else str(res.returncode))
                return _PROTECT_WARNING
        return None
    except Exception as e:
        debug.log("KEYGEN protect failed", str(e))
        return _PROTECT_WARNING

