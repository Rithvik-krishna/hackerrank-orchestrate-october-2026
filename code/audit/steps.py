"""Step construction, canonical ordering and serialisation."""

import json

from .model import OPERATION_ORDER, OPERATIONS, PHASES
from .shapes import Step


def make_step(operation: str | None, system_id: str, resource_id: str, evidence_id: str = "") -> Step:
    if operation is None:
        raise ValueError("the assessment has no such operation")
    phase, state = OPERATIONS[operation]
    return {
        "step_no": 0,
        "phase": phase,
        "system_id": system_id,
        "resource_id": resource_id,
        "operation": operation,
        "state": state,
        "evidence_id": evidence_id,
    }


def order_steps(steps: list[Step]) -> list[Step]:
    """Sort by phase, then the spec's operation order, then system_id and resource_id;
    drop exact duplicates and renumber from 1."""
    unique: dict[tuple[str, str, str], Step] = {}
    for s in steps:
        unique.setdefault((s["operation"], s["system_id"], s["resource_id"]), s)
    ordered = sorted(
        unique.values(),
        key=lambda s: (
            PHASES.index(s["phase"]),
            OPERATION_ORDER[s["operation"]],
            s["system_id"],
            s["resource_id"],
        ),
    )
    for i, s in enumerate(ordered, 1):
        s["step_no"] = i
    return ordered


def dumps_steps(steps: list[Step]) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
