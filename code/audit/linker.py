"""Decide which resources belong to a case's subject.

Evidence of belonging, strongest first:
  1. identifier match: an observation's identifiers/lookup keys intersect the subject's
     identifier set as of audit_at (current + historical email/phone/hash, user_id)
  2. dated catalog link (LINK_RESOURCE_IDENTITY activity, request <= event_time <= audit_at)
  3. chains: a search document's source_resource_id, a PDF reference (statement number)
  4. deletion-worker activity near the case's reported_at for resources that carry no
     identifier at all (anonymised or already-deleted records). Resources that evidence
     ties to a *different* user are excluded; what remains is assigned by alignment
     (see `_align`) and flagged as such.

Every ownership fact is time-aware: a record only counts as someone's if it was observed
at or before the cutoff in question.
"""

from typing import TYPE_CHECKING
from datetime import datetime
import os
import re
from collections import defaultdict
from datetime import timedelta

from .evidence.common import category_for, system_for
from .loaders import norm_user, require_ts
from .model import Observation, PRESENT
from .shapes import ActivityRow, AlignStats, Alignment, AmbiguousGroup, Links, Request, Resolver

if TYPE_CHECKING:
    from .evidence import EvidenceStore
    from .identity import IdentityIndex

_LINK_RE = re.compile(r"(?:linked|confirmed) (\S+) \((\S+)\) (?:to|belongs only to) (\S+) and (user_\d+)")

# "unique": assign only 1:1 matches. "order": also zip equal-sized ambiguous groups by case order
# against time order. "llm": 1:1 matches, and every ambiguous group goes to the model adjudicator
# (audit/adjudicator.py) behind its guardrails; undecided groups stay unassigned.
# Chosen by measurement on the labelled samples (see evaluation report).
ZIP_MODE = os.environ.get("AUDIT_ZIP_MODE", "order")
# "cutoff": only ownership shown by evidence available at the case's audit_at rules a row out (strict
# reading of "use only information available at audit_at"). "any": ownership of an account by another
# person learned at any time may rule a row out (never attributes one). See the evaluation report.
OWNERSHIP = os.environ.get("AUDIT_OWNERSHIP", "cutoff")
RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
ACCOUNT_WINDOW = (-1.5, 0.25)   # hours relative to reported_at
POLICY_WINDOW = (-1.0, 2.5)


def _is_account_delete(a: ActivityRow) -> bool:
    return a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service"


def _case_no(case: Request) -> int:
    return int(re.sub(r"\D", "", case["case_id"]) or 0)


