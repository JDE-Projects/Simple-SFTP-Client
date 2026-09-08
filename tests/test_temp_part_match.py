"""is_temp_part must recognize only the app's own transfer scratch files.

local_temp_path / remote_temp_path build one exact shape: a leading dot, the
original name, a dot, 8 lowercase hex characters, then .sxtpart. The old check
matched any name ending in .sxtpart, so an ordinary user file like
"notes.sxtpart" was hidden from listings and deleted on the next connect. The
"does not match" cases below fail against that old behavior and pass now.
"""

import os

import pytest

from simple_sftp_client import is_temp_part, local_temp_path, remote_temp_path


# Ordinary files that merely share the suffix, or partial shapes, are NOT ours.
NOT_SCRATCH = [
    "",                             # empty name
    "notes.sxtpart",               # bare suffix, no leading dot, no token
    ".notes.sxtpart",              # leading dot but no random token
    "report.sxtpart",              # plain user file
    ".sxtpart",                    # suffix only
    ".name.deadbeef.txt",          # right token, wrong suffix
    ".name.dead.sxtpart",          # token too short
    ".name.deadbeef0.sxtpart",     # token too long
    ".name.DEADBEEF.sxtpart",      # uppercase hex (urandom.hex is lowercase)
    ".name.deadbeeg.sxtpart",      # non-hex character in token
    "name.deadbeef.sxtpart",       # missing leading dot
    ".name.deadbeef.sxtpart\n",    # trailing newline (fullmatch, not "$")
]

# The exact generated shape IS ours.
SCRATCH = [
    ".notes.txt.deadbeef.sxtpart",
    ".a.00000000.sxtpart",
    ".archive.tar.gz.cafebabe.sxtpart",
]


@pytest.mark.parametrize("name", NOT_SCRATCH)
def test_ordinary_names_are_not_scratch(name):
    assert is_temp_part(name) is False


@pytest.mark.parametrize("name", SCRATCH)
def test_generated_shape_is_scratch(name):
    assert is_temp_part(name) is True


def test_local_temp_path_names_are_recognized():
    for final in ("data.bin", "notes.sxtpart", "archive.tar.gz"):
        temp = os.path.basename(local_temp_path(os.path.join("/browse", final)))
        assert is_temp_part(temp) is True


def test_remote_temp_path_names_are_recognized():
    for final in ("/home/u/data.bin", "/home/u/notes.sxtpart"):
        temp = os.path.basename(remote_temp_path(final))
        assert is_temp_part(temp) is True
