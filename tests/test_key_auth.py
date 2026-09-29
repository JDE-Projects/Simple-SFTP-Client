"""
Tests for key-based login and for installing a public key on the server.

Key login: the right key connects (and no password is remembered), a wrong key
or a wrong passphrase is refused with a plain-language message, and a
passphrase-protected key connects with its passphrase. install_pubkey: refuses
when not connected, creates .ssh and authorized_keys, does not add the same key
twice, and appends to an existing file without disturbing it.

Runs against the in-process SFTP server from conftest.py. The server accepts
one client public key. Every key is generated fresh in tmp_path; no real key is
read. The test server ignores permission modes, so file modes are not checked.
"""
import paramiko
import pytest

from app import paths
from app.api import Api
from simple_sftp_client import APP_VERSION
from tests.conftest import _bring_up_server
from tools.sftp_server_core import USER


def _make_key(tmp_path, name, passphrase=""):
    """Generate an Ed25519 key with the app's generator. Returns (private
    path, public text, loaded paramiko key)."""
    private_path = tmp_path / name
    result = Api(APP_VERSION).generate_key("Ed25519", str(private_path), passphrase)
    assert result["ok"] is True
    pkey = paramiko.Ed25519Key.from_private_key_file(
        str(private_path), password=passphrase or None)
    return private_path, result["public"], pkey


@pytest.fixture
def key_server(tmp_path, monkeypatch):
    """A throwaway server that also accepts one freshly generated key. Yields
    a helper-friendly tuple (params, server_root, private_path, public_text)
    where params has no password, only the key path."""
    monkeypatch.setattr(paths, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    private_path, public_text, pkey = _make_key(tmp_path, "id_good")
    port, server_root, _local, srv_sock = _bring_up_server(tmp_path, client_key=pkey)
    params = {"host": "127.0.0.1", "port": port, "username": USER,
              "key_path": str(private_path)}
    try:
        yield params, server_root, private_path, public_text
    finally:
        srv_sock.close()


def _connect(api, params):
    """Connect the way the UI does: the first contact asks about the unknown
    host, the user trusts it, then the connect is retried."""
    first = api.connect(params)
    assert first.get("host_key_unknown") is True
    assert api.trust_host_key()["ok"] is True
    return api.connect(params)


# ───────────── key connect ─────────────

def test_right_key_connects_and_remembers_no_password(key_server):
    params, _root, _priv, _pub = key_server
    api = Api(APP_VERSION)
    api._cred_pass = "stale"

    result = _connect(api, params)

    assert result["ok"] is True
    assert api.connected is True
    assert api._cred_pass == ""
    api.disconnect()


def test_wrong_key_is_refused_plainly(key_server, tmp_path):
    params, _root, _priv, _pub = key_server
    other_path, _text, _key = _make_key(tmp_path, "id_other")
    params = dict(params, key_path=str(other_path))
    api = Api(APP_VERSION)

    result = _connect(api, params)

    assert result["ok"] is False
    assert result["error"] == "Authentication failed. Check the username, password, or key."
    assert "Traceback" not in result["error"]
    assert api.connected is False
    assert api.client is None


def test_passphrase_key_connects_with_right_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    private_path, _text, pkey = _make_key(tmp_path, "id_pass", "s3cret-phrase")
    port, _root, _local, srv_sock = _bring_up_server(tmp_path, client_key=pkey)
    params = {"host": "127.0.0.1", "port": port, "username": USER,
              "key_path": str(private_path), "passphrase": "s3cret-phrase"}
    api = Api(APP_VERSION)
    try:
        result = _connect(api, params)
        assert result["ok"] is True
        assert api._cred_pass == ""
        api.disconnect()
    finally:
        srv_sock.close()


def test_passphrase_key_wrong_passphrase_is_refused_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    private_path, _text, pkey = _make_key(tmp_path, "id_pass", "s3cret-phrase")
    port, _root, _local, srv_sock = _bring_up_server(tmp_path, client_key=pkey)
    params = {"host": "127.0.0.1", "port": port, "username": USER,
              "key_path": str(private_path), "passphrase": "wrong-phrase"}
    api = Api(APP_VERSION)
    try:
        result = _connect(api, params)
        assert result["ok"] is False
        # paramiko's own short wording passes through as it is today.
        assert result["error"] == "Invalid key"
        assert api.connected is False
    finally:
        srv_sock.close()


# ───────────── install_pubkey ─────────────

def test_install_pubkey_not_connected():
    result = Api(APP_VERSION).install_pubkey("ssh-ed25519 AAAA test")

    assert result == {"ok": False, "error": "Not connected."}


def test_install_pubkey_creates_ssh_folder_and_file(sftp_env):
    api, server_root, _local = sftp_env
    pub = "ssh-ed25519 AAAAkeyone test@example"

    result = api.install_pubkey(pub)

    assert result == {"ok": True}
    assert (server_root / ".ssh").is_dir()
    assert (server_root / ".ssh" / "authorized_keys").read_text() == pub + "\n"


def test_install_pubkey_same_key_again_is_already_and_file_unchanged(sftp_env):
    api, server_root, _local = sftp_env
    pub = "ssh-ed25519 AAAAkeyone test@example"
    api.install_pubkey(pub)
    ak = server_root / ".ssh" / "authorized_keys"
    before = ak.read_bytes()

    result = api.install_pubkey(pub)

    assert result == {"ok": True, "already": True}
    assert ak.read_bytes() == before


def test_install_pubkey_adds_newline_when_file_lacks_one(sftp_env):
    api, server_root, _local = sftp_env
    ssh_dir = server_root / ".ssh"
    ssh_dir.mkdir()
    ak = ssh_dir / "authorized_keys"
    ak.write_bytes(b"ssh-rsa AAAAold old@example")

    result = api.install_pubkey("ssh-ed25519 AAAAnew new@example")

    assert result == {"ok": True}
    assert ak.read_text() == ("ssh-rsa AAAAold old@example\n"
                              "ssh-ed25519 AAAAnew new@example\n")


def test_install_pubkey_keeps_existing_content(sftp_env):
    api, server_root, _local = sftp_env
    ssh_dir = server_root / ".ssh"
    ssh_dir.mkdir()
    ak = ssh_dir / "authorized_keys"
    ak.write_bytes(b"ssh-rsa AAAAold old@example\n# a note\n")

    result = api.install_pubkey("ssh-ed25519 AAAAnew new@example")

    assert result == {"ok": True}
    assert ak.read_text() == ("ssh-rsa AAAAold old@example\n# a note\n"
                              "ssh-ed25519 AAAAnew new@example\n")
