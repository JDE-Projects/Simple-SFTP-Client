"""
The same strict port rule applies at every entry point that takes a port
from outside: Test, Connect, and Save. Blank means 22, but any non-blank
invalid port (out of range, non-numeric, or padded with stray whitespace)
must be refused with a clear message - never silently substituted with 22.

test_connection() used to catch the ValueError from int(port or 22) and
silently test port 22 instead of refusing. These tests demonstrate that
regression is gone: for an invalid port, the socket layer (and, for
connect(), _open()) must never be reached at all.
"""
import pytest

import simple_sftp_client as app

INVALID_PORTS = ["0", "65536", "-1", "abc", "22 ", " 22", "999999", "1.5"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SESSIONS_FILE", str(tmp_path / "servers.json"))
    return app.Api()


# ───────────── test_connection ─────────────

@pytest.mark.parametrize("port", INVALID_PORTS)
def test_test_connection_refuses_invalid_port_without_connecting(api, port, monkeypatch):
    calls = []

    def spy_create_connection(addr, timeout=None):
        calls.append(addr)
        raise OSError("should not be reached")

    monkeypatch.setattr(app.socket, "create_connection", spy_create_connection)

    result = api.test_connection({"host": "example.com", "port": port})

    assert result["ok"] is False
    assert result.get("error")
    assert calls == []


def test_test_connection_blank_port_defaults_to_22(api, monkeypatch):
    seen = {}

    def fake_create_connection(addr, timeout=None):
        seen["addr"] = addr
        raise OSError("refused")

    monkeypatch.setattr(app.socket, "create_connection", fake_create_connection)

    api.test_connection({"host": "example.com", "port": ""})

    assert seen["addr"] == ("example.com", 22)


def test_test_connection_valid_nondefault_port_used_as_given(api, monkeypatch):
    seen = {}

    def fake_create_connection(addr, timeout=None):
        seen["addr"] = addr
        raise OSError("refused")

    monkeypatch.setattr(app.socket, "create_connection", fake_create_connection)

    api.test_connection({"host": "example.com", "port": "2222"})

    assert seen["addr"] == ("example.com", 2222)


# ───────────── connect ─────────────

@pytest.mark.parametrize("port", INVALID_PORTS)
def test_connect_refuses_invalid_port_before_opening(api, port, monkeypatch):
    def must_not_be_called(*a, **k):
        raise AssertionError("_open must not be reached for an invalid port")

    monkeypatch.setattr(app.Api, "_open", must_not_be_called)

    result = api.connect({"host": "example.com", "username": "u", "password": "pw", "port": port})

    assert result["ok"] is False
    assert result.get("error")


def test_connect_blank_port_defaults_to_22(api, monkeypatch):
    seen = {}

    class FakeSftp:
        def normalize(self, path):
            return "/home/u"

        def stat(self, path):
            return object()

    class FakeClient:
        def open_sftp(self):
            return FakeSftp()

    def fake_open(self, host, port, username, password, key_path, passphrase):
        seen["port"] = port
        return FakeClient()

    monkeypatch.setattr(app.Api, "_open", fake_open)
    monkeypatch.setattr(app.Api, "_transport_info", lambda self, client=None: {})
    monkeypatch.setattr(app.Api, "_sweep_scratch_files", lambda self: None)

    result = api.connect({"host": "example.com", "username": "u", "password": "pw", "port": ""})

    assert result["ok"] is True
    assert seen["port"] == 22


# ───────────── save_session ─────────────

@pytest.mark.parametrize("port", INVALID_PORTS)
def test_save_session_refuses_invalid_port(api, port):
    result = api.save_session({"name": "x", "host": "h", "port": port, "username": "u",
                                "auth": "password", "key_path": "", "start_path": "",
                                "remember": False})

    assert result["ok"] is False
    assert result.get("error")


def test_save_session_blank_port_accepted(api):
    result = api.save_session({"name": "x", "host": "h", "port": "", "username": "u",
                                "auth": "password", "key_path": "", "start_path": "",
                                "remember": False})

    assert result["ok"] is True


def test_save_session_valid_nondefault_port_accepted(api):
    result = api.save_session({"name": "x", "host": "h", "port": "2222", "username": "u",
                                "auth": "password", "key_path": "", "start_path": "",
                                "remember": False})

    assert result["ok"] is True
