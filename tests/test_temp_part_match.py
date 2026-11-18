"""Tests for scratch-path generation."""

import os
import posixpath
import re

from app.paths import local_temp_path, remote_temp_path


def _assert_shape(path, final, dirname, basename):
    assert dirname(path) == dirname(final)
    assert re.fullmatch(rf"\.{re.escape(basename(final))}\.[0-9a-f]{{8}}\.sxtpart",
                        basename(path))


def test_local_temp_path_stays_beside_final_file():
    for final in (r"C:\browse\data.bin", r"C:\browse\archive.tar.gz"):
        _assert_shape(local_temp_path(final), final, os.path.dirname, os.path.basename)


def test_remote_temp_path_stays_beside_final_file():
    for final in ("/home/u/data.bin", "/home/u/notes.sxtpart"):
        _assert_shape(remote_temp_path(final), final, posixpath.dirname, posixpath.basename)
