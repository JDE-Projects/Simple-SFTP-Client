"""
Debug log warnings can fire from any thread (a worker's log() call, a launch
time prune()...), but the window's evaluate_js call must only ever happen from
the main/GUI flow: calling it from another thread deadlocks this app. So
debug.on_warning is wired to Api._on_debug_warning, which only buffers; the
page picks the messages up through drain_debug_warnings(), set_debug(), and
poll_queue(), never through _emit. This file covers that wiring, independent
of the real module-level `debug` singleton (which points at the real exe
folder), by exercising Api's buffer directly and a throwaway DebugLog pointed
at a tmp_path.
"""
from app.api import Api
from app.debug import _scrub, debug
from app.debug_log import DebugLog
from simple_sftp_client import APP_VERSION


def test_warning_reaches_api_buffer_and_drains_once():
    api = Api(APP_VERSION)
    api._on_debug_warning("Debug log: something went wrong.")

    first = api.drain_debug_warnings()
    assert first["warnings"] == ["Debug log: something went wrong."]

    second = api.drain_debug_warnings()
    assert second["warnings"] == []


def test_drain_reports_whether_logging_is_on(monkeypatch):
    # The page uses this to untick the Debug switch after a failed write
    # turned logging off in the background.
    api = Api(APP_VERSION)
    monkeypatch.setattr(debug, "_on", False)
    assert api.drain_debug_warnings()["enabled"] is False
    monkeypatch.setattr(debug, "_on", True)
    assert api.drain_debug_warnings()["enabled"] is True


def test_on_warning_never_calls_evaluate_js():
    api = Api(APP_VERSION)

    class FakeWindow:
        def __init__(self):
            self.calls = []

        def evaluate_js(self, script):
            self.calls.append(script)
            raise AssertionError("evaluate_js must never be called from on_warning")

    fake_window = FakeWindow()
    api._set_window(fake_window)

    api._on_debug_warning("Debug log: write failed.")

    assert fake_window.calls == []
    assert api.drain_debug_warnings()["warnings"] == ["Debug log: write failed."]


def test_set_debug_return_includes_and_drains_warnings(tmp_path, monkeypatch):
    api = Api(APP_VERSION)
    monkeypatch.setattr(debug, "on_warning", api._on_debug_warning)
    api._on_debug_warning("Debug log: an earlier warning.")

    result = api.set_debug(False)

    assert result["warnings"] == ["Debug log: an earlier warning."]
    assert api.drain_debug_warnings()["warnings"] == []


def test_poll_queue_includes_and_drains_debug_warnings(sftp_env):
    api, _, _ = sftp_env
    api._on_debug_warning("Debug log: could not delete old log.")

    status = api.poll_queue()

    assert status["debug_warnings"] == ["Debug log: could not delete old log."]
    assert status["debug_enabled"] is False
    assert api.poll_queue()["debug_warnings"] == []


def test_failed_write_turns_logging_off_and_produces_a_warning(tmp_path):
    # The log folder itself does not exist, so the very first write fails.
    missing_dir = str(tmp_path / "does_not_exist")
    warnings = []
    dbg = DebugLog(missing_dir, "Test App", redact=_scrub, on_warning=warnings.append)

    ok = dbg.set_enabled(True)

    assert ok is False
    assert dbg.is_enabled() is False
    # The missing folder also fails the prune scan that runs first, so there
    # are two warnings here; the create failure is the one that matters.
    assert any("could not create" in w for w in warnings)


def test_paramiko_records_never_reach_debug_log(tmp_path, monkeypatch):
    import logging
    from app import debug as debug_module

    dbg = DebugLog(str(tmp_path), "Test App", redact=_scrub)
    assert dbg.set_enabled(True)
    monkeypatch.setattr(debug_module, "debug", dbg)
    logging.getLogger("paramiko").warning("MARKERWORD")
    logging.getLogger("paramiko.transport.sftp").warning("MARKERWORD")
    assert "MARKERWORD" not in open(dbg._path, encoding="utf-8").read()
