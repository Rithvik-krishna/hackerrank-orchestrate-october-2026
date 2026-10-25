"""The shapes of the data that moves between modules.

Everything here is a plain dict (or tuple) at runtime: a TypedDict only *describes* the keys, so nothing is converted and
the CSV and JSON the pipeline reads and writes are unchanged. The names make the signatures in audit/ say what they take
and return instead of `dict` or `Any`.
"""

from datetime import datetime
from typing import Any, NotRequired, Protocol, TypedDict

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

# A JSON object parsed from a file or a provider response. Its fields are not known statically; every parser and
# validator that reads one checks the fields it needs at runtime (audit/evidence/json_records.py, audit/llm.validate).
JsonObject = dict[str, Any]

ResourceKey = tuple[str, str]       # (system_id, resource_id)
Links = dict[ResourceKey, list[str]]   # a case's linked records, each with the reasons that tie it to the subject
WorkerLink = tuple[str, str, str, str]  # (system_id, resource_id, activity_id, how it was matched)
Alignment = dict[str, dict[str, list[WorkerLink]]]   # case_id -> kind -> worker rows attributed to the case
AlignStats = dict[str, int]


class RequestRow(TypedDict):
    """One row of deletion_requests.csv / sample_deletion_requests.csv."""

    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


class Request(RequestRow):
    """A request after the pipeline has decided its intent and resolved its subject."""

    is_deletion: NotRequired[bool]
    intent: NotRequired[str]
    uid: NotRequired[str | None]


class ManifestRow(TypedDict):
    file_id: str
    file_path: str
    origin: str
    captured_at: str
    captured_at_dt: datetime


class ActivityRow(TypedDict):
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str
    event_time_dt: datetime


class RuleRow(TypedDict):
    rule_id: str
    data_category: str
    condition: str
    required_action: str


class Step(TypedDict):
    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str


class AmbiguousCase(TypedDict):
    """What a model sees of one case whose worker rows cannot be told apart by evidence."""

    case_id: str
    uid: str
    email: str
    request_text: str
    reported_status: str
    reported_at: str
    audit_at: str
    linked: list[tuple[str, str]]


class AmbiguousRow(TypedDict):
    activity_id: str
    resource_id: str
    event_time: str
    result: str
    known_owners: list[str]


class AmbiguousGroup(TypedDict):
    kind: str
    cases: list[AmbiguousCase]
    rows: list[AmbiguousRow]


class Resolver(Protocol):
    """A link adjudicator (single answer) or investigator (tool loop): proposes (case_id, activity_id) pairs."""

    def resolve(self, amb: AmbiguousGroup) -> list[tuple[str, str]]: ...
