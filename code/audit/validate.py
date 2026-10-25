"""Output-contract checks (AGENTS.md §6.3, §6.4, §6.6). Returns a list of error strings."""

import json
from collections.abc import Sequence
from datetime import datetime

from .loaders import case_sort_key, parse_ts
from .model import OPERATIONS, PHASES, STATUSES, STEP_FIELDS
from .shapes import RequestRow, Step

HEADER = ["case_id", "deletion_status", "steps_json"]


def validate_rows(rows: list[dict], requests: Sequence[RequestRow], citable: dict[str, datetime | None], strict_dates: bool = True) -> list[str]:
    """rows: [{case_id, deletion_status, steps_json}] in file order.
    requests: input request rows. citable: {evidence_id: datetime available}."""
    errors = []
    req = {r["case_id"]: r for r in requests}
    ids = [r["case_id"] for r in rows]
    if sorted(ids) != sorted(req):
        missing = set(req) - set(ids)
        extra = set(ids) - set(req)
        errors.append(f"case set mismatch: missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}")
    if len(ids) != len(set(ids)):
        errors.append("duplicate case_id rows")
    if ids != sorted(ids, key=case_sort_key):
        errors.append("rows are not sorted by case_id")

    for row in rows:
        cid, status = row["case_id"], row["deletion_status"]
        where = f"{cid}:"
        if status not in STATUSES:
            errors.append(f"{where} bad status {status!r}")
        try:
            steps = json.loads(row["steps_json"])
        except json.JSONDecodeError as exc:
            errors.append(f"{where} steps_json is not JSON ({exc})")
            continue
        if not isinstance(steps, list):
            errors.append(f"{where} steps_json is not an array")
            continue
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{where} NOT_APPLICABLE must have []")
        audit_at = parse_ts(req[cid]["audit_at"]) if cid in req else None
        errors += _check_steps(where, status, steps, audit_at, citable, strict_dates)
    return errors


def _check_steps(where: str, status: str, steps: list[Step], audit_at: datetime | None, citable: dict[str, datetime | None], strict_dates: bool) -> list[str]:
    """Contract checks for one row's steps: fields, numbering, operation/phase/state, phase order, evidence rules."""
    errors = []
    last_phase = 0
    for i, s in enumerate(steps, 1):
        if set(s) != set(STEP_FIELDS):
            errors.append(f"{where} step {i} fields {sorted(s)}")
            continue
        if s["step_no"] != i:
            errors.append(f"{where} step_no {s['step_no']} at position {i}")
        op = s["operation"]
        if op not in OPERATIONS:
            errors.append(f"{where} unknown operation {op}")
            continue
        phase, state = OPERATIONS[op]
        if (s["phase"], s["state"]) != (phase, state):
            errors.append(f"{where} {op} must be {phase}/{state}")
        p = PHASES.index(s["phase"])
        if p < last_phase:
            errors.append(f"{where} phases out of order at step {i}")
        last_phase = p
        if status == "COMPLETE" and phase != "VERIFICATION":
            errors.append(f"{where} COMPLETE case has a {phase} step")
        errors += _check_evidence(where, i, s["evidence_id"], phase == "FINDING" or status == "COMPLETE", audit_at, citable, strict_dates)
    return errors


def _check_evidence(where: str, i: int, ev: str, needs_evidence: bool, audit_at: datetime | None, citable: dict[str, datetime | None], strict_dates: bool) -> list[str]:
    """A finding or a COMPLETE-case step must cite an existing, already available id; future work cites nothing."""
    if needs_evidence and not ev:
        return [f"{where} step {i} needs an evidence_id"]
    if not needs_evidence and ev:
        return [f"{where} step {i} is future work and must use \"\""]
    if ev and ev not in citable:
        return [f"{where} step {i} cites unknown id {ev}"]
    available = citable.get(ev)
    if ev and strict_dates and audit_at and available and available > audit_at:
        return [f"{where} step {i} cites {ev} dated after audit_at"]
    return []
