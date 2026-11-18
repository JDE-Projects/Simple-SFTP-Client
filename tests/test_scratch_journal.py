"""Tests for durable local download scratch-file ownership records."""

import json
import os
import threading

import pytest

from app import atomic, paths, scratch_journal


@pytest.fixture
def journal_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SCRATCH_JOURNAL_FILE", str(tmp_path / "scratch-journal.json"))
    monkeypatch.setattr(paths, "SCRATCH_LOCK_FILE", str(tmp_path / "scratch-journal.lock"))
    return tmp_path


def _paths(folder, name="final.txt", token="deadbeef"):
    final = folder / name
    return str(folder / f".{name}.{token}.sxtpart"), str(final)


def test_atomic_write_non_durable_skips_fsync(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: calls.append(fd))
    path = tmp_path / "data.json"

    assert atomic._atomic_write_json(str(path), {"one": 1}, durable=False)
    assert calls == []
    assert json.loads(path.read_text(encoding="utf-8")) == {"one": 1}

    assert atomic._atomic_write_json(str(path), {"two": 2})
    assert len(calls) == 1


def test_register_writes_durably_before_return(journal_paths):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    scratch, final = _paths(journal_paths)
    journal.register(scratch, final)

    assert json.loads((journal_paths / "scratch-journal.json").read_text()) == [
        {"scratch": os.path.abspath(scratch), "final": os.path.abspath(final), "identity": None}]
    journal.close()


def test_register_rejects_non_sibling(journal_paths):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    scratch, _final = _paths(journal_paths / "other")
    with pytest.raises(ValueError):
        journal.register(scratch, str(journal_paths / "final.txt"))
    assert not (journal_paths / "scratch-journal.json").exists()
    journal.close()


@pytest.mark.parametrize("scratch_name", [
    ".final.txt.deadbee.sxtpart", ".final.txt.DEADBEEF.sxtpart",
    ".final.txt.deadbeef.part", ".other.txt.deadbeef.sxtpart",
])
def test_register_rejects_wrong_name_shape(journal_paths, scratch_name):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    with pytest.raises(ValueError):
        journal.register(str(journal_paths / scratch_name), str(journal_paths / "final.txt"))
    journal.close()


def test_register_failure_rolls_back(journal_paths, monkeypatch):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    scratch, final = _paths(journal_paths)
    monkeypatch.setattr(scratch_journal, "_atomic_write_json", lambda *args, **kwargs: False)
    with pytest.raises(OSError, match="Could not record the download scratch file"):
        journal.register(scratch, final)
    assert not journal.owns(scratch)
    journal.close()


def test_retire_and_identity_persist(journal_paths):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    first, final = _paths(journal_paths, "one.txt")
    second, second_final = _paths(journal_paths, "two.txt", "cafebabe")
    journal.register(first, final)
    journal.register(second, second_final)
    journal.set_identity(first, (11, 22))
    journal.retire(second)
    journal.close()

    loaded = scratch_journal.ScratchJournal()
    assert loaded.open()
    assert loaded.entries() == [{"scratch": os.path.abspath(first),
                                 "final": os.path.abspath(final),
                                 "identity": [11, 22]}]
    loaded.close()


def test_owns_matches_case_and_separators(journal_paths):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    scratch, final = _paths(journal_paths, "Case.txt")
    slash_scratch = scratch.replace("\\", "/")
    journal.register(slash_scratch, final.replace("\\", "/"))
    lookup = "\\\\?\\" + scratch.swapcase()
    assert journal.owns(lookup)
    journal.close()


def test_second_open_is_refused(journal_paths):
    first = scratch_journal.ScratchJournal()
    second = scratch_journal.ScratchJournal()
    assert first.open()
    assert not second.open()
    first.close()
    assert second.open()
    second.close()


def test_malformed_journal_preserved(journal_paths):
    journal_path = journal_paths / "scratch-journal.json"
    journal_path.write_text("{bad", encoding="utf-8")
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    assert journal.entries() == []
    assert not journal_path.exists()
    assert len(list(journal_paths.glob("scratch-journal.json.corrupt-*"))) == 1
    journal.close()


def test_malformed_journal_not_preserved_refuses(journal_paths, monkeypatch):
    journal_path = journal_paths / "scratch-journal.json"
    original = b"{bad"
    journal_path.write_bytes(original)
    monkeypatch.setattr(scratch_journal, "_preserve_corrupt", lambda *args: None)
    journal = scratch_journal.ScratchJournal()
    assert not journal.open()
    assert journal_path.read_bytes() == original


def test_existing_entries_kept_on_open(journal_paths):
    scratch, final = _paths(journal_paths)
    raw = [
        {"scratch": os.path.abspath(scratch), "final": os.path.abspath(final), "identity": None},
        {"scratch": "not-absolute", "final": os.path.abspath(final), "identity": None},
    ]
    journal_path = journal_paths / "scratch-journal.json"
    journal_path.write_text(json.dumps(raw), encoding="utf-8")
    before = journal_path.read_bytes()
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    assert len(journal.entries()) == 2
    assert journal.owns(scratch)
    assert not journal.owns("not-absolute")
    assert journal_path.read_bytes() == before
    journal.close()


def test_concurrent_register_retire(journal_paths):
    journal = scratch_journal.ScratchJournal()
    assert journal.open()
    errors = []

    def worker(number):
        for index in range(50):
            scratch, final = _paths(journal_paths, f"{number}-{index}.txt", f"{index:08x}")
            try:
                journal.register(scratch, final)
                journal.retire(scratch)
            except OSError as error:
                errors.append(error)

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert json.loads((journal_paths / "scratch-journal.json").read_text()) == []
    journal.close()

    loaded = scratch_journal.ScratchJournal()
    assert loaded.open()
    assert loaded.entries() == []
    loaded.close()
