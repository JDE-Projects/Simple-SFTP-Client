"""Fallback integration coverage against the separate Simple SFTP Server app."""

import os
import subprocess
import sys
from pathlib import Path

import paramiko
import pytest
from paramiko.sftp import SFTP_OP_UNSUPPORTED

from app.api import Api
from app.services import transfer_io
from simple_sftp_client import APP_VERSION


SERVER_REPO = Path(os.environ.get(
    "SXT_SERVER_REPO", Path(__file__).resolve().parents[2] / "Simple SFTP Server"))
if not SERVER_REPO.is_dir():
    pytest.skip("Simple SFTP Server repository is unavailable", allow_module_level=True)


@pytest.fixture
def real_server(tmp_path):
    helper = Path(__file__).with_name("real_server_fallback_helper.py")
    command = [sys.executable, "-B", str(helper), "--server-repo", str(SERVER_REPO),
               "--root", str(tmp_path)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env)
    try:
        line = process.stdout.readline().strip()
        if not line:
            stderr = process.stderr.read()
            pytest.skip(f"Simple SFTP Server could not start: {stderr}")
        yield int(line), tmp_path / "home"
    finally:
        if process.stdin:
            process.stdin.close()
        process.wait(timeout=10)


def _client(port):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("127.0.0.1", port=port, username="fallback", password="fallback-password",
                   look_for_keys=False, allow_agent=False)
    return client, client.open_sftp()


def _put(sftp, source, remote, api=None):
    api = api or Api(APP_VERSION)
    return transfer_io._put_file(api, sftp, str(source), remote, lambda *_: None, lambda: False)


@pytest.fixture
def no_server_swap(monkeypatch):
    """Make the client see the server refuse posix-rename as unsupported.

    The real server supports the one-step swap, so this is the only way to
    drive the fallback path against it.
    """
    def unsupported(*_args):
        error = IOError("operation unsupported")
        error.sftp_status = SFTP_OP_UNSUPPORTED
        raise error

    monkeypatch.setattr(transfer_io, "_posix_rename_with_status", unsupported)


def _logging_api(monkeypatch):
    api = Api(APP_VERSION)
    logged = []
    monkeypatch.setattr(api, "_worker_log", lambda msg, *a, **k: logged.append(msg))
    return api, logged


def test_real_server_new_file_fallback(real_server, tmp_path, monkeypatch, no_server_swap):
    port, home = real_server
    source = tmp_path / "new.txt"
    source.write_bytes(b"new")
    api, logged = _logging_api(monkeypatch)
    client, sftp = _client(port)
    renames = []
    real_rename = sftp.rename
    monkeypatch.setattr(sftp, "rename",
                        lambda old, new: (renames.append(new), real_rename(old, new)))
    try:
        assert _put(sftp, source, "/new.txt", api)
    finally:
        sftp.close()
        client.close()
    assert renames == ["/new.txt"]
    assert not [msg for msg in logged if "two steps" in msg]
    assert (home / "new.txt").read_bytes() == b"new"
    assert not list(home.glob("*.sxtpart"))


def test_real_server_overwrite_fallback(real_server, tmp_path, monkeypatch, no_server_swap):
    port, home = real_server
    (home / "existing.txt").write_bytes(b"old")
    source = tmp_path / "existing.txt"
    source.write_bytes(b"new")
    api, logged = _logging_api(monkeypatch)
    client, sftp = _client(port)
    try:
        assert _put(sftp, source, "/existing.txt", api)
    finally:
        sftp.close()
        client.close()
    assert [msg for msg in logged if "two steps" in msg]
    assert (home / "existing.txt").read_bytes() == b"new"
    assert not list(home.glob("*.sxtpart"))


def test_real_server_permission_denied_leaves_no_scratch(tmp_path):
    helper = Path(__file__).with_name("real_server_fallback_helper.py")
    command = [sys.executable, "-B", str(helper), "--server-repo", str(SERVER_REPO),
               "--root", str(tmp_path), "--no-upload"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    try:
        port = int(process.stdout.readline().strip())
        source = tmp_path / "denied.txt"
        source.write_bytes(b"new")
        client, sftp = _client(port)
        try:
            with pytest.raises(IOError, match="Permission denied|denied"):
                _put(sftp, source, "/denied.txt")
        finally:
            sftp.close()
            client.close()
        assert not list((tmp_path / "home").glob("*.sxtpart"))
    finally:
        if process.stdin:
            process.stdin.close()
        process.wait(timeout=10)


def test_real_server_one_step_swap(real_server, tmp_path, monkeypatch):
    port, home = real_server
    (home / "existing.txt").write_bytes(b"old")
    source = tmp_path / "existing.txt"
    source.write_bytes(b"new")
    api, logged = _logging_api(monkeypatch)
    client, sftp = _client(port)
    fallback_calls = []
    monkeypatch.setattr(sftp, "remove", lambda *a: fallback_calls.append(("remove", a)))
    monkeypatch.setattr(sftp, "rename", lambda *a: fallback_calls.append(("rename", a)))
    try:
        assert _put(sftp, source, "/existing.txt", api)
    finally:
        sftp.close()
        client.close()
    assert fallback_calls == []
    assert not [msg for msg in logged if "two steps" in msg]
    assert (home / "existing.txt").read_bytes() == b"new"
    assert not list(home.glob("*.sxtpart"))
