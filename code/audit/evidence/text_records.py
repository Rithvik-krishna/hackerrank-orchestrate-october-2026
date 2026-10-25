"""Pipe-delimited handover records:  - <ts> | <system> | <resource> | <CATEGORY> | <STATE> | <identifier>"""

from datetime import datetime
from pathlib import Path
import re

from ..diagnostics import ITEM, Diagnostic, Salvage
from ..loaders import extract_identifiers, parse_ts
from ..model import Observation
from .common import RESULT_STATUS

_ROW = re.compile(r"^-\s*(.+?)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.*)$")


def parse(evidence_id: str, path: Path, manifest_time: datetime) -> tuple:
    obs, skipped = [], []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _ROW.match(line.strip())
        if not m:
            if line.strip().startswith("- ") and "|" in line:   # a record row that does not have the six fields
                skipped.append(Diagnostic(evidence_id, "text", ITEM, f"line {no}", "malformed_row", "expected six pipe-separated fields"))
            continue
        ts, system, rid, category, state, ident = m.groups()
        ids = extract_identifiers({"v": ident.strip()})
        # IDENTIFIABLE / FOUND / ACTIVE rows expose the identifier; the rest only reference it.
        exposed = state in ("IDENTIFIABLE", "FOUND", "ACTIVE")
        obs.append(Observation(
            evidence_id=evidence_id,
            observed_at=parse_ts(ts) or manifest_time,
            system_id=system,
            resource_id=rid,
            status=RESULT_STATUS.get(state, "UNKNOWN"),
            category=category,
            identifiers=ids if exposed else set(),
            link_keys=ids,
            attrs={"result": state},
            source="text.handover",
        ))
    return (obs, [], Salvage(skipped)) if skipped else (obs, [])
