"""Uncertainty ledger: how firmly each answer rests on evidence.

Every record in a case's answer was tied to the subject in some way, and every state was either seen in evidence or
inferred. Those ways are not equally strong, and a few of them are tie-breaks (case order, worker timing). The ledger
labels each resource, rolls the labels up to each case, and lists what may be missing, so the answer says how much it
can be trusted instead of only what it concludes.

basis of a link (strongest first)
  evidence             an identifier in evidence at or before the cutoff names the subject
  catalog / chain      a dated catalog link, or a chain through a source record
  alignment:*          an identifier-less record attributed through the deletion worker's activity:
      unique, unique-class   the only candidate row (of the right class)       -> medium
      llm, agent             chosen by the model, guardrails passed             -> medium
      relaxed                the only row left once the reported status is ignored -> low
      order                  one of several equally fitting rows, picked by case order -> low
state basis
  observed             a record in evidence shows it
  inferred absence     a table snapshot that should have listed the record does not                  -> medium

case confidence = the weakest basis among the steps in the answer; unresolved ambiguity (records that may be missing
from the answer) caps it at medium; an answer that relies on a partly unreadable evidence file (a "degraded" parse, see
audit/diagnostics.py) is capped at medium too and names the files in `degraded_evidence`; an answer with no
evidence-backed state at all is low. A linked record whose state no evidence shows is left out of the answer (the engine
cannot assert what it cannot see), so it caps the case at medium and is listed in `unverified_records`.
"""

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from .shapes import Step

if TYPE_CHECKING:
    from .policy import Assessment

HIGH, MEDIUM, LOW = "high", "medium", "low"
RANK = {"evidence": 0, "catalog": 0, "chain": 0,
        "alignment:unique": 1, "alignment:unique-class": 1, "alignment:llm": 1, "alignment:agent": 1,
        "alignment:relaxed": 2, "alignment:order": 2}
LEVEL = {0: HIGH, 1: MEDIUM, 2: LOW}


def basis_of(reasons: list[str]) -> str:
    """The strongest way a resource was linked, from the linker's reason strings."""
    labels = []
    for r in reasons:
        kind = r.split(":", 1)[0]
        if kind == "identifier":
            labels.append("evidence")
        elif kind == "catalog":
            labels.append("catalog")
        elif kind == "source_of":
            labels.append("chain")
        elif kind == "worker_alignment":
            labels.append("alignment:" + (r.split(":")[2] if r.count(":") >= 2 else "order"))
    return min(labels, key=lambda b: RANK.get(b, 2)) if labels else "evidence"


def case_meta(status: str, steps: list[Step], assessments: list["Assessment"], unresolved: list[str], intent_label: str | None,
              degraded: list[str] | None = None, unverified: list[str] | None = None) -> dict:
    """Roll the resource labels up to one case. `assessments` carry .resource_id, .basis, .via.
    `degraded` lists evidence files the case relies on that were only partly readable (audit/diagnostics.py);
    `unverified` lists linked records whose state no evidence shows, which the answer leaves out."""
    by_rid = {a.resource_id: a for a in assessments}
    counts = {"evidence": 0, "inferred": 0, "tie_break": 0, "other": 0}
    rank, tie_break_resources, labels = 0, [], {}
    for s in steps:
        a = by_rid.get(s["resource_id"])
        if a is None:
            continue
        label = a.basis + (" +inferred absence" if a.via == "omission" else "")
        labels[s["resource_id"]] = label
        r = RANK.get(a.basis, 2)
        if a.via == "omission":
            r = max(r, 1)
        rank = max(rank, r)
        if a.basis in ("alignment:order", "alignment:relaxed"):
            counts["tie_break"] += 1
            tie_break_resources.append(s["resource_id"])
        elif a.via == "omission" or a.basis.startswith("alignment"):
            counts["inferred"] += 1
        else:
            counts["evidence"] += 1
    level = LEVEL[rank]
    notes = []
    if status == "NOT_APPLICABLE":
        level = HIGH if intent_label in ("DELETE", "NOT_DELETE") else MEDIUM
        if intent_label not in ("DELETE", "NOT_DELETE"):
            notes.append("intent decided by the model or by default")
    elif not steps:
        level = LOW
        notes.append("no evidence-backed state for any record")
    if unresolved and status != "NOT_APPLICABLE":
        if level == HIGH:
            level = MEDIUM
        notes.append("may be missing records: " + "; ".join(sorted(set(unresolved))))
    if degraded and status != "NOT_APPLICABLE":
        if level == HIGH:
            level = MEDIUM
        notes.append("relies on partly unreadable evidence: " + ", ".join(sorted(degraded)))
    if unverified and status != "NOT_APPLICABLE":
        if level == HIGH:
            level = MEDIUM
        notes.append("linked but unverifiable, left out: " + ", ".join(sorted(unverified)))
    return {"confidence": level, "counts": counts, "labels": labels, "degraded": sorted(degraded or []),
            "unverified": sorted(unverified or []),
            "tie_break_resources": sorted(set(tie_break_resources)), "unresolved": sorted(set(unresolved)), "notes": notes}


FIELDS = ["case_id", "deletion_status", "confidence", "steps", "steps_evidence", "steps_inferred", "steps_tie_break",
          "tie_break_resources", "unresolved", "degraded_evidence", "unverified_records"]


def write_csv(path: Path, rows: list[dict], metas: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for r in rows:
            m = metas[r["case_id"]]
            c = m["counts"]
            w.writerow([r["case_id"], r["deletion_status"], m["confidence"], sum(c.values()), c["evidence"], c["inferred"],
                        c["tie_break"], " ".join(m["tie_break_resources"]), " | ".join(m["unresolved"]),
                        " ".join(m["degraded"]), " ".join(m["unverified"])])
