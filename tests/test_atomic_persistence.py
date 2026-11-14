"""
Prefs, sessions, and known_hosts persistence must be atomic (never leave a
half-written file behind) and loud (a corrupt or unreadable file is preserved
and logged, never silently discarded or, for known_hosts, silently treated as
empty). These tests point every file path at tmp_path via monkeypatch, so
none of them ever touches a real app file.
"""
import glob
import json
import os
import tempfile

import paramiko
import pytest

from app import paths, prefs
from app.api import Api
from app.errors import KnownHostsUnreadable
from app.hostkeys import load_known_hosts
from app.prefs import load_prefs, save_prefs
from simple_sftp_client import APP_VERSION


# ───────────── prefs ─────────────

@pytest.fixture
def pref_path(tmp_path, monkeypatch):
    path = str(tmp_path / "simple_sftp_client.pref")
    monkeypatch.setattr(prefs, "_pref_path", lambda: path)
    return path


def test_load_prefs_corrupt_file_preserved_aside(pref_path):
    with open(pref_path, "w", encoding="utf-8") as f:
        f.write("not valid json{{{")

    result = load_prefs()

    assert result == {}
    assert not os.path.exists(pref_path)
    leftovers = glob.glob(pref_path + ".corrupt-*")
    assert len(leftovers) == 1
    with open(leftovers[0], encoding="utf-8") as f:
        assert f.read() == "not valid json{{{"


def test_load_prefs_locked_file_left_in_place(pref_path, monkeypatch):
    with open(pref_path, "w", encoding="utf-8") as f:
        json.dump({"theme": "light"}, f)
    real_open = open
    logged = []

    def locked_open(file, *a, **k):
        if file == pref_path:
            raise PermissionError(13, "Permission denied", pref_path)
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", locked_open)
    monkeypatch.setattr(prefs, "log_event", lambda event, **fields: logged.append((event, fields)))

    result = load_prefs()

    monkeypatch.undo()
    assert result == {}
    assert glob.glob(pref_path + ".corrupt-*") == []
    with open(pref_path, encoding="utf-8") as f:
        assert json.load(f) == {"theme": "light"}
    assert logged[0][0] == "prefs unreadable"
    assert isinstance(logged[0][1]["error"], PermissionError)


def test_load_prefs_kept_aside_log_line_has_no_path(pref_path, monkeypatch):
    from app import atomic
    logged = []
    monkeypatch.setattr(atomic, "log_event", lambda event, **fields: logged.append((event, fields)))
    with open(pref_path, "w", encoding="utf-8") as f:
        f.write("not valid json{{{")

    load_prefs()

    assert len(logged) == 1
    assert logged[0][0] == "atomic unreadable"


def test_save_prefs_missing_file_is_first_run(pref_path):
    assert load_prefs() == {}


