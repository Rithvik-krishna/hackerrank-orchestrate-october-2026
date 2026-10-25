"""Text extraction for the ReportLab PDFs (ASCII85 + Flate content streams), stdlib only.

PDFs are business documents (statements, attachments, registers, journal extracts). Each
resource id mentioned becomes a PRESENT observation carrying the identifiers in the
document; business references (statement numbers, order ids) are kept for catalog links.
"""

from pathlib import Path
import base64
import re
import zlib
from datetime import datetime, timezone

from ..loaders import extract_identifiers
from ..model import PRESENT, Observation
from .common import RESOURCE_ID_RE, category_for, system_for

_STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)endstream", re.S)
_TJ = re.compile(rb"\(((?:\\.|[^\\)])*)\)\s*Tj")
_REFERENCE = re.compile(r"\b(?:NST-\d{4}-\d{4}-\d+|ORD-\d+|CS-\d+|CJX-\d+-\d+)\b")
_RETAIN = re.compile(r"retain until (\d{1,2} \w{3} \d{4})", re.I)


def extract_lines(path: Path) -> list[str]:
    data = path.read_bytes()
    lines = []
    for m in _STREAM.finditer(data):
        hdr, body = m.group(1), m.group(2).strip()
        try:
            if b"ASCII85" in hdr:
                body = base64.a85decode(body.removeprefix(b"<~").removesuffix(b"~>"))
            if b"Flate" in hdr:
                body = zlib.decompress(body)
        except (ValueError, zlib.error):
            continue  # font programs and images
        for t in _TJ.findall(body):
            lines.append(re.sub(rb"\\(.)", rb"\1", t).decode("latin-1"))
    return lines


def _date(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%d %b %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse(evidence_id: str, path: Path, manifest_time: datetime) -> tuple:
    lines = extract_lines(path)
    text = "\n".join(lines)
    ids = extract_identifiers(lines)
    refs = sorted(set(_REFERENCE.findall(text)))
    retain = [_date(d) for d in _RETAIN.findall(text)]
    obs = []
    for rid in sorted(set(RESOURCE_ID_RE.findall(text))):
        attrs = {"references": refs, "pii_fields": {}}
        if retain and rid.startswith("audit_record_"):
            attrs["retention_until"] = max(d for d in retain if d).isoformat()
        category = category_for(rid)
        if rid.startswith("support_") and re.search(r"attachment", text, re.I):
            category = "SUPPORT_ATTACHMENT"
        obs.append(Observation(
            evidence_id=evidence_id, observed_at=manifest_time, system_id=system_for(rid),
            resource_id=rid, status=PRESENT, category=category,
            identifiers=set(ids), link_keys=set(ids), attrs=attrs, source="pdf",
        ))
    # Documents without a resource id (statements, registers) are kept as references only;
    # the linker resolves them through LINK_RESOURCE_IDENTITY (e.g. NST-...-1842 -> export_1842).
    doc = {"evidence_id": evidence_id, "observed_at": manifest_time, "identifiers": ids,
           "references": refs, "text": text}
    return obs, [], doc
