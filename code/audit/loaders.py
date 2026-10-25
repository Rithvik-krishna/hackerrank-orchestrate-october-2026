"""CSV loaders, timestamp parsing and identifier normalisation shared by every module."""

from typing import cast
import csv
import re
from datetime import datetime, timezone

from . import paths
from .shapes import ActivityRow, ManifestRow, Request, RuleRow

_USER_RE = re.compile(r"^user_0*(\d+)$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PHONE_RE = re.compile(r"^\+\d[\d \-()]{6,}$")
NULLISH = {"", "null", "none", "nan"}


def parse_ts(value: object) -> datetime | None:
    """Parse ISO-8601 (with Z) into an aware UTC datetime; None for blanks."""
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() in NULLISH:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def require_ts(value: object) -> datetime:
    """Like parse_ts, for a field that must hold a timestamp (a request's dates, an event time): a blank is an error."""
    dt = parse_ts(value)
    if dt is None:
        raise ValueError(f"expected a timestamp, got {value!r}")
    return dt


def read_csv(name: str) -> list[dict]:
    with open(paths.DATASET / name, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def case_sort_key(case_id: str) -> tuple[int, str]:
    m = re.search(r"(\d+)$", case_id)
    return (int(m.group(1)) if m else 0, case_id)


# ---------------------------------------------------------------- identifiers

def norm_user(value: object) -> str | None:
    m = _USER_RE.match(str(value).strip().lower())
    return f"user_{int(m.group(1))}" if m else None


def norm_email(value: object) -> str:
    return str(value).strip().lower()


def norm_phone(value: object) -> str:
    return re.sub(r"[^\d+]", "", str(value))


def classify_identifier(value: object) -> tuple[str, str] | None:
    """Return (kind, normalised) for a scalar that looks like an identifier, else None."""
    if value is None or isinstance(value, (bool, int, float)):
        return None
    s = str(value).strip()
    if not s or s.lower() in NULLISH:
        return None
    u = norm_user(s)
    if u:
        return ("user", u)
    if _EMAIL_RE.fullmatch(s):
        return ("email", norm_email(s))
    if _HASH_RE.match(s.lower()):
        return ("hash", s.lower())
    if _PHONE_RE.match(s):
        return ("phone", norm_phone(s))
    return None


def extract_identifiers(obj: object, skip_keys: tuple[str, ...] = ("resource_id",)) -> set[str]:
    """Collect normalised identifiers from any nested JSON-like value.

    Scalars are classified whole; free text is also scanned for embedded emails
    (support message bodies quote the customer's address).
    """
    found = set()

    def walk(v: object, key: str | None = None) -> None:
        if key in skip_keys:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x, key)
        else:
            c = classify_identifier(v)
            if c:
                found.add(c[1])
            elif isinstance(v, str):
                found.update(norm_email(e) for e in _EMAIL_RE.findall(v))

    walk(obj)
    return found


def is_nullish(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in NULLISH)


# ---------------------------------------------------------------- datasets

def load_requests(sample: bool = False) -> list[Request]:
    return cast(list[Request], read_csv("sample_deletion_requests.csv" if sample else "deletion_requests.csv"))


def load_manifest() -> dict[str, ManifestRow]:
    rows = read_csv("evidence_files.csv")
    for r in rows:
        r["captured_at_dt"] = require_ts(r["captured_at"])
    return cast(dict[str, ManifestRow], {r["file_id"]: r for r in rows})


def load_activity() -> list[ActivityRow]:
    rows = read_csv("system_activity.csv")
    for r in rows:
        r["event_time_dt"] = require_ts(r["event_time"])
    return cast(list[ActivityRow], rows)


def load_rules() -> dict[str, RuleRow]:
    return cast(dict[str, RuleRow], {r["data_category"]: r for r in read_csv("deletion_rules.csv")})
