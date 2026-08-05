"""
Tests for _update_error_reason in simple_sftp_client.py.

Pure and network-free: every exception is constructed directly and passed to
the helper, no urlopen and no sockets are touched.
"""
import errno
import json
import socket
import ssl
import urllib.error

import simple_sftp_client as app


def test_http_error_403():
    exc = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
    reason = app._update_error_reason(exc)
    assert "rate-limiting" in reason


def test_http_error_404():
    exc = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    reason = app._update_error_reason(exc)
    assert reason == "No published release was found."


def test_http_error_500():
    exc = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
    reason = app._update_error_reason(exc)
    assert "HTTP 500" in reason
    assert "trouble on its end" in reason


def test_http_error_other_code():
    exc = urllib.error.HTTPError("url", 418, "I'm a teapot", {}, None)
    reason = app._update_error_reason(exc)
    assert reason == "GitHub returned an error (HTTP 418)."


def test_json_decode_error():
    try:
        json.loads("not json")
    except json.JSONDecodeError as exc:
        reason = app._update_error_reason(exc)
    assert "unexpected" in reason


def test_url_error_ssl_cert_verification():
    cause = ssl.SSLCertVerificationError("certificate verify failed")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert "certificate could not be verified" in reason


def test_url_error_ssl_eof():
    cause = ssl.SSLEOFError("EOF occurred in violation of protocol")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert reason == "The secure connection was cut off during the handshake with GitHub."


def test_url_error_ssl_generic():
    cause = ssl.SSLError("some other TLS failure")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert reason == "The secure connection to GitHub failed."


def test_url_error_gaierror():
    cause = socket.gaierror("Name or service not known")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert "could not be looked up" in reason


def test_url_error_timeout():
    cause = socket.timeout("timed out")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert reason == "GitHub didn't respond in time."


def test_url_error_connection_refused():
    cause = ConnectionRefusedError("connection refused")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert "refused or reset" in reason


def test_url_error_network_unreachable():
    cause = OSError()
    cause.errno = errno.ENETUNREACH
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert reason == "No network connection."


def test_url_error_generic_fallback():
    cause = OSError("some unclassified socket failure")
    exc = urllib.error.URLError(cause)
    reason = app._update_error_reason(exc)
    assert reason == "Couldn't reach GitHub. Check the internet connection."


def test_unknown_exception_fallback():
    exc = ValueError("something odd happened")
    reason = app._update_error_reason(exc)
    assert reason == "ValueError: something odd happened"


def test_unknown_exception_fallback_truncated():
    exc = ValueError("x" * 200)
    reason = app._update_error_reason(exc)
    assert len(reason) <= 120
    assert reason.endswith("...")
