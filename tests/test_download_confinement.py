"""
Tests for the path-traversal guard on downloads: a server-supplied filename
must never be able to place a file outside the local folder the user
selected. Covers the low-level helper (safe_local_child) directly, and the
enqueue() download path end to end against the in-process SFTP server.
"""
import os

import pytest

from simple_sftp_client import safe_local_child


# ───────────── unit tests: safe_local_child ─────────────

@pytest.mark.parametrize("name", [
    "..",
    "../../x",
    "C:\\evil.bin",
    "C:/evil.bin",
    "\\\\server\\share\\x",
    "a/b",
    "a\\b",
    ".",
    "",
    "/etc/x",
])
def test_safe_local_child_rejects_hostile_names(tmp_path, name):
    root = str(tmp_path)
    with pytest.raises(ValueError):
        safe_local_child(root, name, root)


@pytest.mark.parametrize("name", ["a.bin", "file.txt"])
def test_safe_local_child_accepts_normal_names(tmp_path, name):
    root = str(tmp_path)
    result = safe_local_child(root, name, root)
    assert os.path.abspath(result) == os.path.abspath(os.path.join(root, name))
    # stays under root
    assert os.path.commonpath([os.path.abspath(root), os.path.abspath(result)]) == \
        os.path.abspath(root)


# ───────────── integration: enqueue() download path ─────────────

def test_download_refuses_traversal_name_and_still_drains(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    # A real file the traversal name would resolve to, if it were allowed
    # through: proves the escape is refused rather than just failing to find
    # the file.
    (server_root / "escape.bin").write_bytes(b"hostile payload")

    parent = local_dir.parent
    before = set(os.listdir(parent))

    result = api.enqueue([{"name": "../escape.bin", "is_dir": False}], "download",
                          str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["scanning"] is True

    wait_for_drain(api)

    after = set(os.listdir(parent))
    assert after == before, "traversal name must not write outside local_dir"
    assert not (parent / "escape.bin").exists()
    assert not (local_dir / "escape.bin").exists()
    assert not (local_dir / ".." / "escape.bin").resolve().exists()
