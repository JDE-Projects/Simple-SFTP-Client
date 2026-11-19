"""Regression coverage for linear Windows-name pairing."""

import time

from app.api import Api
from app.services.compare_sync import _compute_pair_maps
from simple_sftp_client import APP_VERSION


def test_windows_name_pairing_scales_linearly(monkeypatch, tmp_path):
    api = Api(APP_VERSION)
    local_dir = str(tmp_path / "local")
    paired_per_folder = 100
    folder_count = 200

    def iter_local(_local, remote, _is_dir, **_kwargs):
        for number in range(folder_count):
            for file_number in range(paired_per_folder):
                rel = f"D{number}/file{file_number}.txt"
                local_rel = rel.replace("/", "\\")
                yield f"{local_dir}\\{local_rel}", f"{remote}/{rel}", 1, 1, False
            rel = f"D{number}/local-only.txt"
            local_rel = rel.replace("/", "\\")
            yield f"{local_dir}\\{local_rel}", f"{remote}/{rel}", 1, 1, False

    def iter_remote(_sftp, remote, local, _is_dir, _root, **_kwargs):
        for number in range(folder_count):
            for file_number in range(paired_per_folder):
                rel = f"d{number}/file{file_number}.txt"
                local_rel = rel.replace("/", "\\")
                yield f"{local}\\{local_rel}", f"{remote}/{rel}", 1, 1, False

    monkeypatch.setattr(api, "_iter_local", iter_local)
    monkeypatch.setattr(api, "_iter_remote", iter_remote)

    started = time.perf_counter()
    local_map, remote_map = _compute_pair_maps(api, None, local_dir, "/remote")
    elapsed = time.perf_counter() - started

    assert elapsed < 15
    local_only = {f"d{number}/local-only.txt" for number in range(folder_count)}
    assert set(local_map) - set(remote_map) == local_only
    assert not set(remote_map) - set(local_map)
    for number in range(folder_count):
        rel = f"d{number}/local-only.txt"
        assert local_map[rel][6] == f"/remote/d{number}/local-only.txt"
