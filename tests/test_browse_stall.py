"""Coverage for bounded file-pane SFTP sessions."""

import socket
import threading
import time

import pytest
from paramiko.sftp import CMD_CLOSE

from app import constants
from app.api import Api
from app.services import browse_session
from app.services.browse_session import (
    BrowsingSFTPClient,
    open_browsing_sftp,
    reset_browsing_session,
)
from simple_sftp_client import APP_VERSION


@pytest.fixture
def browsing_env(sftp_env):
    api, root, local = sftp_env
    old = api._sftp
    api._sftp = open_browsing_sftp(api._client)
    old.close()
    try:
        yield api, root, local
    finally:
        api._sftp.close()


class FakeChannel:
    def __init__(self, replies=(), delays=()):
        self.replies = list(replies)
        self.delays = list(delays)
        self.timeout = None
        self.closed = False
        self.sends = []

    def settimeout(self, value):
        self.timeout = value

    def recv(self, _size):
        delay = self.delays.pop(0) if self.delays else 0
        if delay > self.timeout:
            raise TimeoutError()
        time.sleep(delay)
        return self.replies.pop(0)

    def send(self, data):
        self.sends.append(data)
        raise TimeoutError()

    def close(self):
        self.closed = True


def bare_client(channel):
    client = object.__new__(BrowsingSFTPClient)
    client.sock = channel
    client.timed_out = False
    client._invalid = False
    client.ultra_debug = False
    return client


def test_listing_stall_resets_session(browsing_env, monkeypatch):
    api, root, _ = browsing_env
    (root / "normal").mkdir()
    old = api._sftp
    events = []
    vlog = api._vlog

    def record(message, level="info", event=None, **fields):
        events.append(event)
        return vlog(message, level, event, **fields)

    monkeypatch.setattr(api, "_vlog", record)

    def stalled(*_args):
        old.timed_out = True
        raise TimeoutError()

    monkeypatch.setattr(old, "normalize", stalled)
    result = api.list_remote("/stalled")
    assert result == {"ok": False, "error": "The server stopped answering, so the file pane's connection to it was reset. Try again."}
    assert api._sftp is not old
    assert api.list_remote("/normal")["ok"] is True
    assert "browse timeout" in events
    assert "browse reset" in events