def test_save_prefs_write_failure_returns_false_and_leaves_no_final_file(pref_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    ok = save_prefs({"theme": "dark"})

    assert ok is False
    assert not os.path.exists(pref_path)
    # the temp file must be cleaned up too
    assert glob.glob(os.path.join(os.path.dirname(pref_path), ".tmp_*")) == []


def test_save_prefs_mkstemp_failure_returns_false(pref_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", boom)

    assert save_prefs({"theme": "dark"}) is False
    assert not os.path.exists(pref_path)


def test_save_prefs_round_trips(pref_path):
    assert save_prefs({"theme": "light"}) is True
    assert load_prefs() == {"theme": "light"}


# ───────────── sessions ─────────────

@pytest.fixture
def sessions_path(tmp_path, monkeypatch):
    path = str(tmp_path / "servers.json")
    monkeypatch.setattr(paths, "SESSIONS_FILE", path)
    return path


def test_load_sessions_corrupt_file_preserved_aside(sessions_path):
    with open(sessions_path, "w", encoding="utf-8") as f:
        f.write("{not json")

    api = Api(APP_VERSION)
    result = api._load_sessions()

    assert result == []
    assert not os.path.exists(sessions_path)
    assert len(glob.glob(sessions_path + ".corrupt-*")) == 1


SAVED = {"sessions": [{"name": "prod", "host": "example.com", "port": 22,
                       "username": "u", "auth": "password", "key_path": "",
                       "start_path": "", "remember": False}]}


def _write_saved_sessions(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(SAVED, f)


def _lock_file(monkeypatch, locked_path):
    real_open = open

    def locked_open(file, *a, **k):
        if file == locked_path:
            raise PermissionError(13, "Permission denied", locked_path)
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", locked_open)


def _assert_sessions_untouched(path):
    assert glob.glob(path + ".corrupt-*") == []
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == SAVED


def test_load_sessions_locked_file_left_in_place_with_notice(sessions_path, monkeypatch):
    _write_saved_sessions(sessions_path)
    api = Api(APP_VERSION)
    notices = []
    api._sessions_notice = notices.append
    _lock_file(monkeypatch, sessions_path)

    result = api._load_sessions()

    monkeypatch.undo()
    assert result == []
    _assert_sessions_untouched(sessions_path)
    assert len(notices) == 1
    assert "Permission denied" in notices[0]
    assert sessions_path not in notices[0]


def test_save_session_refused_when_file_locked(sessions_path, monkeypatch):
    _write_saved_sessions(sessions_path)
    api = Api(APP_VERSION)
    _lock_file(monkeypatch, sessions_path)

    result = api.save_session({"name": "new", "host": "other.example.com",
                               "port": "22", "username": "u",
                               "auth": "password"})

    monkeypatch.undo()
    assert result["ok"] is False
    assert "Nothing was changed" in result["error"]
    _assert_sessions_untouched(sessions_path)


def test_delete_session_refused_when_file_locked(sessions_path, monkeypatch):
    _write_saved_sessions(sessions_path)
    api = Api(APP_VERSION)
    _lock_file(monkeypatch, sessions_path)

    result = api.delete_session("prod")

    monkeypatch.undo()
    assert result["ok"] is False
    assert "Nothing was changed" in result["error"]
    _assert_sessions_untouched(sessions_path)


def test_load_sessions_missing_file_is_empty_not_corrupt(sessions_path):
    api = Api(APP_VERSION)
    assert api._load_sessions() == []
    assert glob.glob(sessions_path + ".corrupt-*") == []


def test_save_sessions_write_failure_returns_false_and_leaves_no_final_file(sessions_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    api = Api(APP_VERSION)
    ok = api._save_sessions([{"name": "x"}])

    assert ok is False
    assert not os.path.exists(sessions_path)
    assert glob.glob(os.path.join(os.path.dirname(sessions_path), ".tmp_*")) == []


def test_save_sessions_mkstemp_failure_returns_false(sessions_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", boom)

    api = Api(APP_VERSION)
    assert api._save_sessions([{"name": "x"}]) is False
    assert not os.path.exists(sessions_path)


def test_save_sessions_round_trips(sessions_path):
    api = Api(APP_VERSION)
    assert api._save_sessions([{"name": "x"}]) is True
    with open(sessions_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["sessions"] == [{"name": "x"}]


# ───────────── known_hosts ─────────────

@pytest.fixture
def known_hosts_path(tmp_path, monkeypatch):
    path = str(tmp_path / "known_hosts")
    monkeypatch.setattr(paths, "KNOWN_HOSTS_FILE", path)
    return path


def _valid_line(host="example.com"):
    key = paramiko.RSAKey.generate(1024)
    return f"{host} {key.get_name()} {key.get_base64()}\n"


def test_load_known_hosts_missing_file_is_clean_first_contact(known_hosts_path):
    hk = load_known_hosts()
    assert len(hk) == 0


def test_load_known_hosts_all_valid_lines_loads_fine(known_hosts_path):
    with open(known_hosts_path, "w", encoding="utf-8") as f:
        f.write(_valid_line("a.example.com"))
        f.write(_valid_line("b.example.com"))

    hk = load_known_hosts()
    assert hk.lookup("a.example.com") is not None
    assert hk.lookup("b.example.com") is not None


def test_load_known_hosts_one_bad_line_among_valid_raises_strictly(known_hosts_path):
    # Paramiko's own loader silently skips a line it can't parse, which would
    # make a corrupt file look like it just has fewer entries. The strict
    # check must treat one bad line among good ones as corruption.
    with open(known_hosts_path, "w", encoding="utf-8") as f:
        f.write(_valid_line("a.example.com"))
        f.write("this is not a valid known_hosts line\n")

    with pytest.raises(KnownHostsUnreadable):
        load_known_hosts()


def test_load_known_hosts_blank_lines_and_comments_are_fine(known_hosts_path):
    with open(known_hosts_path, "w", encoding="utf-8") as f:
        f.write("# a comment\n\n")
        f.write(_valid_line("a.example.com"))

    hk = load_known_hosts()
    assert hk.lookup("a.example.com") is not None


def test_connect_refuses_when_known_hosts_unreadable(known_hosts_path, monkeypatch):
    with open(known_hosts_path, "w", encoding="utf-8") as f:
        f.write("garbage garbage garbage\n")

    api = Api(APP_VERSION)
    result = api.connect({"host": "example.com", "username": "u", "password": "p"})

    assert result["ok"] is False
    assert "host_key_unknown" not in result
    assert "host_key_changed" not in result
    assert known_hosts_path in result["error"]


def test_trust_host_key_on_corrupt_file_leaves_it_untouched(known_hosts_path):
    original = "garbage garbage garbage\n"
    with open(known_hosts_path, "w", encoding="utf-8") as f:
        f.write(original)

    api = Api(APP_VERSION)
    key = paramiko.RSAKey.generate(1024)
    api._pending_host_key = ("example.com", key)

    result = api.trust_host_key()

    assert result["ok"] is False
    with open(known_hosts_path, encoding="utf-8") as f:
        assert f.read() == original


def test_get_host_key_on_corrupt_file_reports_unreadable(known_hosts_path):
    with open(known_hosts_path, "w", encoding="utf-8") as f:
        f.write("garbage garbage garbage\n")

    api = Api(APP_VERSION)
    result = api.get_host_key("example.com")

    assert result["known"] is False
    assert result.get("unreadable") is True


def test_trust_host_key_succeeds_on_empty_file_and_key_is_present(known_hosts_path):
    api = Api(APP_VERSION)
    key = paramiko.RSAKey.generate(1024)
    api._pending_host_key = ("example.com", key)

    result = api.trust_host_key()

    assert result["ok"] is True
    hk = load_known_hosts()
    assert hk.lookup("example.com") is not None


def test_trust_host_key_replace_failure_leaves_prior_file_intact(known_hosts_path, monkeypatch):
    api = Api(APP_VERSION)
    first_key = paramiko.RSAKey.generate(1024)
    api._pending_host_key = ("example.com", first_key)
    assert api.trust_host_key()["ok"] is True
    with open(known_hosts_path, encoding="utf-8") as f:
        before = f.read()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    second_key = paramiko.RSAKey.generate(1024)
    api._pending_host_key = ("other.example.com", second_key)
    result = api.trust_host_key()

    assert result["ok"] is False
    with open(known_hosts_path, encoding="utf-8") as f:
        assert f.read() == before
