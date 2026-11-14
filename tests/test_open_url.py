"""Tests for the open_url bridge: only the JDE-Projects website may be opened
in the system browser. webbrowser.open is replaced, so nothing is launched."""
import webbrowser

import pytest

from app.api import Api
from app.services import updates


ALLOWED = [
    "https://jde-projects.com",
    "https://jde-projects.com/",
    "https://jde-projects.com/support/#commercial-licensing",
]

REFUSED = [
    "http://jde-projects.com",
    "https://evil.example",
    "https://jde-projects.com.evil.example",
    "https://evil.example/jde-projects.com",
    "https://jde-projects.com@evil.example",
    "https://user@jde-projects.com",
    "https://jde-projects.com:8443/",
    "https://sub.jde-projects.com",
    "https://github.com/JDE-Projects/Simple-SFTP-Client/blob/main/README.md#updating",
    "file:///C:/Windows/System32/calc.exe",
    "javascript:alert(1)",
    "jde-projects.com",
    "https://jde-projects.com/\nhttps://evil.example",
    " https://jde-projects.com",
    "",
    None,
    123,
]


@pytest.fixture
def opened(monkeypatch):
    calls = []
    monkeypatch.setattr(webbrowser, "open", calls.append)
    return calls


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(updates, "log_event",
                        lambda event, **fields: lines.append((event, fields)))
    return lines


@pytest.mark.parametrize("url", ALLOWED)
def test_is_allowed_url_accepts_site(url):
    assert updates._is_allowed_url(url) is True


@pytest.mark.parametrize("url", REFUSED)
def test_is_allowed_url_refuses_everything_else(url):
    assert updates._is_allowed_url(url) is False


@pytest.mark.parametrize("url", ALLOWED)
def test_open_url_opens_allowed_site(url, opened, logged):
    assert Api("test").open_url(url) == {"ok": True}
    assert opened == [url]
    assert logged == []


@pytest.mark.parametrize("url", REFUSED)
def test_open_url_refuses_without_opening(url, opened, logged):
    result = Api("test").open_url(url)
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"]
    assert opened == []


def test_refusal_writes_a_log_line_without_the_address(opened, logged):
    Api("test").open_url("https://evil.example/secret-path")
    assert len(logged) == 1
    assert logged[0][0] == "open url refused"