def test_drip_fed_reply_times_out(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REPLY_TIMEOUT", 0.04)
    channel = FakeChannel([b"\x00\x00\x00\x03", b"e", b"xx"], [0, 0.025, 0.025])
    with pytest.raises(socket.timeout):
        bare_client(channel)._read_packet()


def test_send_timeout_invalidates_session(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REPLY_TIMEOUT", 0.04)
    channel = FakeChannel()
    client = bare_client(channel)
    with pytest.raises(socket.timeout):
        client._write_all(b"request")
    assert client.timed_out is True
    before = len(channel.sends)
    with pytest.raises(socket.timeout):
        client._write_all(b"again")
    assert len(channel.sends) == before


class OpenChannel:
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release
        self.closed = False

    def settimeout(self, _value):
        pass

    def invoke_subsystem(self, _name):
        self.entered.set()
        self.release.wait()

    def close(self):
        self.closed = True


class OpenTransport:
    def __init__(self, channel):
        self.channel = channel

    def open_session(self, timeout):
        assert timeout == constants.BROWSE_REOPEN_TIMEOUT
        return self.channel


class OpenClient:
    def __init__(self, channel):
        self.transport = OpenTransport(channel)

    def get_transport(self):
        return self.transport


class SimpleSession:
    def __init__(self, channel):
        self.channel = channel

    def close(self):
        self.channel.close()


def timed_api(client):
    api = Api(APP_VERSION)
    api._client = client
    api._sftp = type("TimedOut", (), {"timed_out": True})()
    api._connected = True
    return api


def test_reopen_timeout_marks_lost(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REOPEN_TIMEOUT", 0.03)
    entered, release = threading.Event(), threading.Event()
    channel = OpenChannel(entered, release)
    monkeypatch.setattr(browse_session, "BrowsingSFTPClient", SimpleSession)
    api = timed_api(OpenClient(channel))
    started = time.monotonic()
    assert reset_browsing_session(api, "listing") == "lost"
    assert time.monotonic() - started < 0.2
    assert api._connected is False
    release.set()
    assert entered.wait(0.2)
    time.sleep(0.02)
    assert channel.closed is True


def test_late_open_after_abandon_is_closed(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REOPEN_TIMEOUT", 0.03)
    entered, release = threading.Event(), threading.Event()
    channel = OpenChannel(entered, release)
    monkeypatch.setattr(browse_session, "BrowsingSFTPClient", SimpleSession)
    with pytest.raises(TimeoutError):
        open_browsing_sftp(OpenClient(channel))
    assert entered.wait(0.2)
    release.set()
    time.sleep(0.02)
    assert channel.closed is True


class HangUntilClosedChannel(OpenChannel):
    """Mimics paramiko: the subsystem request waits until the channel closes."""

    def invoke_subsystem(self, _name):
        self.entered.set()
        self.release.wait()
        if self.closed:
            raise EOFError("Channel closed.")

    def close(self):
        self.closed = True
        self.release.set()


def test_open_timeout_closes_stuck_channel(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REOPEN_TIMEOUT", 0.03)
    entered, release = threading.Event(), threading.Event()
    channel = HangUntilClosedChannel(entered, release)
    monkeypatch.setattr(browse_session, "BrowsingSFTPClient", SimpleSession)

    with pytest.raises(TimeoutError) as raised:
        open_browsing_sftp(OpenClient(channel))

    # The server never answers: only the give-up path can end the attempt.
    assert raised.value.opener_done.wait(0.5)
    assert channel.closed is True


class SlowOpenTransport(OpenTransport):
    def __init__(self, channel, release):
        super().__init__(channel)
        self.release = release

    def open_session(self, timeout):
        self.release.wait()
        return self.channel


def test_channel_opened_after_abandon_is_closed(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REOPEN_TIMEOUT", 0.03)
    entered, release = threading.Event(), threading.Event()
    channel = OpenChannel(entered, threading.Event())
    client = OpenClient(channel)
    client.transport = SlowOpenTransport(channel, release)

    with pytest.raises(TimeoutError) as raised:
        open_browsing_sftp(client)

    release.set()
    assert raised.value.opener_done.wait(0.5)
    assert channel.closed is True
    assert not entered.is_set()


def test_open_timeout_carries_opener_done(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REOPEN_TIMEOUT", 0.03)
    entered, release = threading.Event(), threading.Event()
    channel = OpenChannel(entered, release)
    monkeypatch.setattr(browse_session, "BrowsingSFTPClient", SimpleSession)

    with pytest.raises(TimeoutError) as raised:
        open_browsing_sftp(OpenClient(channel))

    opener_done = raised.value.opener_done
    assert isinstance(opener_done, threading.Event)
    assert entered.wait(0.2)
    assert not opener_done.is_set()
    release.set()
    assert opener_done.wait(0.2)


def test_disconnect_during_reset_publishes_nothing(monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REOPEN_TIMEOUT", 0.2)
    entered, release = threading.Event(), threading.Event()
    channel = OpenChannel(entered, release)
    monkeypatch.setattr(browse_session, "BrowsingSFTPClient", SimpleSession)
    api = timed_api(OpenClient(channel))
    original = api._sftp
    result = []
    thread = threading.Thread(target=lambda: result.append(reset_browsing_session(api, "listing")))
    thread.start()
    assert entered.wait(0.2)
    api._conn_generation += 1
    release.set()
    thread.join(0.2)
    assert result == ["stale"]
    assert api._sftp is original and api._connected is True
    assert channel.closed is True


def test_ping_after_reset_reports_ok(browsing_env, monkeypatch):
    api, _, _ = browsing_env
    old = api._sftp

    def stalled(_path):
        old.timed_out = True
        raise TimeoutError()

    monkeypatch.setattr(old, "stat", stalled)
    assert api.ping()["ok"] is True
    assert api._connected is True


def test_ping_reset_failure_reports_down(browsing_env, monkeypatch):
    api, _, _ = browsing_env
    old = api._sftp

    def stalled(_path):
        old.timed_out = True
        raise TimeoutError()

    monkeypatch.setattr(old, "stat", stalled)
    monkeypatch.setattr(browse_session, "open_browsing_sftp", lambda _client: (_ for _ in ()).throw(TimeoutError()))
    assert api.ping() == {"ok": False}
    assert api._connected is False


def test_lost_connection_does_not_retry_reopen(browsing_env, monkeypatch):
    api, _, _ = browsing_env
    old = api._sftp

    def stalled(_path):
        old.timed_out = True
        raise TimeoutError()

    opens = []

    def failing_open(_client):
        opens.append(1)
        raise TimeoutError()

    monkeypatch.setattr(old, "stat", stalled)
    monkeypatch.setattr(browse_session, "open_browsing_sftp", failing_open)
    assert api.ping() == {"ok": False}
    assert api.ping() == {"ok": False}
    assert api.list_remote("/") == {"ok": False, "error": "Not connected."}
    assert len(opens) == 1


def test_rename_stall_says_may_or_may_not(browsing_env, monkeypatch):
    api, _, _ = browsing_env
    old = api._sftp

    def stalled(*_args):
        old.timed_out = True
        raise TimeoutError()

    monkeypatch.setattr(old, "rename", stalled)
    result = api.rename("remote", "/", "old", "new")
    assert result == {"ok": False, "error": "The server stopped answering, so the file pane's connection to it was reset. It may or may not have finished. Refresh to check."}


def test_delete_stops_at_stalled_item(browsing_env, monkeypatch):
    api, root, _ = browsing_env
    for name in ("one", "two", "three", "four"):
        (root / name).write_text(name)
    old = api._sftp
    remove = old.remove

    def remove_second(path):
        if path.endswith("/two"):
            old.timed_out = True
            raise TimeoutError()
        return remove(path)

    monkeypatch.setattr(old, "remove", remove_second)
    items = [{"name": name, "is_dir": False} for name in ("one", "two", "three", "four")]
    result = api.delete("remote", "/", items)
    assert result["ok"] is True
    assert any("two: the server stopped answering; it may or may not be deleted" in error for error in result["errors"])
    assert "2 item(s) not attempted: three, four" in result["errors"]
    assert any("connection to it was reset" in error for error in result["errors"])
    # The stalled item and the two not attempted are the only failures, even
    # though the stall adds more messages than that.
    assert result["failed"] == 3
    assert len(result["errors"]) > result["failed"]
    assert not (root / "one").exists()
    assert (root / "three").exists() and (root / "four").exists()


def test_close_stall_after_full_read(browsing_env, monkeypatch):
    api, root, _ = browsing_env
    (root / "entry").write_text("x")
    old = api._sftp
    request = old._request

    def stall_close(command, *args):
        if command == CMD_CLOSE:
            old.timed_out = True
            raise TimeoutError()
        return request(command, *args)

    monkeypatch.setattr(old, "_request", stall_close)
    result = api.list_remote("/")
    assert result == {"ok": False, "error": "The server stopped answering, so the file pane's connection to it was reset. Try again."}


def test_slow_reply_under_deadline_untouched(browsing_env, monkeypatch):
    monkeypatch.setattr(constants, "BROWSE_REPLY_TIMEOUT", 0.2)
    api, root, _ = browsing_env
    (root / "entry").write_text("x")
    old = api._sftp
    normalize = old.normalize

    def slow(path):
        time.sleep(0.02)
        return normalize(path)

    monkeypatch.setattr(old, "normalize", slow)
    assert api.list_remote("/")["ok"] is True
    assert api._sftp is old


def test_watch_upload_stall_leaves_the_file_pane_session(
        browsing_env, monkeypatch, wait_until):
    api, _, local = browsing_env
    api._watch_interval = 0.01
    watched = local / "watch.txt"
    watched.write_text("first")
    assert api.start_watch(str(local), "/")["ok"] is True
    old = api._sftp
    attempts = []

    def stalled(session, *_args):
        attempts.append(session)
        session.timed_out = True
        raise TimeoutError()

    monkeypatch.setattr(api, "_put_file", stalled)
    watched.write_text("changed")
    try:
        wait_until(lambda: len(attempts) >= 2, timeout=2)
    finally:
        api.stop_watch()
    assert api._sftp is old
    assert not api._sftp.timed_out
    assert len({id(session) for session in attempts}) >= 2
