import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT_PLATE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}|\d{2})[\s\-\.]?([A-Z]{2}|\d{2})[\s\-\.]?([A-Z]{2}|\d{2})(?![A-Z0-9])")


def strip_accents(s):
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(ch))


def norm_text(s):
    return re.sub(r"\s+", " ", strip_accents(s or "").lower()).strip()


def norm_plate(s):
    return re.sub(r"[^A-Z0-9]", "", strip_accents(s or "").upper())


def guess_plate(*texts):
    """Procura uma matrícula portuguesa (AA-00-BB, 00-AA-00, 00-00-AA, AA-00-00...)."""
    for t in texts:
        if not t:
            continue
        for m in PT_PLATE.finditer(strip_accents(str(t)).upper()):
            parts = m.groups()
            if any(p.isalpha() for p in parts) and any(p.isdigit() for p in parts):
                return "".join(parts)
    return None


def fmt_plate(p):
    p = norm_plate(p)
    return f"{p[0:2]}-{p[2:4]}-{p[4:6]}" if len(p) == 6 else p


def to_utc(dt, tz_name):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(s):
    return datetime.fromisoformat(s) if s else None


def local_str(s_or_dt, tz_name, fmt="%d/%m/%Y %H:%M"):
    if not s_or_dt:
        return ""
    dt = from_iso(s_or_dt) if isinstance(s_or_dt, str) else s_or_dt
    return dt.astimezone(ZoneInfo(tz_name)).strftime(fmt)


def now_iso():
    return datetime.now(timezone.utc).isoformat()
