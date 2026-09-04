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
    api._cred_identity = ("example.com", 22, "alice", "password")
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
    api._cred_identity = ("example.com", 22, "alice", "password")
    result = api.save_session(base_session())

    assert result["ok"] is True
    assert result["pw_saved"] is True
    assert "pw_error" not in result

    saved = result["sessions"][0]
    assert saved["remember"] is True
    assert calls == [("SimpleSFTPClient", "example.com|alice", "hunter2")]


def test_save_session_remember_with_no_password_does_not_claim_saved(api, monkeypatch):
    # Saving before a successful connect against these exact settings (no
    # cached password/identity) is refused, not silently skipped: the
    # session must not claim it, and the user gets a visible reason why.
    calls = []
    monkeypatch.setattr(keyring, "set_password",
                        lambda *a: calls.append(a))

    api._cred_pass = ""
    result = api.save_session(base_session())

    assert result["ok"] is True
    assert result["pw_saved"] is False
    assert "pw_error" in result and result["pw_error"]
    assert calls == []

    saved = result["sessions"][0]
    assert saved["remember"] is False


def test_save_session_edited_fields_refused(api, monkeypatch):
    # Cached password/identity belongs to a different server than the one
    # being saved (e.g. the host field was edited after connecting): refuse
    # rather than misfile the password under the new name.
    calls = []
    monkeypatch.setattr(keyring, "set_password",
                        lambda *a: calls.append(a))

    api._cred_pass = "pw"
    api._cred_identity = ("hostA", 22, "alice", "password")
    result = api.save_session(base_session(host="hostB"))

    assert result["pw_saved"] is False
    assert "pw_error" in result and result["pw_error"]
    assert calls == []
    assert result["sessions"][0]["remember"] is False


def test_save_session_auth_switch_refused_quietly(api, monkeypatch):
    # Cached identity is from a password login, but the session being saved
    # uses key auth: there is nothing to remember, so no write and no error.
    calls = []
    monkeypatch.setattr(keyring, "set_password",
                        lambda *a: calls.append(a))

    api._cred_pass = "pw"
    api._cred_identity = ("example.com", 22, "alice", "password")
    result = api.save_session(base_session(auth="key"))

    assert result["pw_saved"] is False
    assert "pw_error" not in result
    assert calls == []
    assert result["sessions"][0]["remember"] is False


def test_save_session_different_port_writes_under_port_aware_key(api, monkeypatch):
    calls = []

    def ok_set_password(service, key, password):
        calls.append((service, key, password))

    monkeypatch.setattr(keyring, "set_password", ok_set_password)

    api._cred_pass = "hunter2"
    api._cred_identity = ("example.com", 2222, "alice", "password")
    result = api.save_session(base_session(port="2222"))

    assert result["pw_saved"] is True
    assert calls == [("SimpleSFTPClient", "example.com|2222|alice", "hunter2")]


def test_cred_key_naming():
    assert app.cred_key("h", 22, "u") == "h|u"
    assert app.cred_key("h", 2222, "u") == "h|2222|u"


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


def test_delete_session_non_default_port_deletes_both_names(api, monkeypatch):
    # A session remembered on a non-22 port may have been saved before the
    # port-aware naming (legacy port-less entry) or after (port-aware
    # entry). Delete both so nothing is left behind either way.
    with open(app.SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"sessions": [base_session(port="2222")]}, f)

    deleted = []
    monkeypatch.setattr(keyring, "delete_password",
                         lambda service, key: deleted.append((service, key)))

    result = api.delete_session("test-session")

    assert result["ok"] is True
    assert set(deleted) == {
        ("SimpleSFTPClient", "example.com|2222|alice"),
        ("SimpleSFTPClient", "example.com|alice"),
    }


def test_remembered_password_falls_back_to_legacy_name_for_other_ports(api, monkeypatch):
    def get_password(service, key):
        if key == "h|2222|u":
            return None
        if key == "h|u":
            return "legacypw"
        return None

    monkeypatch.setattr(keyring, "get_password", get_password)

    assert api._remembered_password("h", "u", 2222) == "legacypw"


def test_remembered_password_port_22_uses_legacy_name_directly(api, monkeypatch):
    monkeypatch.setattr(keyring, "get_password",
                         lambda service, key: "pw" if key == "h|u" else None)

    assert api._remembered_password("h", "u", 22) == "pw"


def test_failed_connect_does_not_cache_password(api, monkeypatch):
    def boom(self, host, port, username, password, key_path, passphrase):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app.Api, "_open", boom)

    result = api.connect({"host": "example.com", "port": "22", "username": "alice",
                           "password": "hunter2", "key_path": "", "passphrase": ""})

    assert result["ok"] is False
    assert api._cred_pass == ""
    assert api._cred_identity is None


def test_cancelled_host_key_prompt_does_not_cache_password(api, monkeypatch):
    import paramiko

    key = paramiko.RSAKey.generate(1024)

    def raise_unknown(self, host, port, username, password, key_path, passphrase):
        raise app.UnknownHostKey(host, key)

    monkeypatch.setattr(app.Api, "_open", raise_unknown)

    result = api.connect({"host": "example.com", "port": "22", "username": "alice",
                           "password": "hunter2", "key_path": "", "passphrase": ""})

    assert result["ok"] is False
    assert result.get("host_key_unknown") is True
    assert api._cred_pass == ""
    assert api._cred_identity is None


def test_successful_password_login_stamps_identity(api, monkeypatch):
    class FakeSftp:
        def normalize(self, path):
            return "/home/alice"

        def stat(self, path):
            return object()

    class FakeClient:
        def open_sftp(self):
            return FakeSftp()

    def fake_open(self, host, port, username, password, key_path, passphrase):
        return FakeClient()

    monkeypatch.setattr(app.Api, "_open", fake_open)
    monkeypatch.setattr(app.Api, "_transport_info", lambda self: {})
    monkeypatch.setattr(app.Api, "_sweep_scratch_files", lambda self: None)

    result = api.connect({"host": " example.com ", "port": "22", "username": " alice ",
                           "password": "hunter2", "key_path": "", "passphrase": ""})

    assert result["ok"] is True
    assert api._cred_pass == "hunter2"
    assert api._cred_identity == ("example.com", 22, "alice", "password")
