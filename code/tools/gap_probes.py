"""Reproducible probes behind the claims in evaluation/rubric_audit.md (read-only; nothing is written).

    python code/tools/gap_probes.py pdfs [id ...]    decoded text of the PDF evidence, searched for ids
    python code/tools/gap_probes.py conventions      do resource numbers follow user numbers anywhere in the evidence?
    python code/tools/gap_probes.py lag              worker-row lag behind reported_at for the labelled samples, with rivals
    python code/tools/gap_probes.py shape            the 518 answers against the shape of the labelled ones
    python code/tools/gap_probes.py sensitivity      how far the answers move when the tuned alignment windows change

`lag` and `shape` read the labelled samples (dataset/sample_output.csv) to measure the engine, never to produce an answer.
"""

import collections
import csv
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit import linker, loaders, paths  # noqa: E402
from audit.evidence import EvidenceStore, pdf_text  # noqa: E402
from audit.pipeline import Pipeline  # noqa: E402
from audit.steps import dumps_steps  # noqa: E402

ROOT = paths.DATASET.parent


def read_gold() -> dict[str, list[dict]]:
    with open(paths.DATASET / "sample_output.csv", encoding="utf-8", newline="") as fh:
        return {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(fh)}


def probe_pdfs(needles: list[str]) -> None:
    for pdf in sorted((paths.EVIDENCE_ROOT / "evidence" / "documents").glob("*.pdf")):
        lines = pdf_text.extract_lines(pdf)
        print(f"=== {pdf.name}: {len(lines)} lines")
        for line in lines:
            if needles and any(n in line for n in needles):
                print("   ", line[:200])


def probe_conventions() -> None:
    pairs: dict[str, set[tuple[int, int]]] = collections.defaultdict(set)
    for o in EvidenceStore().load().observations:
        m = re.match(r"([a-z_]+)_(\d+)$", o.resource_id)
        for ident in (o.identifiers | o.link_keys) if m else ():
            u = loaders.norm_user(ident)
            if u and m:
                pairs[m.group(1)].add((int(m.group(2)), int(u.split("_")[1])))
    for prefix, ps in sorted(pairs.items()):
        print(f"{prefix:14s} pairs {len(ps):4d}  resource number == user number: {sum(1 for r, u in ps if r == u):4d}")


def probe_lag() -> None:
    gold = read_gold()
    rows = [a for a in loaders.load_activity() if linker._is_account_delete(a)]
    by_rid = {a["resource_id"]: a for a in rows}
    lo, hi = linker.ACCOUNT_WINDOW
    for req in loaders.load_requests(sample=True):
        rep = loaders.require_ts(req["reported_at"])
        for rid in dict.fromkeys(s["resource_id"] for s in gold[req["case_id"]] if s["system_id"] == "account_service"):
            a = by_rid.get(rid)
            lag = f"{(rep - a['event_time_dt']).total_seconds() / 60:6.1f}" if a else "   n/a"
            rivals = [(b["resource_id"], round((rep - b["event_time_dt"]).total_seconds() / 60, 1)) for b in rows
                      if b["resource_id"] != rid and rep + timedelta(hours=lo) <= b["event_time_dt"] <= rep + timedelta(hours=hi)]
            print(f"{req['case_id']:8s} {rid:14s} lag {lag} min  rivals {rivals[:4]}")


def _shape(rows: dict[str, tuple[str, list]]) -> dict:
    counts = collections.Counter(status for status, _ in rows.values())
    sizes: dict[str, list[int]] = collections.defaultdict(list)
    for status, steps in rows.values():
        sizes[status].append(len(steps))
    empty = sum(1 for status, steps in rows.values() if status == "INCOMPLETE" and not steps)
    return {"n": len(rows), "counts": dict(counts), "mean_steps": {k: round(sum(v) / len(v), 1) for k, v in sizes.items()}, "incomplete_without_steps": empty}


def probe_shape() -> None:
    with open(paths.DATASET / "sample_output.csv", encoding="utf-8", newline="") as fh:
        gold = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}
    with open(ROOT / "output.csv", encoding="utf-8", newline="") as fh:
        ours = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}
    print("labelled samples:", _shape(gold))
    print("our 518 answers: ", _shape(ours))


def _answers() -> dict[str, tuple[str, list]]:
    pipe = Pipeline()
    out = {}
    for req in loaders.load_requests() + loaders.load_requests(sample=True):
        status, steps, _ = pipe.audit(req)
        out[req["case_id"]] = (status, json.loads(dumps_steps(steps)))
    return out


def probe_sensitivity() -> None:
    gold = read_gold()
    base_windows = (linker.ACCOUNT_WINDOW, linker.POLICY_WINDOW)
    base = _answers()
    print("baseline windows", base_windows, "| samples exactly right:", sum(base[c][1] == s for c, s in gold.items()), "of 32")
    grid = [("account x0.5", ((-0.75, 0.125), base_windows[1])), ("account x2", ((-3.0, 0.5), base_windows[1])),
            ("policy x0.5", (base_windows[0], (-0.5, 1.25))), ("policy x2", (base_windows[0], (-2.0, 5.0))),
            ("both x0.5", ((-0.75, 0.125), (-0.5, 1.25))), ("both x2", ((-3.0, 0.5), (-2.0, 5.0)))]
    try:
        for name, (acc, pol) in grid:
            linker.ACCOUNT_WINDOW, linker.POLICY_WINDOW = acc, pol
            res = _answers()
            changed = [c for c in res if c not in gold and res[c] != base[c]]
            print(f"{name:13s} samples right {sum(res[c][1] == s for c, s in gold.items()):2d}/32 | statuses changed {sum(res[c][0] != base[c][0] for c in res):3d} | of the 518, answers changed {len(changed)}")
    finally:
        linker.ACCOUNT_WINDOW, linker.POLICY_WINDOW = base_windows


def main() -> int:
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    table = {"pdfs": lambda: probe_pdfs(rest), "conventions": probe_conventions, "lag": probe_lag, "shape": probe_shape,
             "sensitivity": probe_sensitivity}
    if cmd not in table:
        print(__doc__)
        return 2
    table[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
