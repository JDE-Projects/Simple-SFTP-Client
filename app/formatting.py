from datetime import datetime


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}")
        n /= 1024


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def negotiated_summary(ti: dict) -> str:
    """Format the connect log's negotiated algorithms, e.g. "cipher X · mac Y".
    Lists only fields that have a real value; returns "" when none do."""
    return " · ".join(f"{k} {ti[k]}" for k in ("cipher", "mac") if ti and ti.get(k))

