"""Independent verification of the generated answers.

The decision engine (linker, state, policy, steps) produces a status and steps. This module does not trust them. It
re-checks every answer from the raw evidence index and the rules table, using its own, simpler code path (no
`state.resolve_state`, no `policy.assess`), and reports what it rejects. It never repairs anything.

What it takes from the engine is only *which records were linked to the case* (the linking is what the engine is for);
everything the answer claims about those records is re-derived here.

Checks (severity error unless noted):
  schema      contract: statuses, operations/phases/states, numbering, evidence rules (via validate.validate_rows)
  evidence    every cited id exists and was available at audit_at; a cited file must actually contain the resource and
              show the state the step claims (present, absent, exposed, clean, active, disabled, restorable, blocked)
  chain       every finding is followed by the remediation and verification the rules require for that record's
              category, no remediation exists without a finding, remediation and verification of future work cite nothing
  order       steps are in the canonical order and numbered 1..n
  status      COMPLETE holds only evidence-backed verification steps; NOT_APPLICABLE is empty and the request text is not
              a deletion request; INCOMPLETE has at least one finding (warn if it is empty because nothing could be proven)
  false-complete  a COMPLETE answer is rejected if any linked record is, by the latest evidence at audit_at, still present
              (delete categories), still exposing the subject (redact/anonymise), an active flow, or a restorable backup
  coverage    an INCOMPLETE answer must have a finding for every such record
  retention   retained records (audit, legal hold) seen in evidence must be verified present (warn if retention has lapsed)
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import intent
from .evidence.common import category_for
from .identity import IdentityIndex
from .loaders import classify_identifier, parse_ts, require_ts
from .model import ABSENT, ACTIVE, BLOCKED, DISABLED, PRESENT, RESTORABLE, UNKNOWN, Observation
from .shapes import ActivityRow, ManifestRow, Request, ResourceKey, RuleRow, Step
from .steps import order_steps
from .validate import validate_rows

if TYPE_CHECKING:
    from .evidence import EvidenceStore

NAME_FIELDS = {"name", "full_name", "display_name", "requester_name"}
CHAIN = {  # finding -> (remediation(s), verification(s))
    "REPORT_REMAINING_RECORD": ({"DELETE_RECORD"}, {"VERIFY_RECORD_ABSENT"}),
    "REPORT_EXPOSED_PII": ({"REDACT_RECORD", "ANONYMIZE_RECORD"}, {"VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED"}),
    "REPORT_ACTIVE_DATA_FLOW": ({"DISABLE_DATA_FLOW"}, {"VERIFY_DATA_FLOW_DISABLED"}),
    "REPORT_RESTORE_RISK": ({"BLOCK_RESTORE"}, {"VERIFY_RESTORE_BLOCKED"}),
}
EXPECT = {  # what a cited file must show for each operation
    "REPORT_REMAINING_RECORD": "present", "REPORT_EXPOSED_PII": "exposed", "REPORT_ACTIVE_DATA_FLOW": "active",
    "REPORT_RESTORE_RISK": "restorable", "VERIFY_RECORD_ABSENT": "absent", "VERIFY_RECORD_PRESENT": "present",
    "VERIFY_RECORD_REDACTED": "clean_redact", "VERIFY_RECORD_ANONYMIZED": "clean_anon",
    "VERIFY_DATA_FLOW_DISABLED": "disabled", "VERIFY_RESTORE_BLOCKED": "blocked",
}
REMEDIATION_FOR_ACTION = {"DELETE": "DELETE_RECORD", "REDACT": "REDACT_RECORD", "ANONYMIZE": "ANONYMIZE_RECORD"}
RETAINED = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}


@dataclass
class Finding:
    case_id: str
    severity: str     # "error" | "warn"
    code: str
    message: str


def _exposed(obs: Observation, subject_ids: set[str], anonymize: bool) -> bool:
    """Direct identifiers of the subject visible in an observation."""
    if any(v for k, v in obs.attrs.get("pii_fields", {}).items() if k in NAME_FIELDS):
        return True
    for ident in obs.identifiers:
        c = classify_identifier(ident)
        if c and c[1] in subject_ids and (anonymize or c[0] != "user"):
            return True
    return False


class Verifier:
    def __init__(self, store: "EvidenceStore", manifest: dict[str, ManifestRow], activity: list[ActivityRow], rules: dict[str, RuleRow], identity: IdentityIndex, citable: dict[str, datetime | None]) -> None:
        self.rules, self.identity, self.citable, self.manifest = rules, identity, citable, manifest
        self.by_file: dict = defaultdict(lambda: defaultdict(list))          # evidence_id -> resource_id -> observations
        self.by_resource: dict = defaultdict(list)
        for o in store.observations:
            self.by_file[o.evidence_id][o.resource_id].append(o)
            self.by_resource[o.resource_id].append(o)
        self.coverage: dict = defaultdict(list)                              # evidence_id -> coverage entries
        for c in store.coverage:
            self.coverage[c.evidence_id].append(c)
        self.dumps = [c for c in store.coverage if c.scope == "full" and c.resource_ids]   # complete table dumps

    # ------------------------------------------------------------------ helpers
    def _category(self, rid: str, obs: list[Observation]) -> str | None:
        cats = [o.category for o in obs if o.category]
        return (cats[0] if cats else None) or category_for(rid)

    def _action(self, category: str | None) -> str | None:
        if category == "DATA_FLOW":
            return "DISABLE"
        if category == "BACKUP_ARCHIVE":
            return "BLOCK"
        rule = self.rules.get(category) if category is not None else None
        return rule["required_action"] if rule else None

    def _supports(self, evidence_id: str, system: str, rid: str, expect: str, audit_at: datetime,
                  subject_ids: set[str]) -> tuple[bool, str]:
        """(ok, why): does the cited file, at audit_at, show what the step claims?"""
        obs = [o for o in self.by_file[evidence_id].get(rid, []) if o.observed_at <= audit_at]
        obs = [o for o in obs if o.status != UNKNOWN] or obs   # an observation with no state does not supersede one that has
        if not obs:
            if expect == "absent" and any(c.system_id == system and c.observed_at <= audit_at for c in self.coverage[evidence_id]):
                return True, "absent by omission from a snapshot of that system"
            return False, f"{evidence_id} does not contain {rid} at or before audit_at"
        latest = max(o.observed_at for o in obs)
        now = [o for o in obs if o.observed_at == latest]
        statuses = {o.status for o in now}
        ok = {
            "present": PRESENT in statuses,
            "absent": ABSENT in statuses and PRESENT not in statuses,
            "exposed": any(o.status == PRESENT and _exposed(o, subject_ids, False) for o in now),
            "clean_redact": PRESENT in statuses and not any(_exposed(o, subject_ids, False) for o in now),
            "clean_anon": PRESENT in statuses and not any(_exposed(o, subject_ids, True) for o in now),
            "active": ACTIVE in statuses,
            "disabled": DISABLED in statuses and ACTIVE not in statuses,
            "restorable": bool(statuses & {RESTORABLE, ACTIVE}),
            "blocked": BLOCKED in statuses and not (statuses & {RESTORABLE, ACTIVE}),
        }[expect]
        return ok, "" if ok else f"{evidence_id} shows {sorted(statuses)} for {rid}, not '{expect}'"

    # ------------------------------------------------------------------ the checks, one concern each
    def _check_status(self, req: Request, status: str, steps: list[Step]) -> list[tuple[str, str, str]]:
        out = []
        rule_label, _ = intent.classify(req["request_text"])
        if status == "NOT_APPLICABLE":
            if steps:
                out.append(("error", "status", "NOT_APPLICABLE must have no steps"))
            if rule_label == intent.DELETE:
                out.append(("error", "status", "NOT_APPLICABLE but the request text asks for deletion"))
            return out
        if rule_label == intent.NOT_DELETE:
            out.append(("error", "status", "the request text is not a deletion request but the case was audited"))
        if status == "COMPLETE" and any(s["phase"] != "VERIFICATION" or not s["evidence_id"] for s in steps):
            out.append(("error", "status", "COMPLETE must hold only evidence-backed verification steps"))
        if status == "COMPLETE" and not steps:
            out.append(("error", "false-complete", "COMPLETE with no evidence-backed step proves nothing"))
        if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
            out.append(("warn", "status", "INCOMPLETE without a finding: nothing could be proven either way"))
        return out

    def _check_order(self, steps: list[Step]) -> list[tuple[str, str, str]]:
        if not steps:
            return []
        key = lambda s: (s["phase"], s["operation"], s["system_id"], s["resource_id"])  # noqa: E731
        if [key(s) for s in order_steps([Step(**s) for s in steps])] != [key(s) for s in steps]:
            return [("error", "order", "steps are not in the canonical phase/operation/system/resource order")]
        return []

    def _check_evidence(self, steps: list[Step], audit_at: datetime, subject_ids: set[str]) -> list[tuple[str, str, str]]:
        out = []
        for s in steps:
            ev = s["evidence_id"]
            if not ev:
                continue
            if ev not in self.citable:
                out.append(("error", "evidence", f"step {s['step_no']} cites unknown id {ev}"))
            elif (available := self.citable[ev]) and available > audit_at:
                out.append(("error", "evidence", f"step {s['step_no']} cites {ev}, which did not exist at audit_at"))
            elif ev in self.manifest and s["operation"] in EXPECT:   # remediation cannot be backed by evidence (see chain)
                ok, why = self._supports(ev, s["system_id"], s["resource_id"], EXPECT[s["operation"]], audit_at, subject_ids)
                if not ok:
                    out.append(("error", "evidence", f"step {s['step_no']} {s['operation']} {s['resource_id']}: {why}"))
        return out

    def _check_chain(self, steps: list[Step]) -> list[tuple[str, str, str]]:
        out = []
        findings = [s for s in steps if s["phase"] == "FINDING"]
        by_key: dict = defaultdict(list)
        for s in steps:
            by_key[(s["system_id"], s["resource_id"])].append(s)
        for f in findings:
            rid = f["resource_id"]
            rems, vers = CHAIN[f["operation"]]
            rem = [s for s in by_key[(f["system_id"], rid)] if s["phase"] == "REMEDIATION"]
            ver = [s for s in by_key[(f["system_id"], rid)] if s["phase"] == "VERIFICATION"]
            if not any(r["operation"] in rems for r in rem):
                out.append(("error", "chain", f"{rid}: no {'/'.join(sorted(rems))} for the finding {f['operation']}"))
            if not any(v["operation"] in vers for v in ver):
                out.append(("error", "chain", f"{rid}: no {'/'.join(sorted(vers))} after remediation"))
            if any(s["evidence_id"] for s in rem + ver):
                out.append(("error", "chain", f"{rid}: future work must not cite evidence"))
            if f["operation"] in ("REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII"):
                cat = self._category(rid, self.by_resource.get(rid, []))
                action = self._action(cat)
                want = REMEDIATION_FOR_ACTION.get(action) if action else None
                if want and rem and not any(r["operation"] == want for r in rem):
                    out.append(("error", "chain", f"{rid} is a {cat}: the rules require {want}"))
        finding_keys = {(f["system_id"], f["resource_id"]) for f in findings}
        for s in steps:
            if s["phase"] == "REMEDIATION" and (s["system_id"], s["resource_id"]) not in finding_keys:
                out.append(("error", "chain", f"remediation of {s['resource_id']} without a finding"))
        return out

    def _linked_problem(self, system: str, rid: str, obs: list[Observation], latest: datetime, audit_at: datetime,
                        subject_ids: set[str]) -> tuple[str | None, str | None]:
        """(category, problem) for one linked record from the latest evidence; problem is None when it complies."""
        now = [o for o in obs if o.observed_at == latest]
        cat = self._category(rid, obs)
        act = self._action(cat)
        if cat == "SUPPORT_ATTACHMENT":  # rule_06: deleted only if it belongs solely to the subject
            owners = {u for o in obs for x in o.identifiers | o.link_keys for u in self.identity.owners(x)}
            if len(owners) > 1:
                return cat, None
        statuses = {o.status for o in now}
        # a later, citable, complete table dump of the same system that no longer lists the record shows it gone,
        # whatever an earlier sighting said (the same absence-by-omission rule as the engine)
        gone = any(c.system_id == system and latest < c.observed_at <= audit_at and (not c.file_time or c.file_time <= audit_at)
                   and rid not in c.resource_ids and category_for("", c.table) == cat for c in self.dumps)
        if act == "DELETE":
            return cat, None if gone or PRESENT not in statuses else "still present"
        if act in ("REDACT", "ANONYMIZE") and any(o.status == PRESENT and _exposed(o, subject_ids, act == "ANONYMIZE") for o in now):
            return cat, "still exposes the subject"
        if act == "DISABLE" and ACTIVE in statuses:
            return cat, "an active data flow"
        if act == "BLOCK" and statuses & {RESTORABLE, ACTIVE}:
            return cat, "a backup that can be restored"
        return cat, None

    def _retention_note(self, now: list[Observation], audit_at: datetime, rid: str) -> list[tuple[str, str, str]]:
        until = next((o.attrs.get("retention_until") or o.attrs.get("retained_until") for o in now
                      if o.attrs.get("retention_until") or o.attrs.get("retained_until")), None)
        when = parse_ts(until) if isinstance(until, str) else until
        if when is not None and when < audit_at:
            return [("warn", "retention", f"retention of {rid} ended {when.date()} before audit_at: it may no longer be retained")]
        return []

    def _check_linked(self, status: str, steps: list[Step], linked: list, audit_at: datetime,
                      subject_ids: set[str]) -> list[tuple[str, str, str]]:
        """False COMPLETE, missing findings and retention, from the latest evidence on every linked record."""
        out = []
        finding_ids = {s["resource_id"] for s in steps if s["phase"] == "FINDING"}
        verified_present = {s["resource_id"] for s in steps if s["operation"] == "VERIFY_RECORD_PRESENT"}
        for system, rid in linked:
            obs = [o for o in self.by_resource.get(rid, []) if o.observed_at <= audit_at]
            obs = [o for o in obs if o.status != UNKNOWN] or obs   # same rule as the engine: no state, no supersession
            if not obs:
                continue
            latest = max(o.observed_at for o in obs)
            cat, problem = self._linked_problem(system, rid, obs, latest, audit_at, subject_ids)
            if problem and status == "COMPLETE":
                out.append(("error", "false-complete", f"COMPLETE but {rid} ({cat}) is {problem} in the latest evidence at audit_at"))
            if problem and status == "INCOMPLETE" and rid not in finding_ids:
                out.append(("error", "coverage", f"{rid} ({cat}) is {problem} but the answer has no finding for it"))
            if cat in RETAINED:
                now = [o for o in obs if o.observed_at == latest]
                if PRESENT in {o.status for o in now} and steps and rid not in verified_present:
                    out.append(("error", "retention", f"retained record {rid} ({cat}) is present but not verified as retained"))
                out += self._retention_note(now, audit_at, rid)
        return out

    # ------------------------------------------------------------------ one case
    def verify_case(self, req: Request, status: str, steps: list[Step], linked: list[ResourceKey]) -> list[Finding]:
        """Everything wrong with one answer, as Findings; an empty list (or warnings only) means it is accepted."""
        cid = req["case_id"]
        audit_at = require_ts(req["audit_at"])
        uid, _ = self.identity.resolve_email(req["email"], audit_at)
        subject_ids = self.identity.identifiers_for(uid, audit_at) if uid else set()
        row = {"case_id": cid, "deletion_status": status, "steps_json": json.dumps(steps)}
        raw = [("error", "schema", e) for e in validate_rows([row], [req], self.citable)]
        raw += self._check_status(req, status, steps)
        if status != "NOT_APPLICABLE":
            raw += (self._check_order(steps) + self._check_evidence(steps, audit_at, subject_ids)
                    + self._check_chain(steps) + self._check_linked(status, steps, linked, audit_at, subject_ids))
        return [Finding(cid, sev, code, msg) for sev, code, msg in raw]
