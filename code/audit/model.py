"""Shared data structures and the output-contract vocabulary."""

from dataclasses import dataclass, field
from datetime import datetime

# Observation statuses (normalised from every evidence format)
PRESENT, ABSENT = "PRESENT", "ABSENT"
ACTIVE, DISABLED = "ACTIVE", "DISABLED"
RESTORABLE, BLOCKED = "RESTORABLE", "BLOCKED"
UNKNOWN = "UNKNOWN"

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]

# operation -> (phase, state), in the order the problem statement lists them
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OPERATION_ORDER = {op: i for i, op in enumerate(OPERATIONS)}
STEP_FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}


@dataclass
class Observation:
    """One fact about one resource, from one evidence item, at one time."""

    evidence_id: str
    observed_at: datetime
    system_id: str
    resource_id: str
    status: str = UNKNOWN
    category: str | None = None
    identifiers: set = field(default_factory=set)  # identifiers present in the record
    link_keys: set = field(default_factory=set)    # keys used to look the record up
    attrs: dict = field(default_factory=dict)      # pii_fields, enabled, retention_until, replica_role...
    source: str = ""                               # parser name, for --explain
    file_time: datetime | None = None              # manifest captured_at of the containing file

    @property
    def available_at(self) -> datetime:
        """A fact can inform a case only once both the record and its file exist."""
        return max(self.observed_at, self.file_time) if self.file_time else self.observed_at


@dataclass
class Coverage:
    """A snapshot that would have contained a resource of this system had it existed."""

    evidence_id: str
    observed_at: datetime
    system_id: str
    table: str
    resource_ids: frozenset
    file_time: datetime | None = None
    # "full": a database table dump, complete for its system (omission proves absence)
    # "subject": a bundle snapshot holding only selected records (counts only when the file
    #            also documents the subject, e.g. an explicit empty profile list beside their audit record)
    scope: str = "subject"

    @property
    def available_at(self) -> datetime:
        return max(self.observed_at, self.file_time) if self.file_time else self.observed_at
