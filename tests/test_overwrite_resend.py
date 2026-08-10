"""
Tests that on_conflict="overwrite" actually resends a file, even when the copy
on the other side is the same size. Size alone does not prove the contents
match, so overwrite must not skip. on_conflict="skip" keeps skipping.

Runs against the in-process SFTP server from conftest.py.
"""
from transfer_queue import COMPLETED, SKIPPED


def _upload_one(api, local_dir, name, on_conflict):
    result = api.enqueue([{"name": name, "is_dir": False}], "upload",
                          str(local_dir), "/", on_conflict)
    assert result["ok"] is True
    return api.queue.snapshot()[-1]["id"]


def test_overwrite_resends_same_size_changed_file(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "same_size.bin"
    original = b"A" * 4096
    changed = b"B" * 4096  # identical size, different bytes

    src = local_dir / name
    src.write_bytes(original)
    first_id = _upload_one(api, local_dir, name, "overwrite")
    wait_for_drain(api)
    assert state_of(api, first_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == original

    # change the local bytes without changing the size, then overwrite again
    src.write_bytes(changed)
    second_id = _upload_one(api, local_dir, name, "overwrite")
    wait_for_drain(api)

    assert state_of(api, second_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == changed


def test_skip_leaves_same_size_file_untouched(sftp_env, wait_for_drain, state_of):
    api, server_root, local_dir = sftp_env
    name = "same_size.bin"
    original = b"A" * 4096
    changed = b"B" * 4096

    src = local_dir / name
    src.write_bytes(original)
    first_id = _upload_one(api, local_dir, name, "skip")
    wait_for_drain(api)
    assert state_of(api, first_id)["state"] == COMPLETED
    assert (server_root / name).read_bytes() == original

    # same size on both sides + skip -> the changed local file is not sent
    src.write_bytes(changed)
    second_id = _upload_one(api, local_dir, name, "skip")
    wait_for_drain(api)

    assert state_of(api, second_id)["state"] == SKIPPED
    assert (server_root / name).read_bytes() == original
