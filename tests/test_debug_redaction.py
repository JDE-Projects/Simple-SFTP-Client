"""
The debug log is off by default and, when turned on, is the single
chokepoint every message funnels through (including paramiko's own
protocol logging). This covers the pattern-based scrub applied there: it
masks passwords embedded in URLs and any private-key material, and leaves
everything else untouched.
"""
from simple_sftp_client import DebugLog, _scrub


def test_scrub_masks_sftp_url_password():
    text = "connecting to sftp://bob:hunter2@example.com"
    result = _scrub(text)
    assert "hunter2" not in result
    assert "sftp://bob:[redacted]@example.com" in result


def test_scrub_masks_https_url_password():
    text = "fetching https://alice:s3cret@files.example.com/path"
    result = _scrub(text)
    assert "s3cret" not in result
    assert "https://alice:[redacted]@files.example.com/path" in result


def test_scrub_leaves_credential_free_url_untouched():
    text = "scheme://host.example.com/some/path"
    assert _scrub(text) == text


def test_scrub_replaces_private_key_block():
    key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAK...\nmore key data\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = f"loaded key:\n{key}\ndone"
    result = _scrub(text)
    assert "MIIBOgIBAAJBAK" not in result
    assert "[redacted]" in result
    assert "-----BEGIN" not in result
    assert "-----END" not in result


def test_scrub_replaces_two_private_key_blocks():
    block = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "abcdef\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    text = f"first:\n{block}\nsecond:\n{block}\n"
    result = _scrub(text)
    assert result.count("[redacted]") == 2
    assert "abcdef" not in result


def test_scrub_passes_through_ordinary_text_unchanged():
    text = "connected to host, transferring file.txt (1024 bytes)"
    assert _scrub(text) == text


def test_debug_log_off_by_default():
    dbg = DebugLog()
    assert dbg.is_enabled() is False


def test_debug_log_writes_nothing_when_off(tmp_path):
    dbg = DebugLog()
    # Off by default, so log() must be a no-op even if a path were ever set.
    dbg.log("some label", "some content")
    assert dbg._path is None
