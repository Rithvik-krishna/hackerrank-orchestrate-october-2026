"""One normaliser for every log dialect in the dataset.

Dialects seen (all start with an ISO timestamp):
  kv          2026-.. level=INFO service=s trace_id=t event=e k=v k="v"
  json        2026-.. {"severity":..,"service":..,"trace_id":..,"event":..}
  syslog      2026-.. <15>1 node-1 <service> <trace> - - k="v" ...
  pipe        2026-.. | LEVEL | <service> | <trace> | <event> | k="v" ...
  bracket     2026-.. [LEVEL] [<service>] [<trace>] <event>: k="v" ...
  cloud       2026-.. requestId=<trace> level=L function=<service> message="k=\"v\" ..."
  flat        2026-.. INFO service=s event=record_lookup system=".." resource_id=".." result=".."

Every line becomes LogLine(ts, service, trace, event, kv). Lines sharing a trace are one
request: the *started* line carries the lookup key, later lines carry resource_id and result.
"""

from datetime import datetime
from pathlib import Path
import json
import re
from dataclasses import dataclass

from ..diagnostics import ITEM, Diagnostic, Salvage, short
from ..loaders import extract_identifiers, parse_ts
from ..model import ABSENT, ACTIVE, BLOCKED, DISABLED, PRESENT, RESTORABLE, UNKNOWN, Observation
from .common import PII_FIELDS, RESULT_STATUS, category_for, system_for, to_bool

_READ_EVENT = re.compile(r"(?:^|\s)\w+_(?:read|loaded)(?:\s|$)")
_KV = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
LOOKUP_KEYS = ("lookup_key", "match_key", "scan_key", "subject_key")
NOISE = {"level", "severity", "logger", "instance", "pod", "region", "attempt", "node", "seq",
         "duration_ms", "took_ms", "latency_ms", "wait_ms", "shard"}

# event name -> category, where the event says more than the resource prefix does
EVENT_CATEGORY = [
    (re.compile(r"attachment"), "SUPPORT_ATTACHMENT"),
    (re.compile(r"analytics_"), "ANALYTICS_PROFILE"),
    (re.compile(r"export_"), "USER_EXPORT"),
    (re.compile(r"message_|ticket_"), "SUPPORT_MESSAGE"),
    (re.compile(r"connector_"), "DATA_FLOW"),
    (re.compile(r"restore_"), "BACKUP_ARCHIVE"),
    (re.compile(r"audit_record"), "SECURITY_AUDIT_LOG"),
    (re.compile(r"contact_query"), "SECONDARY_IDENTIFIER"),   # legacy contact index entry
    (re.compile(r"contact_lookup"), "MARKETING_PROFILE"),     # opt-in/contact record
]


@dataclass
class LogLine:
    ts: object
    service: str
    trace: str
    event: str
    kv: dict


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return re.sub(r'\\(.)', r"\1", v[1:-1])
    return v


def _pairs(text: str) -> dict[str, str]:
    return {k: _unquote(v) for k, v in _KV.findall(text)}


def parse_line(line: str) -> "LogLine | None":
    ts_text, _, rest = line.partition(" ")
    ts = parse_ts(ts_text) if ts_text[:2] == "20" else None
    if ts is None:
        return None
    rest = rest.strip()
    service = trace = event = ""
    if rest.startswith("{"):
        kv = {k: str(v) for k, v in json.loads(rest).items()}
    elif rest.startswith("<"):
        parts = rest.split(" ", 5)
        service, trace = parts[2], parts[3]
        kv = _pairs(parts[5] if len(parts) > 5 else "")
    elif rest.startswith("|"):
        parts = [p.strip() for p in rest.strip("|").split("|")]
        service, trace, event = parts[1], parts[2], parts[3]
        kv = _pairs(parts[4] if len(parts) > 4 else "")
    elif rest.startswith("["):
        m = re.match(r"\[(\w+)\]\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s*([\w.]+):?\s*(.*)", rest)
        if not m:
            return None
        _, service, trace, event, tail = m.groups()
        kv = _pairs(tail)
    else:
        kv = _pairs(rest)
        if "message" in kv and "function" in kv:  # cloud runtime: inner pairs are escaped
            inner = _pairs(kv.pop("message"))
            service, trace = kv.pop("function"), kv.pop("requestId", "")
            kv.update(inner)
    service = kv.pop("service", "") or kv.pop("logger", "") or service
    trace = kv.pop("trace_id", "") or kv.pop("requestId", "") or trace
    event = kv.pop("event", "") or event
    kv = {k: v for k, v in kv.items() if k not in NOISE}
    return LogLine(ts, service, trace, event, kv)


