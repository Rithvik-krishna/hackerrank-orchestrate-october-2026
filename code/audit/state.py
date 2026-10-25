"""State of one resource at audit_at, reconstructed from evidence.

Rules (AGENTS.md §6.2, §6.6):
  * a record informs the case when the *record's own* time is at or before audit_at: a log
    exported later can still hold a line written before the cutoff, while a later snapshot
    inside the same bundle must not count
  * the latest observation wins; an earlier ACTIVE record never overrides a later 404
  * inside the latest snapshot, a replica that still shows the record outranks a primary
    that does not (replication drift)
  * a later non-empty snapshot of the resource's table that does not list it proves absence
    (only where policy wants the record gone)
  * a step may cite only a file captured at or before audit_at, so the citation is chosen
    separately from the decision
"""

from datetime import datetime
from typing import TYPE_CHECKING
from collections.abc import Iterable
from dataclasses import dataclass

from .evidence.common import category_for
from .model import ABSENT, PRESENT, UNKNOWN, Coverage, Observation
from .shapes import ResourceKey

if TYPE_CHECKING:
    from .evidence import EvidenceStore


@dataclass
class State:
    status: str
    evidence_id: str          # citable file ("" when none qualifies)
    category: str | None
    obs: Observation | None   # the observation that decided the state (None when by omission)
    via: str                  # "observed" | "omission" | "none"


def resource_category(observations: Iterable[Observation], resource_id: str) -> str | None:
    """Most specific category any observation gives; the resource prefix is the fallback."""
    cats = {o.category for o in observations if o.category}
    # An attachment scan names the record as an attachment; generic support tables (support_records,
    # tickets) list attachments and messages alike, so the specific reading wins (rule_06 vs rule_05).
    if "SUPPORT_ATTACHMENT" in cats and "SUPPORT_MESSAGE" in cats:
        return "SUPPORT_ATTACHMENT"
    ranked = sorted(
        (o for o in observations if o.category),
        key=lambda o: (o.source in ("log",), o.observed_at),  # structured sources first
    )
    return (ranked[0].category if ranked else None) or category_for(resource_id)


def _citable(item: Observation | Coverage, audit_at: datetime) -> bool:
    return item.file_time is None or item.file_time <= audit_at


def _status_class(status: str) -> str:
    """Sources word the same fact differently (an archive is ACTIVE in one export, RESTORABLE in a log)."""
    return {"ACTIVE": "OPEN", "RESTORABLE": "OPEN", "DISABLED": "CLOSED", "BLOCKED": "CLOSED"}.get(status, status)


def resolve_state(store: "EvidenceStore", key: ResourceKey, audit_at: datetime, category: str | None = None,
                  omit_ok: bool = False, subject_files: frozenset[str] | set[str] = frozenset()) -> State:
    system, rid = key
    everything = store.by_resource.get(key, ())
    category = category or resource_category(everything, rid)
    obs = sorted((o for o in everything if o.observed_at <= audit_at),
                 key=lambda o: (o.observed_at, o.evidence_id))
    # An observation whose status is UNKNOWN (a log line that read the record but says nothing about its state, or a
    # file that was only partly readable) carries no information, so it must not supersede one that does.
    informative = [o for o in obs if o.status != UNKNOWN]
    obs = informative or obs

    last = obs[-1] if obs else None
    decided, status = None, UNKNOWN
    if last:
        same = [o for o in obs if o.evidence_id == last.evidence_id and o.observed_at == last.observed_at]
        present = [o for o in same if o.status == PRESENT]
        decided = present[-1] if present and any(o.status == ABSENT for o in same) else last
        status = decided.status

    # A table dump proves absence if it is non-empty (an empty capture proves nothing); a bundle
    # snapshot only holds selected records, so it counts when its file also documents the subject.
    covers = [c for c in store.coverage
              if omit_ok
              and c.system_id == system and c.observed_at <= audit_at and _citable(c, audit_at)
              and (last is None or c.observed_at > last.observed_at)
              and rid not in c.resource_ids
              and ((c.scope == "full" and c.resource_ids) or c.evidence_id in subject_files)
              and category_for("", c.table) == category]
    # a record already seen absent stays absent; omission only overturns a sighting or fills a gap
    if covers and (last is None or status in (PRESENT, UNKNOWN)):
        c = max(covers, key=lambda c: (c.observed_at, c.evidence_id))
        return State(ABSENT, c.evidence_id, category, decided, "omission")

    if decided is None:
        return State(UNKNOWN, "", category, None, "none")

    evidence_id = decided.evidence_id if _citable(decided, audit_at) else ""
    if not evidence_id:  # the same fact seen in a file that existed by audit_at
        alt = [o for o in obs if _status_class(o.status) == _status_class(decided.status) and _citable(o, audit_at)]
        if alt:
            evidence_id = max(alt, key=lambda o: (o.observed_at, o.evidence_id)).evidence_id
    return State(status, evidence_id, category, decided, "observed")
