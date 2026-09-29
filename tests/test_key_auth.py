"""
Tests for key-based login and for installing a public key on the server.

Key login: the right key connects (and no password is remembered), a wrong key
is refused with a plain-language message, and a passphrase-protected key
connects with its passphrase. Key file problems (wrong or missing passphrase
for Ed25519 and RSA keys, a public key or other non-key file, a missing file)
each get their own plain message before any network contact. install_pubkey: refuses
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
    assert api._connected is True
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
    assert api._connected is False
    assert api._client is None


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


def test_rsa_passphrase_key_connects_with_right_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    private_path = tmp_path / "id_rsa"
    assert Api(APP_VERSION).generate_key("RSA", str(private_path), "s3cret-phrase")["ok"]
    pkey = paramiko.RSAKey.from_private_key_file(str(private_path), password="s3cret-phrase")
    port, _root, _local, srv_sock = _bring_up_server(tmp_path, client_key=pkey)
    params = {"host": "127.0.0.1", "port": port, "username": USER,
              "key_path": str(private_path), "passphrase": "s3cret-phrase"}
    api = Api(APP_VERSION)
    try:
        result = _connect(api, params)
        assert result["ok"] is True
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
        result = api.connect(params)
        assert result["ok"] is False
        assert result["error"] == "Couldn't unlock the key. Check the passphrase."
        assert "host_key_unknown" not in result
        assert api._connected is False
    finally:
        srv_sock.close()


# ───────────── key file problems (caught before any network contact) ─────────────
#
# The key is loaded before the connection opens, so these point at a port
# nothing listens on: a reached network step would fail as "Connection
# refused" instead of the key message asserted.

def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _key_params(key_path, passphrase=""):
    return {"host": "127.0.0.1", "port": _closed_port(), "username": USER,
            "key_path": str(key_path), "passphrase": passphrase}


def _key_error(tmp_path, monkeypatch, key_path, passphrase=""):
    monkeypatch.setattr(paths, "KNOWN_HOSTS_FILE", str(tmp_path / "known_hosts"))
    api = Api(APP_VERSION)
    result = api.connect(_key_params(key_path, passphrase))
    assert result["ok"] is False
    assert api._connected is False
    assert api._client is None
    assert result["tips"]
    return result["error"]


@pytest.mark.parametrize("key_type", ["Ed25519", "RSA"])
def test_wrong_passphrase_message_for_each_key_type(tmp_path, monkeypatch, key_type):
    path = tmp_path / "id_pass"
    assert Api(APP_VERSION).generate_key(key_type, str(path), "s3cret-phrase")["ok"]

    error = _key_error(tmp_path, monkeypatch, path, "wrong-phrase")

    assert error == "Couldn't unlock the key. Check the passphrase."


@pytest.mark.parametrize("key_type", ["Ed25519", "RSA"])
def test_blank_passphrase_on_encrypted_key_asks_for_it(tmp_path, monkeypatch, key_type):
    path = tmp_path / "id_pass"
    assert Api(APP_VERSION).generate_key(key_type, str(path), "s3cret-phrase")["ok"]

    error = _key_error(tmp_path, monkeypatch, path, "")

    assert error == "This key is protected by a passphrase. Enter it and try again."


@pytest.mark.parametrize("passphrase", ["", "some-phrase"])
def test_public_key_file_is_not_a_private_key(tmp_path, monkeypatch, passphrase):
    _priv, _text, _key = _make_key(tmp_path, "id_good")

    error = _key_error(tmp_path, monkeypatch, tmp_path / "id_good.pub", passphrase)

    assert error == "That file isn't an SSH private key this app can read."


@pytest.mark.parametrize("content", [b"", bytes(range(256)) * 4,
                                     b"PuTTY-User-Key-File-3: ssh-ed25519\n"],
                         ids=["empty", "binary", "putty"])
def test_non_key_file_is_not_a_private_key(tmp_path, monkeypatch, content):
    path = tmp_path / "not_a_key"
    path.write_bytes(content)

    error = _key_error(tmp_path, monkeypatch, path, "")

    assert error == "That file isn't an SSH private key this app can read."


def test_missing_key_file_names_the_path(tmp_path, monkeypatch):
    path = tmp_path / "no_such_key"

    error = _key_error(tmp_path, monkeypatch, path, "")

    assert error == f"Not found: {path}"


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
