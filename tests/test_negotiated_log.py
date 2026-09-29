"""
The connect log's "Negotiated:" line lists only algorithms the connection
actually reports. Key exchange is never listed: paramiko discards the agreed
method once the handshake ends, so the line used to read "kex None".
"""
from app.api import Api
from app.formatting import negotiated_summary


class FakeTransport:
    remote_cipher = "aes256-ctr"
    remote_mac = "hmac-sha2-256"
    kex_engine = None  # what paramiko leaves behind after the handshake


class FakeClient:
    def get_transport(self):
        return FakeTransport()


def test_transport_info_reports_cipher_and_mac_only():
    ti = Api._transport_info(object.__new__(Api), FakeClient())
    assert ti == {"cipher": "aes256-ctr", "mac": "hmac-sha2-256"}


def test_summary_never_reads_none_or_kex():
    ti = Api._transport_info(object.__new__(Api), FakeClient())
    line = negotiated_summary(ti)
    assert line == "cipher aes256-ctr · mac hmac-sha2-256"
    assert "None" not in line
    assert "kex" not in line
    assert "?" not in line


def test_summary_skips_missing_or_empty_fields():
    assert negotiated_summary({"cipher": "aes256-ctr", "mac": None}) == "cipher aes256-ctr"
    assert negotiated_summary({"cipher": "", "mac": "hmac-sha2-256"}) == "mac hmac-sha2-256"


def test_summary_empty_when_nothing_known():
    assert negotiated_summary({}) == ""
    assert negotiated_summary(None) == ""


def test_transport_info_empty_when_transport_unavailable():
    class Broken:
        def get_transport(self):
            raise EOFError("gone")

    assert Api._transport_info(object.__new__(Api), Broken()) == {}
