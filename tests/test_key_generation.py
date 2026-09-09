"""Tests for local SSH key generation."""

import io
import os
import sys

import paramiko
import pytest
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


def test_generate_key_success_retries_backup_delete_on_transient_failure(tmp_path, monkeypatch):
    # Regression test: on a successful overwrite, the old code cleared
    # backup_path to None even when os.remove(backup_path) raised (e.g. a
    # brief antivirus/indexer lock on Windows). That swallowed the error and
    # also stopped `finally`'s cleanup loop from ever retrying, silently
    # leaving the raw old private key behind in a .sftpkey_bak_* file even
    # though the function reported ok: True. Only os.remove for the backup
    # file is made to fail here; the private/public temp files are
    # published via os.replace, not os.remove, so this doesn't disturb the
    # normal publish path.
    private_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    old_priv = b"old private marker"
    old_pub = "old public marker"
    private_path.write_bytes(old_priv)
    pub_path.write_text(old_pub)

    real_remove = os.remove
    calls = []

    def flaky_remove(path, *args, **kwargs):
        if os.path.basename(path).startswith(".sftpkey_bak_"):
            calls.append(path)
            if len(calls) == 1:
                # First attempt (the success-path delete) hits a transient
                # lock. Later attempts (finally's retry loop) succeed, as a
                # real transient antivirus/indexer lock would clear shortly.
                raise OSError("simulated transient lock")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", flaky_remove)

    result = app.Api().generate_key(
        "Ed25519", str(private_path), "test-passphrase", overwrite=True
    )

    assert result["ok"] is True
    # The first delete attempt (in the success path, before `finally`) must
    # have been made and failed, proving the retry in `finally` is what
    # actually cleaned things up.
    assert len(calls) >= 1

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".sftpkey_bak_")]
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


def test_generate_key_restore_failure_leaves_durable_backup(tmp_path, monkeypatch):
    # Public swap fails, and the rollback's own restore rename fails too
    # (e.g. the folder became briefly unwritable). The durable backup file
    # must survive on disk as the recovery artifact instead of being wiped
    # by `finally`, and out_path (left holding the new, mismatched key) must
    # never be left truncated.
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
            # Call 1 is the private swap (succeeds). Call 2 is the public
            # swap, which fails and triggers the rollback.
            raise OSError("simulated publish failure")
        if len(calls) == 3:
            # Call 3 is the rollback's own restore rename (backup_path ->
            # out_path). Fail that too.
            raise OSError("simulated restore failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)

    result = app.Api().generate_key(
        "Ed25519", str(private_path), "test-passphrase", overwrite=True
    )

    assert result["ok"] is False
    assert len(calls) == 3

    backups = [p for p in tmp_path.iterdir() if p.name.startswith(".sftpkey_bak_")]
    assert len(backups) == 1
    assert backups[0].read_bytes() == old_priv

    assert private_path.exists()
    assert len(private_path.read_bytes()) > 0


def test_generate_key_interrupted_between_swaps_leaves_durable_backup(tmp_path, monkeypatch):
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
            # Simulate the process dying (power loss / kill) between the
            # private swap and the public swap. KeyboardInterrupt isn't a
            # subclass of Exception, so it skips generate_key's own
            # rollback handler (which only catches Exception) the same way
            # a real hard kill never runs any of that code either.
            raise KeyboardInterrupt("simulated hard exit")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)

    # A real hard kill also never reaches generate_key's `finally` cleanup,
    # so the backup-removal step there never runs either. Model that too by
    # making removal of the backup file a no-op, leaving everything else
    # untouched.
    real_remove = os.remove

    def remove_but_keep_backup(path, *args, **kwargs):
        if os.path.basename(path).startswith(".sftpkey_bak_"):
            return
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", remove_but_keep_backup)

    with pytest.raises(KeyboardInterrupt):
        app.Api().generate_key(
            "Ed25519", str(private_path), "test-passphrase", overwrite=True
        )

    backups = [p for p in tmp_path.iterdir() if p.name.startswith(".sftpkey_bak_")]
    assert len(backups) == 1
    assert backups[0].read_bytes() == old_priv


def test_generate_key_publish_failure_with_no_prior_key_removes_orphan(tmp_path, monkeypatch):
    private_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    # No prior private or public key exists, so there is nothing to back up
    # or restore: a failed publish should just remove the newly written
    # private key rather than leave an orphaned file behind.

    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst, *args, **kwargs):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError("simulated publish failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)

    result = app.Api().generate_key("Ed25519", str(private_path), "test-passphrase")

    assert result["ok"] is False
    assert not private_path.exists()
    assert not pub_path.exists()

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