def _status(event: str, kv: dict[str, str]) -> str:
    if "enabled" in kv:
        return ACTIVE if to_bool(kv["enabled"]) else DISABLED
    if "restore_allowed" in kv:
        return RESTORABLE if to_bool(kv["restore_allowed"]) else BLOCKED
    if "deletion_block" in kv:
        return BLOCKED if to_bool(kv["deletion_block"]) else RESTORABLE
    if "eligible" in kv:
        return RESTORABLE if to_bool(kv["eligible"]) else BLOCKED
    if "decision" in kv:  # restore_authorization_completed
        return BLOCKED if kv["decision"].lower() == "deny" else RESTORABLE
    if "available" in kv:  # export_download_check_completed
        return PRESENT if to_bool(kv["available"]) else ABSENT
    if "matched_value" in kv or "direct_identifiers" in kv:  # *_pii_scan / analytics_identity_scan
        return PRESENT
    if kv.get("http_status") == "404" or kv.get("rows") == "0":
        return ABSENT
    if "result" in kv:
        r = kv["result"].upper()
        if r in RESULT_STATUS:
            return RESULT_STATUS[r]
        if r == "HIT":
            return PRESENT
        if r in ("MISS", "NO_HIT", "EMPTY"):
            return ABSENT
    if kv.get("marker_found") is not None:
        return PRESENT if not to_bool(kv["marker_found"]) else ABSENT
    if "pii_scan" in event or kv.get("rows", "0") != "0" or kv.get("http_status") == "200":
        return PRESENT
    if "read_completed" in event or "query_completed" in event:  # the record was read back
        return PRESENT
    if _READ_EVENT.search(event):  # message_read, retained_order_loaded: the record exists (its content shows in the line)
        return PRESENT
    return UNKNOWN


def _category(event: str, rid: str, kv: dict[str, str]) -> str | None:
    if kv.get("record_type"):
        return kv["record_type"]
    for pattern, cat in EVENT_CATEGORY:
        if pattern.search(event):
            return cat
    return category_for(rid)


def read_lines(evidence_id: str, text: str) -> tuple[list, list]:
    """Parse every line; a line that starts like a log entry but cannot be read is skipped and reported."""
    lines, skipped = [], []
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = parse_line(raw)
        except Exception as exc:   # damaged JSON, short syslog header, ...
            skipped.append(Diagnostic(evidence_id, "log", ITEM, f"line {no}", "malformed_line", short(exc)))
            continue
        if line is not None:
            lines.append(line)
        elif raw[:2] == "20":   # looks like an entry (starts with a year) but has no readable timestamp or layout
            skipped.append(Diagnostic(evidence_id, "log", ITEM, f"line {no}", "unrecognised_line", "no readable timestamp or layout"))
    return lines, skipped


def parse(evidence_id: str, path: Path, manifest_time: datetime) -> tuple:
    lines, skipped = read_lines(evidence_id, path.read_text(encoding="utf-8"))
    result = _observations(evidence_id, lines)
    return (*result, Salvage(skipped)) if skipped else result


def _observations(evidence_id: str, lines: list) -> tuple:

    # Lookup keys per trace come from the lines that name no resource (the *started* lines):
    # whatever identifier-shaped value they carry (lookup_key, subject_ref, owner_key, ...).
    trace_keys: dict[str, set[str]] = {}
    for l in lines:
        if l.trace:
            ids = extract_identifiers(l.kv)  # resource_id is skipped by extract_identifiers
            if ids:
                trace_keys.setdefault(l.trace, set()).update(ids)

    # Merge every resource line of a trace (e.g. contact_query_completed + deletion_marker_check).
    merged: dict[tuple[str, str], dict] = {}
    for l in lines:
        rid = l.kv.get("resource_id")
        if not rid:
            continue
        key = (l.trace or f"line-{id(l)}", rid)
        if key in merged:
            m = merged[key]
            m["kv"].update(l.kv)
            m["events"].append(l.event)
            m["ts"] = max(m["ts"], l.ts)
        else:
            merged[key] = {"ts": l.ts, "service": l.service, "trace": l.trace, "events": [l.event], "kv": dict(l.kv)}

    obs = []
    for (trace, rid), m in merged.items():
        kv, events = m["kv"], " ".join(m["events"])
        statuses = [_status(e, kv) for e in m["events"]]
        status = _status(events, kv)
        if status == UNKNOWN:
            status = next((s for s in statuses if s != UNKNOWN), UNKNOWN)
        payload = {k: v for k, v in kv.items() if k not in LOOKUP_KEYS and k not in ("resource_id", "system")}
        pii = {k: (None if v in ("null", "None", "") else v) for k, v in payload.items()
               if k in PII_FIELDS or k in ("matched_value", "correlation_key")}
        link = set(trace_keys.get(trace, set()))
        link |= extract_identifiers({"s": kv.get("subject_reference")})
        attrs = {"pii_fields": pii, "events": m["events"], "trace": trace}
        for k in ("retention_until", "connector_name", "archive_key", "result", "last_delivery_status"):
            if k in kv:
                attrs[k] = kv[k]
        system = kv.get("system") or (m["service"] if m["service"] in _KNOWN_SYSTEMS else system_for(rid))
        exposed = extract_identifiers(payload) if status != ABSENT else set()
        link |= exposed if status == ABSENT else set()
        obs.append(Observation(
            evidence_id=evidence_id, observed_at=m["ts"], system_id=system, resource_id=rid,
            status=status, category=_category(events, rid, kv),
            identifiers=exposed, link_keys=link | exposed, attrs=attrs, source="log",
        ))
    return obs, []


_KNOWN_SYSTEMS = {
    "account_service", "search_service", "analytics_platform", "support_platform", "export_service",
    "integration_service", "backup_service", "security_service", "identity_service",
}
