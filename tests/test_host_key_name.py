"""
connect() must pin a pending host key under the port-aware name paramiko
actually checks against (bracketed for non-standard ports), not the raw
hostname the exception carries. On a non-standard port the changed-key path
hands back a bare host, so pinning by that name stored the new key somewhere
the library never rechecked and the "host key changed" warning looped forever.

These drive connect() with a stubbed _open that raises, so no network or real
server is involved. hostkey_name(host, port) is the source of truth for the name.
"""
import paramiko
import pytest

from simple_sftp_client import Api, UnknownHostKey, hostkey_name

HOST = "example.com"
PORT = 2222  # non-standard, so the name must be bracketed: [example.com]:2222


@pytest.fixture
def keys():
    # two distinct real keys: one offered, one "expected" (for the changed case)
    return paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)


def _payload():
    return {"host": HOST, "username": "u", "password": "pw", "port": PORT}


def test_unknown_host_key_pins_port_aware_name(monkeypatch, keys):
    offered, _ = keys

    def fake_open(*a, **k):
        # a bare host in the exception, on purpose: the fix must ignore it
        raise UnknownHostKey(HOST, offered)

    api = Api()
    monkeypatch.setattr(api, "_open", fake_open)

    result = api.connect(_payload())

    assert result["host_key_unknown"] is True
    assert api._pending_host_key[0] == hostkey_name(HOST, PORT)
    assert api._pending_host_key[0] == "[example.com]:2222"
    assert api._pending_host_key[1] is offered


def test_changed_host_key_pins_port_aware_name(monkeypatch, keys):
    offered, expected = keys

    def fake_open(*a, **k):
        # BadHostKeyException carries the bare host on this path
        raise paramiko.BadHostKeyException(HOST, offered, expected)

    api = Api()
    monkeypatch.setattr(api, "_open", fake_open)

    result = api.connect(_payload())

    assert result["host_key_changed"] is True
    assert api._pending_host_key[0] == hostkey_name(HOST, PORT)
    assert api._pending_host_key[0] == "[example.com]:2222"
    assert api._pending_host_key[1] is offered


def test_default_port_name_is_bare(monkeypatch, keys):
    offered, _ = keys

    def fake_open(*a, **k):
        raise UnknownHostKey(HOST, offered)

    api = Api()
    monkeypatch.setattr(api, "_open", fake_open)

    payload = _payload()
    payload["port"] = 22
    api.connect(payload)

    # port 22 is stored bare, no brackets
    assert api._pending_host_key[0] == HOST
