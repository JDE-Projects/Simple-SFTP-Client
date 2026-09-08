"""
Saved sessions (servers.json) must never reach the UI or a connect attempt
unvalidated. _load_sessions() drops any entry with the wrong shape - a bad
root, a sessions collection that isn't a list, an entry that isn't a dict,
a field of the wrong type, an unsupported auth value, or an invalid port -
instead of handing it through. Dropped entries never rewrite servers.json:
the file is left exactly as it was found either way. A dropped entry also
surfaces a one-line notice and a debug-log trace (both go through
Api._vlog, same as every other load-time notice in this app).

The port/auth fields used to be interpolated straight into the session
manager's innerHTML in the UI. Rendering isn't unit-testable from this
pytest suite (no browser), so the malicious-content cases here prove the
data path: a hostile port or auth value never survives validation to reach
that render in the first place. The DOM-node rendering itself is a manual
smoke-test item, not covered here.
"""
import json

import pytest

import simple_sftp_client as app


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SESSIONS_FILE", str(tmp_path / "servers.json"))
    return app.Api()


def _write_raw(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def base_entry(**overrides):
    e = {"name": "srv", "host": "example.com", "port": "22", "username": "alice",
         "auth": "password", "key_path": "", "start_path": "", "remember": False}
    e.update(overrides)
    return e


def test_valid_entry_passes_through(api):
    entry = base_entry()
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [entry]}))

    assert api._load_sessions() == [entry]


def test_root_not_a_dict_is_ignored_and_file_left_untouched(api):
    original = json.dumps(["not", "a", "dict"])
    _write_raw(app.SESSIONS_FILE, original)

    result = api._load_sessions()

    assert result == []
    with open(app.SESSIONS_FILE, encoding="utf-8") as f:
        assert f.read() == original


def test_sessions_not_a_list_is_ignored_and_file_left_untouched(api):
    original = json.dumps({"sessions": {"oops": "not a list"}})
    _write_raw(app.SESSIONS_FILE, original)

    result = api._load_sessions()

    assert result == []
    with open(app.SESSIONS_FILE, encoding="utf-8") as f:
        assert f.read() == original


def test_entry_not_a_dict_is_dropped_valid_ones_kept(api):
    good = base_entry(name="good")
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [good, "not a dict", 42, None, []]}))

    assert api._load_sessions() == [good]


@pytest.mark.parametrize("field,bad_value", [
    ("host", 12345),
    ("username", ["alice"]),
    ("key_path", {"path": "x"}),
    ("start_path", 1.5),
    ("remember", "true"),
    ("name", 5),
    ("name", ""),
    ("name", "   "),
])
def test_wrong_field_type_drops_entry(api, field, bad_value):
    entry = base_entry(**{field: bad_value})
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [entry]}))

    assert api._load_sessions() == []


@pytest.mark.parametrize("auth", ["", "ssh-agent", "PASSWORD", "<script>alert(1)</script>", None, 1])
def test_unsupported_auth_values_are_dropped(api, auth):
    entry = base_entry(auth=auth)
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [entry]}))

    assert api._load_sessions() == []


@pytest.mark.parametrize("port", [
    "0", "65536", "-1", "abc", "22 ",
    "1;DROP TABLE sessions",
    "<img src=x onerror=alert(1)>",
    "22\"><script>alert(1)</script>",
])
def test_malicious_or_invalid_port_content_is_dropped(api, port):
    entry = base_entry(port=port)
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [entry]}))

    assert api._load_sessions() == []


def test_blank_port_is_valid(api):
    entry = base_entry(port="")
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [entry]}))

    assert api._load_sessions() == [entry]


def test_malformed_entries_leave_sessions_file_untouched(api):
    original = json.dumps({"sessions": [base_entry(auth="bogus"), base_entry(port="abc")]})
    _write_raw(app.SESSIONS_FILE, original)

    api._load_sessions()

    with open(app.SESSIONS_FILE, encoding="utf-8") as f:
        assert f.read() == original


def test_dropped_entries_surface_visible_notice_and_debug_trace(api, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_vlog", lambda msg, level="info": calls.append((msg, level)))
    _write_raw(app.SESSIONS_FILE,
               json.dumps({"sessions": [base_entry(auth="bogus"), base_entry(name="ok")]}))

    result = api._load_sessions()

    assert result == [base_entry(name="ok")]
    # one visible+logged notice, at warn level, mentioning the drop count
    assert len(calls) == 1
    msg, level = calls[0]
    assert "1" in msg
    assert level == "warn"


def test_no_notice_when_nothing_dropped(api, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_vlog", lambda msg, level="info": calls.append((msg, level)))
    _write_raw(app.SESSIONS_FILE, json.dumps({"sessions": [base_entry()]}))

    api._load_sessions()

    assert calls == []


def test_corrupt_json_still_preserved_aside_as_before(api, tmp_path):
    # Unchanged behavior: a file that isn't valid JSON at all goes through
    # _preserve_corrupt, not the new entry-shape validation.
    _write_raw(app.SESSIONS_FILE, "{not json")

    result = api._load_sessions()

    assert result == []
    import os
    assert not os.path.exists(app.SESSIONS_FILE)
