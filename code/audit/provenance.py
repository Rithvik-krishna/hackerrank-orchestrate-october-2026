"""Provenance graph: how a case's answer is connected to its sources.

For one audited case the graph holds typed nodes and typed, timestamped edges from the request to each step:

  request  --resolves_to-->      subject                (edge says which identifier matched and why it was valid then)
  subject  --has_identifier-->   identifier             (kind, valid_from, source: user_identities.csv or an audit_id)
  identifier --matched_in-->     observation            (an evidence record that carries it)
  observation --observes-->      resource
  observation --recorded_in-->   evidence file/activity (with the file's capture time)
  resource --governed_by-->      rule                   (deletion_rules.csv row, required action)
  resource --links_via-->        ...                    (how it was tied to the subject: evidence, catalog, chain, alignment)
  resource --yields-->           step
  evidence --degraded_by-->      diagnostic             (an item of that file was skipped while parsing; audit/diagnostics.py)

Observations that were *not* used are kept, with the reason: recorded after audit_at, recorded before the request,
in a file captured after audit_at. Conflicting observations of one record (an earlier sighting against a later one, a
replica against its primary) are kept as `conflicts`, with which one decided the state and why.

The graph is read-only and is built from the pipeline's own stores, so every path in it is one the engine actually used.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from collections import defaultdict
from datetime import datetime

from .loaders import require_ts
from .shapes import Request, ResourceKey, Step
from .state import resolve_state, resource_category

if TYPE_CHECKING:
    from .pipeline import Pipeline


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.conflicts: list[dict[str, Any]] = []
        self.excluded: list[dict[str, Any]] = []

    def node(self, ntype: str, key: str, **attrs: Any) -> str:
        nid = f"{ntype}:{key}"
        self.nodes.setdefault(nid, {"id": nid, "type": ntype, **attrs})
        return nid

    def edge(self, src: str, rel: str, dst: str, **attrs: Any) -> None:
        self.edges.append({"from": src, "rel": rel, "to": dst, **attrs})

    def paths_to(self, step_node: str) -> list:
        """Every chain of edges that ends in `step_node` (reverse walk), for the readable explanation."""
        into = defaultdict(list)
        for e in self.edges:
            into[e["to"]].append(e)
        out = []

        def walk(n: str, trail: list) -> None:
            parents = into.get(n, [])
            if not parents:
                out.append(trail)
            for e in parents:
                walk(e["from"], [e] + trail)

        walk(step_node, [])
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.nodes.values()), "edges": self.edges, "conflicts": self.conflicts, "excluded": self.excluded}


def build(pipe: "Pipeline", req: Request) -> tuple[str, list[Step], "Graph"]:
    """Audit `req` and return (status, steps, Graph)."""
    status, steps, _ = pipe.audit(req)
    g = Graph()
    audit_at, requested_at = require_ts(req["audit_at"]), require_ts(req["requested_at"])
    uid, _ = pipe.identity.resolve_email(req["email"], audit_at)
    rq = g.node("request", req["case_id"], email=req["email"], audit_at=req["audit_at"], requested_at=req["requested_at"],
                text=req["request_text"][:80])
    if uid is None:
        return status, steps, g
    subject = g.node("subject", uid)
    facts = pipe.identity.identifier_facts(uid, audit_at)
    email = req["email"].strip().lower()
    for kind, value, valid_from, source in facts:
        ident = g.node("identifier", value, kind=kind)
        g.edge(subject, "has_identifier", ident, valid_from=valid_from.isoformat() if valid_from else None, source=source)
        if value == email:
            g.edge(rq, "resolves_to", subject, via=ident, basis="exact match, case-insensitive",
                   valid_from=valid_from.isoformat() if valid_from else None, source=source)
    subject_ids = {v for _, v, _, _ in facts}

    for key, reasons in sorted(pipe.linker.link(req, uid).items()):
        _add_resource(g, pipe, key, reasons, subject, uid, subject_ids, audit_at, requested_at)
    for fid in pipe.degraded_for(req["case_id"]):   # partly unreadable evidence this case relies on
        ev = g.node("evidence", fid)
        g.nodes[ev]["degraded"] = True
        for i, d in enumerate(pipe.store.degraded[fid]):
            g.edge(ev, "degraded_by", g.node("diagnostic", f"{fid}#{i}", parser=d.parser, location=d.location,
                                             category=d.category, message=d.message))
    for s in steps:
        sn = g.node("step", f"{s['step_no']}:{s['operation']}:{s['resource_id']}", **{k: s[k] for k in ("phase", "operation", "evidence_id")})
        g.edge(g.node("resource", s["resource_id"]), "yields", sn)
        if s["evidence_id"]:
            g.edge(g.node("evidence", s["evidence_id"]), "cited_by", sn)
    return status, steps, g


def _add_resource(g: "Graph", pipe: "Pipeline", key: ResourceKey, reasons: list[str], subject: str, uid: str, subject_ids: set[str],
                  audit_at: datetime, requested_at: datetime) -> None:
    """One linked record: its rule, why it is linked, which observations count, and any conflict between them."""
    system, rid = key
    category = resource_category(pipe.store.by_resource.get(key, ()), rid)
    owners = sorted(pipe.linker.owners_at(rid, audit_at) - {uid})
    res = g.node("resource", rid, system=system, category=category, **({"shared_with": owners} if owners else {}))
    rule = pipe.rules.get(category) if category else None
    if rule:
        g.edge(res, "governed_by", g.node("rule", rule["rule_id"], action=rule["required_action"], condition=rule["condition"]))
    for r in reasons:
        g.edge(subject, "links_via", res, basis=r)
    state = resolve_state(pipe.store, key, audit_at, category, omit_ok=rule is not None and rule["required_action"] == "DELETE")
    decided = f"{state.evidence_id or '-'} ({state.via}); status {state.status}"
    obs = sorted(pipe.store.by_resource.get(key, ()), key=lambda o: (o.observed_at, o.evidence_id))
    for o in obs:
        why = None
        if o.observed_at > audit_at:
            why = "recorded after audit_at"
        elif o.observed_at < requested_at and not any(r.startswith(("catalog", "worker", "source")) for r in reasons):
            why = "recorded before the request (cannot link on its own)"
        if why:
            g.excluded.append({"resource": rid, "evidence": o.evidence_id, "observed_at": o.observed_at.isoformat(),
                               "status": o.status, "reason": why})
            continue
        ev = g.node("evidence", o.evidence_id, captured_at=o.file_time.isoformat() if o.file_time else None,
                    citable=o.file_time is None or o.file_time <= audit_at)
        ob = g.node("observation", f"{o.evidence_id}/{rid}@{o.observed_at.isoformat()}", status=o.status, source=o.source,
                    table=o.attrs.get("table"))
        g.edge(ob, "observes", res)
        g.edge(ob, "recorded_in", ev)
        for ident in sorted(o.identifiers | o.link_keys):
            if ident in subject_ids:
                g.edge(g.node("identifier", ident), "matched_in", ob)
    used = [o for o in obs if o.observed_at <= audit_at]
    _add_conflicts(g, pipe, system, rid, used, decided)


def _add_conflicts(g: "Graph", pipe: "Pipeline", system: str, rid: str, used: list, decided: str) -> None:
    """Replica drift (one table shows the record, another table of the same snapshot does not) and status disagreements."""
    for o in used:
        if o.status != "PRESENT":
            continue
        for c in pipe.store.coverage:
            if (c.evidence_id == o.evidence_id and c.system_id == system and c.table != o.attrs.get("table")
                    and c.observed_at == o.observed_at and rid not in c.resource_ids):
                g.conflicts.append({"resource": rid, "kind": "replica drift",
                                    "observations": [(o.evidence_id, o.observed_at.isoformat(), "PRESENT", o.attrs.get("table")),
                                                     (c.evidence_id, c.observed_at.isoformat(), "NOT LISTED", c.table)],
                                    "decided": decided,
                                    "rule": "a replica that still shows the record outranks a primary that does not"})
    if len({o.status for o in used}) > 1:
        g.conflicts.append({"resource": rid, "observations": [(o.evidence_id, o.observed_at.isoformat(), o.status, o.attrs.get("table")) for o in used],
                            "decided": decided,
                            "rule": "latest record wins; within one snapshot a replica that still shows the record outranks a primary that does not"})


def explain(g: "Graph", steps: list[Step], out: Callable[[str], object] = print) -> None:
    """Readable paths, one per step: request -> subject -> identifier -> observation -> resource -> rule -> step."""
    for s in steps:
        node = f"step:{s['step_no']}:{s['operation']}:{s['resource_id']}"
        out(f"step {s['step_no']} {s['operation']} {s['resource_id']}  [{s['evidence_id'] or 'future work'}]")
        res = f"resource:{s['resource_id']}"
        for e in g.edges:
            if e["to"] == res and e["rel"] == "links_via":
                out(f"    linked: {e['basis']}")
            if e["to"] == res and e["rel"] == "observes":
                ob = g.nodes[e["from"]]
                out(f"    seen:   {ob['id'].split(':', 1)[1]} status={ob['status']}")
            if e["from"] == res and e["rel"] == "governed_by":
                r = g.nodes[e["to"]]
                out(f"    rule:   {r['id'].split(':', 1)[1]} -> {r['action']} ({r['condition']})")
    if g.conflicts:
        out("conflicting observations:")
        for c in g.conflicts:
            out(f"    {c['resource']}: {c['observations']} -> decided {c['decided']}")
    degraded = [e for e in g.edges if e["rel"] == "degraded_by"]
    if degraded:
        out("degraded evidence (skipped while parsing; it cannot prove absence and caps confidence at medium):")
        for e in degraded:
            d = g.nodes[e["to"]]
            out(f"    {e['from'].split(':', 1)[1]} {d['location']}: {d['category']}")
    if g.excluded:
        out("observations not used:")
        for x in g.excluded:
            out(f"    {x['resource']} {x['evidence']} {x['observed_at']} {x['status']}: {x['reason']}")
