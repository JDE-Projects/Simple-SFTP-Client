"""Tests for local SSH key generation."""

import io
import os
import sys

import paramiko
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_ssh_private_key

import simple_sftp_client as app


def test_generate_encrypted_ed25519_key(tmp_path):
    private_path = tmp_path / "id_ed25519"

    result = app.Api().generate_key(
        "Ed25519", str(private_path), "test-passphrase"
    )

    assert result["ok"] is True
    assert result["private_path"] == str(private_path)
    assert result["public_path"] == str(private_path) + ".pub"

    private_key = load_ssh_private_key(
        private_path.read_bytes(), b"test-passphrase"
    )
    assert isinstance(private_key, Ed25519PrivateKey)

    public_text = private_path.with_suffix(".pub").read_text(encoding="utf-8")
    assert public_text == result["public"] + "\n"
    assert public_text.startswith("ssh-ed25519 ")
    assert public_text.endswith(" simple-sftp-client\n")


def test_generate_unencrypted_ed25519_key(tmp_path):
    private_path = tmp_path / "id_ed25519"

    result = app.Api().generate_key("Ed25519", str(private_path), "")

    assert result["ok"] is True
    private_key = load_ssh_private_key(private_path.read_bytes(), password=None)
    assert isinstance(private_key, Ed25519PrivateKey)


def test_generate_rsa_key(tmp_path):
    private_path = tmp_path / "id_rsa"

    result = app.Api().generate_key("RSA", str(private_path), "")

    assert result["ok"] is True

    # paramiko writes RSA private keys out in traditional PEM format (not the
    # OpenSSH format the cryptography library expects), so load it back the
    # same way the app would use it to connect.
    key = paramiko.RSAKey.from_private_key(io.StringIO(private_path.read_text()))
    public_text = private_path.with_suffix(".pub").read_text(encoding="utf-8")
    assert public_text.startswith("ssh-rsa ")
    assert public_text.endswith(" simple-sftp-client\n")
    assert f"ssh-rsa {key.get_base64()}" in public_text


def test_generate_key_blocks_on_existing_private_file(tmp_path):
    private_path = tmp_path / "id_ed25519"
    original = b"not a real key, just a marker"
    private_path.write_bytes(original)

    result = app.Api().generate_key("Ed25519", str(private_path), "")

    assert result["ok"] is False
    assert result["needs_overwrite"] is True
    assert str(private_path) in result["existing"]
    assert not (tmp_path / "id_ed25519.pub").exists()
    assert private_path.read_bytes() == original


def test_generate_key_blocks_on_existing_public_file(tmp_path):
    private_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    pub_path.write_text("old public key marker")

    result = app.Api().generate_key("Ed25519", str(private_path), "")

    assert result["ok"] is False
    assert result["needs_overwrite"] is True
    assert str(pub_path) in result["existing"]
    assert not private_path.exists()
    assert pub_path.read_text() == "old public key marker"


def test_generate_key_overwrite_confirmed_replaces_pair(tmp_path):
    private_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    private_path.write_bytes(b"old private marker")
    pub_path.write_text("old public marker")

    result = app.Api().generate_key(
        "Ed25519", str(private_path), "test-passphrase", overwrite=True
    )

    assert result["ok"] is True
    private_key = load_ssh_private_key(
        private_path.read_bytes(), b"test-passphrase"
    )
    assert isinstance(private_key, Ed25519PrivateKey)
    assert pub_path.read_text().startswith("ssh-ed25519 ")

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".sftpkey_")]
    assert leftovers == []


def test_generate_key_rollback_on_publish_failure(tmp_path, monkeypatch):
    private_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    old_priv = b"old private marker"
    old_pub = "old public marker"
    private_path.write_bytes(old_priv)
    pub_path.write_text(old_pub)

    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst, *args, **kwargs):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError("simulated publish failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)

    result = app.Api().generate_key(
        "Ed25519", str(private_path), "test-passphrase", overwrite=True
    )

    assert result["ok"] is False
    assert private_path.read_bytes() == old_priv
    assert pub_path.read_text() == old_pub

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".sftpkey_")]
    assert leftovers == []


def test_generate_key_permission_error_returns_friendly_message(tmp_path, monkeypatch):
    private_path = tmp_path / "id_ed25519"

    def raise_permission_error(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(app.tempfile, "mkstemp", raise_permission_error)

    result = app.Api().generate_key("Ed25519", str(private_path), "")

    assert result["ok"] is False
    assert "permission" in result["error"].lower()
    assert not private_path.exists()


def test_protect_private_key_does_not_raise(tmp_path):
    target = tmp_path / "id_ed25519"
    target.write_bytes(b"placeholder")

    warning = app._protect_private_key(str(target))

    if sys.platform == "win32":
        assert warning is None
    else:
        assert isinstance(warning, str)
