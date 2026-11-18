"""
Tests for the path-traversal guard on downloads: a server-supplied filename
must never be able to place a file outside the local folder the user
selected. Covers the low-level helper (safe_local_child) directly, and the
enqueue() download path end to end against the in-process SFTP server.
"""
import os

import pytest

from app.paths import safe_local_child, windows_destination_key, windows_name_problem


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


@pytest.mark.parametrize("name", ["a.bin", "file.txt", "Report.txt", "caf\u00e9.txt"])
def test_safe_local_child_accepts_normal_names(tmp_path, name):
    root = str(tmp_path)
    result = safe_local_child(root, name, root)
    assert os.path.abspath(result) == os.path.abspath(os.path.join(root, name))
    # stays under root
    assert os.path.commonpath([os.path.abspath(root), os.path.abspath(result)]) == \
        os.path.abspath(root)


@pytest.mark.parametrize("name", [
    "CON", "con", "Con.txt", "nul .txt", "PRN", "AUX.log", "COM1", "LPT9.bin",
    "COM\u00b9", "LPT\u00b3.log", "CONIN$", "conout$",
])
def test_windows_name_problem_rejects_reserved_names(name):
    assert windows_name_problem(name)


@pytest.mark.parametrize("name", ["name.", "name ", "name. ", "report.txt."])
def test_windows_name_problem_rejects_trailing_dot_and_space(name):
    assert windows_name_problem(name)


@pytest.mark.parametrize("name", [
    "a:b", "notes:hidden", "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "a\x01b", "a\x1fb",
])
def test_windows_name_problem_rejects_forbidden_characters(name):
    assert windows_name_problem(name)


@pytest.mark.parametrize("name", [
    "COM10", "LPT0x", "CONSOLE", "connect.txt", "nul_file", "Report.txt", "caf\u00e9.txt",
    "cafe\u0301.txt", "\u65e5\u672c\u8a9e.txt", ".hidden", "a.b.c",
])
def test_windows_name_problem_accepts_near_misses_and_unicode(name):
    assert windows_name_problem(name) is None


@pytest.mark.parametrize("name", ["CON.txt", "name.", "a:b"])
def test_safe_local_child_rejects_windows_unsafe_names(tmp_path, name):
    root = str(tmp_path)
    with pytest.raises(ValueError):
        safe_local_child(root, name, root)


def test_destination_key_matches_case_variants(tmp_path):
    assert windows_destination_key(str(tmp_path / "Docs" / "Report.txt")) == \
        windows_destination_key(str(tmp_path / "docs" / "REPORT.TXT"))


def test_destination_key_covers_folder_components(tmp_path):
    assert windows_destination_key(str(tmp_path / "A" / "report.txt")) != \
        windows_destination_key(str(tmp_path / "B" / "report.txt"))
    assert windows_destination_key(str(tmp_path / "Docs" / "a")) == \
        windows_destination_key(str(tmp_path / "docs" / "a"))


def test_destination_key_normalizes_path_spelling(tmp_path):
    assert windows_destination_key(str(tmp_path / "x" / ".." / "Docs" / "a")) == \
        windows_destination_key(str(tmp_path / "docs" / "a"))
    forward = f"{tmp_path}/docs/a"
    backward = forward.replace("/", "\\")
    assert windows_destination_key(forward) == windows_destination_key(backward)


def test_destination_key_keeps_unicode_forms_distinct(tmp_path):
    assert windows_destination_key(str(tmp_path / "caf\u00e9")) != \
        windows_destination_key(str(tmp_path / "cafe\u0301"))


def test_destination_key_greek_titlecase_pair(tmp_path):
    assert windows_destination_key(str(tmp_path / "\u1f80")) == \
        windows_destination_key(str(tmp_path / "\u1f88"))


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


def test_direct_download_skips_reserved_name_and_finishes_rest(sftp_env, wait_for_drain):
    api, server_root, local_dir = sftp_env
    payload = b"good payload"
    (server_root / "good.bin").write_bytes(payload)

    result = api.enqueue([
        {"name": "CON.txt", "is_dir": False},
        {"name": "good.bin", "is_dir": False},
    ], "download", str(local_dir), "/", "overwrite")
    assert result["ok"] is True
    assert result["scanning"] is True

    wait_for_drain(api)

    assert (local_dir / "good.bin").read_bytes() == payload
    assert not any(path.name.upper().startswith("CON") for path in local_dir.iterdir())
    with api._console_lock:
        messages = [line["msg"] for line in api._console_buffer]
    assert any("skipped unsafe remote name" in message and "CON.txt" in message for message in messages)
