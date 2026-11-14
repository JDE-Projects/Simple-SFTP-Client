"""The event gateway is the only application route into the debug file."""

import builtins

import keyring

from app import paths, prefs
from app.api import Api
from app import debug as debug_module
from app.debug_log import DebugLog
from app.services import connections, scanning, sessions, updates
from simple_sftp_client import APP_VERSION


def test_log_event_validates_values_and_never_echoes_untrusted_text(tmp_path, monkeypatch):
    dbg = DebugLog(str(tmp_path), "Test App")
    assert dbg.set_enabled(True)
    monkeypatch.setattr(debug_module, "debug", dbg)

    debug_module.log_event("connect", auth="key")
    debug_module.log_event("not-MARKERWORD", value="MARKERWORD")
    debug_module.log_event("connect", auth="MARKERWORD")
    debug_module.log_event("connect", auth=object())

    text = open(dbg._path, encoding="utf-8").read()
    assert "connect\nauth=key" in text
    assert "unknown event" in text
    assert "auth=rejected" in text
    assert "MARKERWORD" not in text


def test_log_event_swallows_a_raising_input(tmp_path, monkeypatch):
    dbg = DebugLog(str(tmp_path), "Test App")
    assert dbg.set_enabled(True)
    monkeypatch.setattr(debug_module, "debug", dbg)

    class RaisingValue:
        def __eq__(self, other):
            raise RuntimeError("MARKERWORD")

    debug_module.log_event("connect", auth=RaisingValue())
    assert "auth=rejected" in open(dbg._path, encoding="utf-8").read()


def test_debug_log_warnings_keep_full_details_out_of_log_files(tmp_path, monkeypatch):
    marker = "MARKERWORD"

    def log_text():
        return "".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())

    folder_warnings = []
    folder_log = DebugLog(str(tmp_path), "Test App", max_bytes=1024,
                          on_warning=folder_warnings.append)
    assert folder_log.set_enabled(True)
    folder_log._current_size = 1023
    monkeypatch.setattr(
        "app.debug_log.os.scandir",
        lambda path: (_ for _ in ()).throw(PermissionError(13, marker, rf"C:\\{marker}")),
    )
    folder_log._log("event", "x" * 10)
    assert marker in folder_warnings[0]
    monkeypatch.undo()
    assert "folder check failed" in log_text()
    assert marker not in log_text()

    write_warnings = []
    write_log = DebugLog(str(tmp_path), "Test App", on_warning=write_warnings.append)
    assert write_log.set_enabled(True)
    real_open = builtins.open

    def fail_append(path, mode="r", *args, **kwargs):
        if mode == "a":
            raise OSError(28, marker)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_append)
    write_log._log("event")
    assert marker in write_warnings[0]
    assert marker not in log_text()
    monkeypatch.undo()

    rollover_warnings = []
    rollover_log = DebugLog(str(tmp_path), "Test App", max_bytes=1024,
                            on_warning=rollover_warnings.append)
    real_new_file = rollover_log._write_new_file
    calls = 0

    def fail_second_file(path):
        nonlocal calls
        calls += 1
        return real_new_file(path) if calls == 1 else None

    rollover_log._write_new_file = fail_second_file
    assert rollover_log.set_enabled(True)
    rollover_log._current_size = 1023
    rollover_log._log("event", "x" * 10)
    assert marker not in rollover_warnings[0]
    assert "could not start a new log file" in rollover_warnings[0]
    assert marker not in log_text()


def test_real_service_failures_log_fixed_events_without_typed_markers(tmp_path, monkeypatch):
    """Drive representative service error paths through the event gateway."""
    marker = "MARKERWORD"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    dbg = DebugLog(str(log_dir), "Test App")
    monkeypatch.setattr(debug_module, "debug", dbg)
    assert dbg.set_enabled(True)
    api = Api(APP_VERSION)

    def fail(*args, **kwargs):
        raise OSError(13, marker, rf"C:\\{marker}")

    monkeypatch.setattr(Api, "_open", fail)
    assert not api.connect({"host": marker, "port": "22", "username": marker,
                            "password": marker, "key_path": rf"C:\\{marker}",
                            "passphrase": marker})["ok"]
    monkeypatch.setattr(connections.socket, "create_connection", fail)
    assert not api.test_connection({"host": marker, "port": "22"})["ok"]

    class FailingSftp:
        def normalize(self, path):
            return path

        listdir_attr = mkdir = rename = remove = fail

    api._connected = True
    api._sftp = FailingSftp()
    api.list_remote(marker)
    api.make_dir("remote", "/", marker)
    api.rename("remote", "/", marker, marker + "-new")
    api.delete("remote", "/", [{"name": marker, "is_dir": False}])
    api._on_external_drop({"dataTransfer": {"files": [{"pywebviewFullPath": marker}]}})

    real_scandir = scanning.os.scandir
    monkeypatch.setattr(scanning.os, "scandir", fail)
    list(api._iter_local(str(tmp_path / marker), "/", True))
    monkeypatch.setattr(scanning.os, "scandir", real_scandir)
    real_urlopen = updates.urlopen
    monkeypatch.setattr(updates, "urlopen", fail)
    assert api.check_update()["offline"]
    monkeypatch.setattr(updates, "urlopen", real_urlopen)

    monkeypatch.setattr(paths, "SESSIONS_FILE", str(tmp_path / marker / "servers.json"))
    monkeypatch.setattr(sessions, "open", fail, raising=False)
    monkeypatch.setattr(prefs, "open", fail, raising=False)
    api._load_sessions()
    assert prefs.load_prefs() == {}
    monkeypatch.setattr(sessions, "open", builtins.open)
    monkeypatch.setattr(prefs, "open", builtins.open)
    real_set_password = keyring.set_password
    monkeypatch.setattr(keyring, "set_password", fail)
    api._cred_pass = marker
    api._cred_identity = (marker, 22, marker, "password")
    api.save_session({"name": marker, "host": marker, "port": "22", "username": marker,
                      "auth": "password", "key_path": "", "start_path": "", "remember": True})
    monkeypatch.setattr(keyring, "set_password", real_set_password)

    class BadClose:
        close = fail

    api._sftp = BadClose()
    api._client = BadClose()
    api._shutdown()
    text = "".join(path.read_text(encoding="utf-8") for path in log_dir.iterdir())
    for event in ("connect", "connect failed", "test connection", "error", "external drop",
                  "list failed", "update check", "session unreadable", "prefs unreadable",
                  "keyring failed", "shutdown close errors"):
        assert f"] {event}\n" in text
    assert marker not in text
    dbg.set_enabled(False)
