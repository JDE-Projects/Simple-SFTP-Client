"""
Downloading a server folder onto a local folder that is a symlink or junction
follows it, as the user set it up, and warns in the log where the files will
really be written. The server cannot create local links, so the only links a
download meets are ones the user made.
"""
import os
import subprocess

import pytest

from simple_sftp_client import local_link_target


def _make_junction(link, target):
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   check=True, capture_output=True)


def _make_symlink(link, target):
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except OSError as e:
        pytest.skip(f"Windows refused to create a symlink here: {e}")


def _messages(api):
    return [(entry["msg"], entry["level"]) for entry in api._console_buffer]


@pytest.mark.parametrize("make_link", [_make_junction, _make_symlink],
                         ids=["junction", "symlink"])
def test_download_follows_link_and_warns(sftp_env, wait_for_drain, tmp_path, make_link):
    api, server_root, local_dir = sftp_env
    (server_root / "Photos").mkdir()
    (server_root / "Photos" / "a.txt").write_bytes(b"hello")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_link(local_dir / "Photos", elsewhere)

    result = api.enqueue([{"name": "Photos", "is_dir": True}], "download",
                         str(local_dir), "/")
    assert result["ok"] is True
    wait_for_drain(api)

    assert (elsewhere / "a.txt").read_bytes() == b"hello"
    expected = f"Photos is symlinked to {os.path.realpath(local_dir / 'Photos')}; files will be written there."
    assert (expected, "warn") in _messages(api)


def test_plain_folder_download_does_not_warn(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    (server_root / "Photos").mkdir()
    (server_root / "Photos" / "a.txt").write_bytes(b"hello")
    (local_dir / "Photos").mkdir()

    api.enqueue([{"name": "Photos", "is_dir": True}], "download", str(local_dir), "/")
    wait_for_drain(api)

    assert (local_dir / "Photos" / "a.txt").read_bytes() == b"hello"
    assert not any("symlinked" in msg for msg, _ in _messages(api))


def test_link_target_none_for_plain_or_missing_paths(tmp_path):
    (tmp_path / "plain").mkdir()
    assert local_link_target(str(tmp_path / "plain")) is None
    assert local_link_target(str(tmp_path / "missing")) is None
