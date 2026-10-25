"""Case pipeline: intent -> subject -> linked resources -> state -> policy -> steps."""

from collections.abc import Callable
from datetime import datetime

from . import intent, intent_llm, ledger, linker as linker_mod, policy
from .adjudicator import LinkAdjudicator
from .evidence import EvidenceStore
from .identity import IdentityIndex
from .investigator import LinkInvestigator
from .linker import Linker
from .llm import LLMClient
from .loaders import load_activity, load_manifest, load_requests, load_rules, parse_ts, require_ts
from .state import resolve_state, resource_category
from .policy import Assessment
from .shapes import JsonObject, Links, Request, ResourceKey, Step
from .steps import make_step, order_steps


REVERIFIED = ("USER_PROFILE", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD")   # categories re-verified in every INCOMPLETE answer
REVERIFY_OP = {"DELETE": "VERIFY_RECORD_ABSENT", "RETAIN": "VERIFY_RECORD_PRESENT"}


class Pipeline:
    """use_llm turns on the model components (intent arbitration, link adjudication); review also asks the
    model for a second opinion on every request and records disagreements without acting on them."""

    def __init__(self, use_llm: bool = False, review: bool = False, agent: bool = False) -> None:
        self.identity = IdentityIndex()
        self.manifest = load_manifest()
        self.activity = load_activity()
        self.rules = load_rules()
        self.store = EvidenceStore().load()

        self.client: LLMClient | None = None
        self.adjudicator: LinkInvestigator | LinkAdjudicator | None = None
        fallback: Callable[[str], tuple[str, str]] | None = None
        self.disagreements: list[JsonObject] = []
        self.meta: dict[str, dict] = {}   # case_id -> uncertainty ledger entry (audit/ledger.py)
        self.unverified: dict[str, list[tuple[str, str, str | None]]] = {}   # case_id -> (system, resource, category) of linked records whose state no evidence shows
        self.linked: dict[str, list[ResourceKey]] = {}   # case_id -> [(system, resource_id)] the engine tied to the case (input of the verifier)
        if use_llm:
            client = LLMClient()
            if client.available:
                self.client = client
                self.adjudicator = LinkInvestigator(client) if agent else LinkAdjudicator(client)
                fallback = intent_llm.make_fallback(client, self.disagreements)
                linker_mod.ZIP_MODE = "llm"
            else:
                print("model components requested but no API key is set (GEMINI_API_KEY or ANTHROPIC_API_KEY): running deterministically")

        reqs = load_requests(sample=True) + load_requests(sample=False)
        self._intent: dict[str, tuple[bool, str, str]] = {}
        for r in reqs:
            r["is_deletion"], label, reason = intent.decide(r["request_text"], fallback)
            self._intent[r["case_id"]] = (r["is_deletion"], label, reason)
            r["intent"] = label
            if review and self.client and not reason.startswith("model"):
                rule_label, _ = intent.classify(r["request_text"])
                d = intent_llm.second_opinion(self.client, r["request_text"], rule_label)
                if d:
                    self.disagreements.append({"case_id": r["case_id"], **d})
            r["uid"], _ = self.identity.resolve_email(r["email"], require_ts(r["audit_at"]))
        self.linker = Linker(self.identity, self.store, self.activity, reqs, self.adjudicator)
        if agent and isinstance(self.adjudicator, LinkInvestigator):
            self.adjudicator.bind(self.store, self.identity, self.linker)

        # Stage A: which cases already show a violation from identifier-linked evidence alone.
        # Records that carry no identifier at all (anonymised or deleted ones) can only belong to
        # a case that shows no violation, which narrows the worker-row alignment in stage B.
        violating: set[str] = set()
        for r in reqs:
            if r["is_deletion"] and r["uid"]:
                linked = self.linker.link_evidence(r, r["uid"])
                if any(not a.compliant for a in self._assess(r, r["uid"], linked, [])):
                    violating.add(r["case_id"])
        self.linker.violating = violating

    def _assess(self, req: Request, uid: str, linked: Links, trail: list[str]) -> list[Assessment]:
        audit_at = require_ts(req["audit_at"])
        subject_ids = self.identity.identifiers_for(uid, audit_at)
        subject_files = {r.split(":", 1)[1] for rs in linked.values() for r in rs if r.startswith("identifier:")}
        assessments: list[Assessment] = []
        unverified: list[tuple[str, str, str | None]] = []
        for key, reasons in sorted(linked.items()):
            system, rid = key
            category = resource_category(self.store.by_resource.get(key, ()), rid)
            omit_ok = policy.required_action(category, self.rules) == "DELETE"
            state = resolve_state(self.store, key, audit_at, category, omit_ok, subject_files)
            shared = len(self.linker.owners_at(rid, audit_at)) > 1
            a = policy.assess(system, rid, category, state, self.rules, subject_ids, audit_at, shared)
            trail.append(f"{rid} [{category}] {state.status}/{state.via}@{state.evidence_id or '-'} "
                         f"via {reasons[0]} -> {a.required + (' ok' if a.compliant else ' VIOLATION') if a else 'no requirement'}")
            if a:
                a.basis, a.via = ledger.basis_of(reasons), state.via
                assessments.append(a)
            else:
                unverified.append((system, rid, category))
        self.unverified[req["case_id"]] = unverified
        return assessments

    def degraded_for(self, case_id: str) -> list[str]:
        """Partly unreadable evidence files that document the case or hold a record linked to it."""
        if not self.store.degraded:
            return []
        files = set(self.linker.case_files.get(case_id, ()))
        for key in self.linked.get(case_id, ()):
            files |= {o.evidence_id for o in self.store.by_resource.get(key, ())}
        return sorted(files & set(self.store.degraded))

    def citable(self) -> dict[str, datetime | None]:
        ids: dict[str, datetime | None] = {fid: r["captured_at_dt"] for fid, r in self.manifest.items()}
        ids.update({a["activity_id"]: a["event_time_dt"] for a in self.activity})
        for events in self.identity.audit_events.values():
            for e in events:
                ids[e["audit_id"]] = parse_ts(e["event_time"])
        return ids

    # ------------------------------------------------------------------
    def audit(self, req: Request) -> tuple[str, list[Step], list[str]]:
        """Return (status, steps, trail); the uncertainty ledger entry is left in self.meta[case_id]."""
        trail: list[str] = []
        is_deletion, label, reason = self._intent.get(req["case_id"]) or intent.decide(req["request_text"])
        trail.append(f"intent={label} ({reason})")
        assessments: list[Assessment] = []

        def done(status: str, steps: list[Step]) -> tuple[str, list[Step], list[str]]:
            steps = order_steps(steps) if steps else []
            degraded = self.degraded_for(req["case_id"])
            if degraded:
                trail.append("degraded evidence (items were skipped while parsing): " + ", ".join(degraded))
            self.meta[req["case_id"]] = ledger.case_meta(
                status, steps, assessments, self.linker.unresolved.get(req["case_id"], []), label, degraded,
                [f"{rid} [{cat}]" for _, rid, cat in self.unverified.get(req["case_id"], [])])
            return status, steps, trail

        if not is_deletion:
            return done("NOT_APPLICABLE", [])

        audit_at = require_ts(req["audit_at"])
        uid, candidates = self.identity.resolve_email(req["email"], audit_at)
        trail.append(f"subject={uid} candidates={candidates}")
        if uid is None:
            return done("INCOMPLETE", [])

        linked = self.linker.link(req, uid)
        self.linked[req["case_id"]] = sorted(linked)
        assessments = self._assess(req, uid, linked, trail)

        violations = [a for a in assessments if not a.compliant]
        steps = []
        if not violations:
            for a in assessments:
                if a.evidence_id:
                    steps.append(make_step(a.verification, a.system, a.resource_id, a.evidence_id))
            if not steps:
                # Nothing evidenced the state of any resource (its proof may sit in an image still
                # to be extracted). A completion claim needs proof, so do not make one.
                trail.append("unresolved: no evidence-backed state")
                return done("INCOMPLETE", [])
            return done("COMPLETE", steps)

        for a in violations:
            if not a.evidence_id:  # a finding must cite a file that existed at audit_at
                trail.append(f"dropped finding {a.resource_id}: no citable evidence")
                continue
            steps.append(make_step(a.finding, a.system, a.resource_id, a.evidence_id))
            steps.append(make_step(a.remediation, a.system, a.resource_id, ""))
            steps.append(make_step(a.verification, a.system, a.resource_id, ""))
        # the account and every retained record are always re-verified: every labelled INCOMPLETE answer has its account
        # check. That check is future work (no evidence_id), so a linked one whose state no evidence shows gets it too.
        for a in assessments:
            if a.compliant and a.category in REVERIFIED:
                steps.append(make_step(a.verification, a.system, a.resource_id, ""))
        for system, rid, category in self.unverified.get(req["case_id"], []):
            op = REVERIFY_OP.get(policy.required_action(category, self.rules) or "") if category in REVERIFIED else None
            if op:
                steps.append(make_step(op, system, rid, ""))
        return done("INCOMPLETE", steps)
