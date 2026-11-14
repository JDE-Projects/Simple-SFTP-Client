"""Keep direct debug-file writes inside the dedicated gateway modules."""

from pathlib import Path


def test_app_code_has_no_direct_debug_writes_or_tracebacks():
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "app").rglob("*.py")) + [root / "simple_sftp_client.py"]
    offenders = []
    for path in paths:
        if path.name in {"debug.py", "debug_log.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "debug.log(" in text or "debug._log(" in text or "traceback.format_exc(" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, offenders