class Linker:
    def __init__(self, identity: "IdentityIndex", store: "EvidenceStore", activity: list[ActivityRow], all_requests: list[Request], adjudicator: Resolver | None = None) -> None:
        self.identity, self.store, self.activity = identity, store, activity
        self.requests = all_requests
        self.adjudicator = adjudicator
        self.case_links: dict[str, list[tuple[str, str]]] = {}   # case_id -> [(resource_id, system)] linked by evidence
        self.worker = [a for a in activity if a["source_system"] == "deletion_worker"]

        # catalog links: resource -> [(user, event_time, activity_id, system)]
        self.links: defaultdict[str, list[tuple[str, datetime, str, str]]] = defaultdict(list)
        self.link_rows: list[tuple[ActivityRow, re.Match[str]]] = []
        for a in activity:
            if a["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = _LINK_RE.search(a["details"])
            if m:
                self.links[a["resource_id"]].append(
                    (norm_user(m.group(4)) or m.group(4), a["event_time_dt"], a["activity_id"], a["target_system"]))
                self.link_rows.append((a, m))
        self._documents_to_observations()

        # ownership facts with the time they became known: resource -> [(user, time)]
        self._owner_facts: defaultdict[str, list[tuple[str, datetime]]] = defaultdict(list)
        for o in store.observations:
            users = {u for x in o.identifiers | o.link_keys for u in identity.owners(x)}
            for u in users:
                self._owner_facts[o.resource_id].append((u, o.observed_at))
                src = o.attrs.get("source_resource_id")
                if src:
                    self._owner_facts[src].append((u, o.observed_at))
        for rid, ls in self.links.items():
            for l in ls:
                self._owner_facts[rid].append((l[0], l[1]))
        self._user_kind_facts: defaultdict[str, list[tuple[str, datetime]]] = defaultdict(list)  # user -> [(kind, time)]
        for rid, facts in self._owner_facts.items():
            for u, t in facts:
                self._user_kind_facts[u].append((system_for(rid), t))

        self._assignments: Alignment | None = None
        self.violating: set[str] = set()
        self.unresolved: defaultdict[str, list[str]] = defaultdict(list)   # case_id -> why the case may be missing a record
        self.case_files: dict[str, set[str]] = {}   # case_id -> evidence files that link the case by identifier

    # ------------------------------------------------------------------ documents
    def _documents_to_observations(self) -> None:
        """A PDF that quotes a business reference is the content of the resource the catalog
        links to that reference (statement NST-... -> export_1842)."""
        for doc in self.store.documents:
            for a, m in self.link_rows:
                resource, label = m.group(1), m.group(2)
                if label in doc["references"] or label in doc["text"]:
                    self.store.observations.append(Observation(
                        evidence_id=doc["evidence_id"], observed_at=doc["observed_at"],
                        system_id=a["target_system"], resource_id=resource, status=PRESENT,
                        category=category_for(resource), identifiers=set(doc["identifiers"]),
                        link_keys=set(doc["identifiers"]), attrs={"pii_fields": {}}, source="pdf.link",
                    ))
        self.store.by_resource.clear()
        for o in self.store.observations:
            self.store.by_resource[(o.system_id, o.resource_id)].append(o)

    # ------------------------------------------------------------------ ownership
    def owners_at(self, rid: str, when: datetime | None) -> set[str]:
        """Users that evidence observed at or before `when` ties to the resource."""
        return {u for u, t in self._owner_facts.get(rid, ()) if when is None or t <= when}

    def _foreign(self, rid: str, uid: str, when: datetime | None) -> bool:
        o = self.owners_at(rid, when)
        return bool(o) and uid not in o

    def _owns_kind(self, uid: str | None, kind: str, when: datetime | None) -> bool:
        if uid is None:
            return False
        return any(k == kind and (when is None or t <= when) for k, t in self._user_kind_facts.get(uid, ()))

    # ------------------------------------------------------------------ alignment
    def _align(self) -> Alignment:
        """Assign identifier-less worker rows to cases sharing a reported_at.

        For each reported_at group and each kind of resource (account, analytics, support...),
        take the worker rows in the window that no evidence ties to anyone, and the group's cases
        that own no resource of that kind. A row that evidence shows (without naming anyone) can
        only belong to a case documented by the same file, and, being an anonymised or deleted
        record, only to a case with no outstanding violation. Equal counts are zipped in case order
        against time order; otherwise nothing is assigned (ambiguous).
        """
        groups = defaultdict(list)
        for r in self.requests:
            if r.get("uid") and r.get("is_deletion", True):
                groups[r["reported_at"]].append(r)
        out: Alignment = {}  # case_id -> {kind: [(system, rid, activity_id)]}
        stats: AlignStats = {"zipped": 0, "ambiguous": 0}
        for rep_s, cases in groups.items():
            cutoff = max(require_ts(c["audit_at"]) for c in cases)
            cases = sorted(cases, key=_case_no)
            for kind, rs in self._window_rows(require_ts(rep_s)).items():
                if kind == "account_service":
                    continue  # accounts are matched across groups, see _align_accounts
                self._align_kind(kind, rs, cases, cutoff, out, stats)
        self._align_accounts(out, stats)
        self.align_stats = stats
        return out

    def _window_rows(self, rep: datetime) -> dict[str, list]:
        """Worker rows whose time falls in the window after `rep` for their operation, grouped by system id."""
        rows = defaultdict(list)
        for a in self.worker:
            t = a["event_time_dt"]
            if _is_account_delete(a):
                lo, hi = ACCOUNT_WINDOW
            elif a["event_type"] in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
                                     "DISABLE_DATA_FLOW", "BLOCK_RESTORE"):
                lo, hi = POLICY_WINDOW
            else:
                continue
            if rep + timedelta(hours=lo) <= t <= rep + timedelta(hours=hi):
                rows[system_for(a["resource_id"])].append(a)  # kinds are system ids
        return rows

    def _align_kind(self, kind: str, rs: list, cases: list[Request], cutoff: datetime, out: Alignment, stats: AlignStats) -> None:
        """Link one kind's untied worker rows to the group's cases that still need a record of that kind."""
        free = sorted((a for a in rs if not self.owners_at(a["resource_id"], cutoff)
                       and not any(l[1] <= cutoff for l in self.links.get(a["resource_id"], ()))),
                      key=lambda a: a["event_time_dt"])
        needy = [c for c in cases if not self._owns_kind(c["uid"], kind, require_ts(c["audit_at"]))]
        needy = [c for c in needy if c["case_id"] not in self.violating]
        files = [self._files_of(a, cutoff) for a in free]
        if free and all(files):
            # only cases documented by the file that shows the row; if none is, the
            # file says nothing about this group and the filter is not applied
            documented = [c for c in needy if any(self.case_files.get(c["case_id"], set()) & f for f in files)]
            needy = documented or needy
        if not free or not needy:
            return

        # Equal counts are zipped in case order against time order. A policy row's result
        # reflects what actually happened, not what the workflow claimed, so the reported
        # status is not used to split these rows. The tag records how each link was made.
        if len(free) == len(needy) and (len(free) == 1 or ZIP_MODE == "order"):
            tag = "unique" if len(free) == 1 else "order"
            for c, a in zip(needy, free):
                out.setdefault(c["case_id"], defaultdict(list))[kind].append(
                    (a["target_system"], a["resource_id"], a["activity_id"], tag))
            stats["zipped"] += len(free)
        elif ZIP_MODE == "llm" and self.adjudicator:
            if not self._adjudicate(kind, needy, free, cutoff, out, stats):
                self._note_unresolved(needy, kind, len(free))
        else:
            stats["ambiguous"] += len(free)
            self._note_unresolved(needy, kind, len(free))

    def _align_accounts(self, out: Alignment, stats: AlignStats) -> None:
        """Match account deletions to cases whose account no evidence shows.

        The worker records an account deletion shortly before the workflow reports its status, and
        the row's result mirrors that status (COMPLETE/SUCCESS, PARTIALLY_COMPLETE/PARTIAL,
        NOT_STARTED/FAILED). A row can fit the windows of cases from different batches, so rows are
        matched across all cases at once: the most constrained case first, evidence-owned rows
        removed, ties broken by case order against time order.
        """
        rows = [a for a in self.worker if _is_account_delete(a)]
        needy = [r for r in self.requests
                 if r.get("uid") and r.get("is_deletion", True)
                 and not self._owns_kind(r["uid"], "account_service", require_ts(r["audit_at"]))]

        def candidates(r: Request, same_class: bool) -> list[ActivityRow]:
            audit_at = require_ts(r["audit_at"])
            rep = require_ts(r["reported_at"])
            want = RESULT_FOR_STATUS.get(r["reported_status"])
            return sorted(
                (a for a in rows
                 if (not same_class or a["reported_result"] == want)
                 and rep + timedelta(hours=ACCOUNT_WINDOW[0]) <= a["event_time_dt"] <= rep + timedelta(hours=ACCOUNT_WINDOW[1])
                 # Ownership of an account by another person rules a row out (never attributes one);
                 # by default only what evidence showed by this case's audit_at counts.
                 and not (self.owners_at(a["resource_id"], audit_at if OWNERSHIP == "cutoff" else None) - {r["uid"]})
                 and not any(l[1] <= audit_at for l in self.links.get(a["resource_id"], ()))),
                key=lambda a: a["event_time_dt"])

        taken, done = set(), set()
        for same_class in (True, False):  # the claim and the worker's result may disagree
            todo = [(r, candidates(r, same_class)) for r in needy if r["case_id"] not in done]
            for r, cands in sorted(todo, key=lambda x: (len(x[1]), _case_no(x[0]))):
                avail = [a for a in cands if a["activity_id"] not in taken]
                if not avail:
                    continue
                if len(avail) > 1 and ZIP_MODE == "llm" and self.adjudicator:
                    picked = self._adjudicate("account_service", [r], avail, require_ts(r["audit_at"]), out, stats)
                    for act_id in picked:
                        taken.add(act_id)
                        done.add(r["case_id"])
                    continue
                if len(avail) > 1 and ZIP_MODE != "order":
                    stats["ambiguous"] += 1
                    continue
                pick = avail[0]
                # how sure the link is: the only row of the right class, the only row left once the
                # claim is ignored, or one of several chosen by order
                tag = "order" if len(avail) > 1 else ("unique-class" if same_class else "relaxed")
                taken.add(pick["activity_id"])
                done.add(r["case_id"])
                out.setdefault(r["case_id"], defaultdict(list))["account_service"].append(
                    (pick["target_system"], pick["resource_id"], pick["activity_id"], tag))
                stats["zipped"] += 1
        for r in needy:  # cases that end without an account row: say why
            if r["case_id"] not in done:
                n = len(candidates(r, False))
                self.unresolved[r["case_id"]].append(
                    f"account_service: {n} candidate worker rows, none assignable" if n
                    else "account_service: no deletion row in the worker activity window")

    def _adjudicate(self, kind: str, cases: list[Request], rows: list[ActivityRow], cutoff: datetime | None, out: Alignment, stats: AlignStats) -> list[str]:
        """Hand an ambiguous group to the model; returns the activity ids it was allowed to assign."""
        amb: AmbiguousGroup = {
            "kind": kind,
            "cases": [{"case_id": c["case_id"], "uid": c.get("uid") or "", "email": c["email"], "request_text": c["request_text"],
                       "reported_status": c["reported_status"], "reported_at": c["reported_at"], "audit_at": c["audit_at"],
                       "linked": self.case_links.get(c["case_id"], [])} for c in cases],
            "rows": [{"activity_id": a["activity_id"], "resource_id": a["resource_id"], "event_time": a["event_time"],
                      "result": a["reported_result"], "known_owners": sorted(self.owners_at(a["resource_id"], cutoff))}
                     for a in rows],
        }
        assert self.adjudicator is not None, "only called when a resolver is configured"
        by_act = {a["activity_id"]: a for a in rows}
        accepted = self.adjudicator.resolve(amb)
        tag = getattr(self.adjudicator, "tag", "llm")
        for case_id, act_id in accepted:
            a = by_act[act_id]
            out.setdefault(case_id, defaultdict(list))[kind].append((a["target_system"], a["resource_id"], act_id, tag))
            stats["adjudicated"] = stats.get("adjudicated", 0) + 1
        if not accepted:
            stats["ambiguous"] += len(rows)
        return [act for _, act in accepted]

    def _note_unresolved(self, cases: list[Request], kind: str, n_rows: int) -> None:
        for c in cases:
            self.unresolved[c["case_id"]].append(f"{kind}: {n_rows} unattributed worker rows fit this case equally")

    def _files_of(self, row: ActivityRow, cutoff: datetime) -> set[str]:
        """Evidence files that show the worker row's resource at or before the cutoff."""
        return {o.evidence_id for k, v in self.store.by_resource.items() if k[1] == row["resource_id"]
                for o in v if o.observed_at <= cutoff}

    # ------------------------------------------------------------------ per case
    def link(self, req: Request, uid: str) -> Links:
        """Evidence links plus the worker-activity alignment for identifier-less records."""
        found = self.link_evidence(req, uid)
        if self._assignments is None:
            self._assignments = self._align()
        when = require_ts(req["audit_at"])
        for kind, items in self._assignments.get(req["case_id"], {}).items():
            for system, rid, act_id, tag in items:
                found.setdefault((system, rid), []).append(f"worker_alignment:{act_id}:{tag}")
        return {k: v for k, v in found.items() if not self._foreign(k[1], uid, when)}

    def link_evidence(self, req: Request, uid: str) -> Links:
        """Return {(system, resource_id): [reasons]} from identifiers, catalog links and chains."""
        audit_at = require_ts(req["audit_at"])
        S = self.identity.identifiers_for(uid, audit_at)
        found: Links = defaultdict(list)

        requested_at = require_ts(req["requested_at"])
        for o in self.store.observations:
            # Evidence that predates the request cannot describe its outcome; it is how a
            # person's earlier request (same identifiers, other account) leaks in.
            if o.observed_at > audit_at or o.observed_at < requested_at:
                continue
            hit = (o.identifiers | o.link_keys) & S
            if hit:
                found[(o.system_id, o.resource_id)].append(f"identifier:{o.evidence_id}")

        for rid, ls in self.links.items():
            for u, when, act_id, system in ls:
                # a link dated before the request belongs to that person's earlier request
                if u == uid and requested_at <= when <= audit_at:
                    found[(system, rid)].append(f"catalog:{act_id}")

        for key in list(found):  # chains through source_resource_id
            for o in self.store.by_resource.get(key, ()):
                src = o.attrs.get("source_resource_id")
                if src and o.observed_at <= audit_at:
                    found[(system_for(src), src)].append(f"source_of:{key[1]}")

        # drop anything evidence ties to someone else only
        found = {k: v for k, v in found.items() if not self._foreign(k[1], uid, audit_at)}
        self.case_files[req["case_id"]] = {r.split(":", 1)[1] for rs in found.values() for r in rs
                                           if r.startswith("identifier:")}
        self.case_links[req["case_id"]] = sorted((rid, system) for system, rid in found)
        return found
