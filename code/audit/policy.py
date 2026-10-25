"""Apply deletion_rules.csv to one resource's reconstructed state.

Each assessment says what the rules require, whether the record complies at audit_at, and
which finding / remediation / verification operations follow.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from dataclasses import dataclass

from .loaders import classify_identifier
from .model import ABSENT, ACTIVE, BLOCKED, DISABLED, PRESENT, RESTORABLE, Observation
from .shapes import RuleRow

if TYPE_CHECKING:
    from .state import State

_NAME_FIELDS = {"name", "full_name", "display_name", "requester_name"}
_REF_KINDS = {"user"}


@dataclass
class Assessment:
    system: str
    resource_id: str
    category: str | None
    required: str          # DELETE | REDACT | ANONYMIZE | RETAIN | DISABLE | BLOCK
    compliant: bool
    finding: str | None
    remediation: str | None
    verification: str | None
    evidence_id: str
    note: str = ""
    basis: str = "evidence"   # how the resource was tied to the subject (see ledger.py)
    via: str = "observed"     # how its state was established: observed, or omission (inferred absence)


def required_action(category: str | None, rules: dict[str, RuleRow]) -> str | None:
    """deletion_rules.csv action for a category, plus the two non-record rules."""
    if category == "DATA_FLOW":
        return "DISABLE"
    if category == "BACKUP_ARCHIVE":
        return "BLOCK"  # rule_14: retained, but only while restore access can be blocked
    rule = rules.get(category) if category is not None else None
    return rule["required_action"] if rule else None


def _exposed(obs: Observation, subject_ids: set[str], anonymize: bool) -> bool:
    """Direct identifiers of the subject still visible in the record."""
    names = any(v for k, v in obs.attrs.get("pii_fields", {}).items() if k in _NAME_FIELDS)
    seen = set()
    for ident in obs.identifiers:
        c = classify_identifier(ident)
        if not c:
            continue
        kind, value = c
        if kind in _REF_KINDS and not anonymize:
            continue  # a stable user reference may stay in a redacted record
        if value in subject_ids:
            seen.add(kind)
    return names or bool(seen)


def assess(system: str, rid: str, category: str | None, state: "State", rules: dict[str, RuleRow], subject_ids: set[str],
           audit_at: datetime, shared: bool = False) -> Assessment | None:
    action = required_action(category, rules)
    ev = state.evidence_id
    if action is None:
        return None

    if action == "DELETE":
        if category == "SUPPORT_ATTACHMENT" and shared:
            return None  # rule_06 only applies to attachments that belong solely to the subject
        if state.status == ABSENT:
            return Assessment(system, rid, category, action, True, None, None, "VERIFY_RECORD_ABSENT", ev)
        if state.status == PRESENT:
            return Assessment(system, rid, category, action, False, "REPORT_REMAINING_RECORD",
                              "DELETE_RECORD", "VERIFY_RECORD_ABSENT", ev)
        return None

    if action in ("REDACT", "ANONYMIZE"):
        anonymize = action == "ANONYMIZE"
        op_fix = "ANONYMIZE_RECORD" if anonymize else "REDACT_RECORD"
        op_ver = "VERIFY_RECORD_ANONYMIZED" if anonymize else "VERIFY_RECORD_REDACTED"
        if state.status != PRESENT or state.obs is None:
            return None
        if _exposed(state.obs, subject_ids, anonymize):
            return Assessment(system, rid, category, action, False, "REPORT_EXPOSED_PII", op_fix, op_ver, ev)
        return Assessment(system, rid, category, action, True, None, None, op_ver, ev)

    if action == "RETAIN":
        retained_until = state.obs.attrs.get("retention_until") or state.obs.attrs.get("retained_until") if state.obs else None
        if state.status == PRESENT:
            return Assessment(system, rid, category, action, True, None, None, "VERIFY_RECORD_PRESENT", ev,
                              note=f"retention_until={retained_until}")
        return None

    if action == "DISABLE":
        if state.status == ACTIVE:
            return Assessment(system, rid, category, action, False, "REPORT_ACTIVE_DATA_FLOW",
                              "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED", ev)
        if state.status == DISABLED:
            return Assessment(system, rid, category, action, True, None, None, "VERIFY_DATA_FLOW_DISABLED", ev)
        return None

    if action == "BLOCK":
        if state.status in (RESTORABLE, ACTIVE):  # an "ACTIVE" archive is one restore can use
            return Assessment(system, rid, category, action, False, "REPORT_RESTORE_RISK",
                              "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED", ev)
        if state.status == BLOCKED:
            return Assessment(system, rid, category, action, True, None, None, "VERIFY_RESTORE_BLOCKED", ev)
        return None
    return None
