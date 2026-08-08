"""Tests for local SSH key generation."""

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
