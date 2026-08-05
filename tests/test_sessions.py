"""
Tests for save_session / delete_session in simple_sftp_client.py.

keyring is mocked throughout: set_password / get_password / delete_password
are monkeypatched on the real `keyring` module (imported locally inside the
functions under test picks up the same module object from sys.modules), so
no test ever touches the real Windows Credential Manager. SESSIONS_FILE is
redirected to a tmp path so no test ever touches the real servers.json.
"""
import json

import keyring
import pytest

import simple_sftp_client as app


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A fresh Api instance with servers.json redirected to a tmp file."""
    monkeypatch.setattr(app, "SESSIONS_FILE", str(tmp_path / "servers.json"))
    return app.Api()


def base_session(**overrides):
    s = {
        "name": "test-session",
        "host": "example.com",
        "port": "22",
        "username": "alice",
        "auth": "password",
        "key_path": "",
        "start_path": "",
        "remember": True,
    }
    s.update(overrides)
    return s


def test_save_session_keyring_failure_clears_remember_and_reports_error(api, monkeypatch):
    def failing_set_password(service, key, password):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(keyring, "set_password", failing_set_password)

    api._cred_pass = "hunter2"
    result = api.save_session(base_session())

    assert result["ok"] is True
    assert result["pw_saved"] is False
    assert "pw_error" in result and result["pw_error"]

    saved = result["sessions"][0]
    assert saved["remember"] is False


def test_save_session_keyring_success_keeps_remember_and_reports_saved(api, monkeypatch):
    calls = []

    def ok_set_password(service, key, password):
        calls.append((service, key, password))

    monkeypatch.setattr(keyring, "set_password", ok_set_password)

    api._cred_pass = "hunter2"
    result = api.save_session(base_session())

    assert result["ok"] is True
    assert result["pw_saved"] is True
    assert "pw_error" not in result

    saved = result["sessions"][0]
    assert saved["remember"] is True
    assert calls == [("SimpleSFTPClient", "example.com|alice", "hunter2")]


def test_delete_session_removes_keyring_credential_when_remembered(api, monkeypatch):
    # Seed servers.json directly with a session that has a saved password.
    with open(app.SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"sessions": [base_session()]}, f)

    deleted = []

    def ok_delete_password(service, key):
        deleted.append((service, key))

    monkeypatch.setattr(keyring, "delete_password", ok_delete_password)

    result = api.delete_session("test-session")

    assert result["ok"] is True
    assert result["sessions"] == []
    assert deleted == [("SimpleSFTPClient", "example.com|alice")]


def test_delete_session_does_not_touch_keyring_when_no_saved_password(api, monkeypatch):
    with open(app.SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"sessions": [base_session(remember=False)]}, f)

    calls = []

    def spy_delete_password(service, key):
        calls.append((service, key))

    monkeypatch.setattr(keyring, "delete_password", spy_delete_password)

    result = api.delete_session("test-session")

    assert result["ok"] is True
    assert result["sessions"] == []
    assert calls == []
