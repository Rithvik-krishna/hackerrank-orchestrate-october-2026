"""Score predictions against the labelled sample answers.

    python code/evaluation/evaluate.py [predictions.csv] [--gold gold.csv] [--cases a,b] [--diff] [--detail]
    python code/evaluation/evaluate.py --check-regression | --update-baseline

Metrics (all reported separately):
  status       share of cases with the right deletion_status
  row          share of cases whose whole steps_json is identical (JSON parsed: key order and whitespace do not count)
  sequence     share of cases whose ordered (phase, operation, system, resource) list matches
  step P/R/F1  steps compared as (phase, operation, system_id, resource_id, state, evidence_id) sets
  field        aligned by position; each of the 7 fields counted, missing/extra steps count as wrong
  evidence     among steps matched on (operation, resource), share citing the right evidence_id
  false_complete / false_incomplete   cases answered COMPLETE (INCOMPLETE) where the label says otherwise
  na_accuracy  NOT_APPLICABLE precision/recall over the cases where either side says NOT_APPLICABLE
  order        share of predicted answers whose steps are canonically ordered and numbered 1..n
With --detail: per-operation precision/recall and a categorised list of what is missing or spurious.
Regression guard: the set of cases whose answer was exactly right is stored in regression_baseline.json; a case that
was exact and no longer is fails --check-regression.
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
from audit.shapes import Step  # noqa: E402

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
BASELINE = HERE / "regression_baseline.json"


Rows = dict[str, dict[str, str]]   # case_id -> the CSV row of that case


def load(path: Path | str) -> Rows:
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def key6(s: Step) -> tuple[str, ...]:
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"], s["state"], s["evidence_id"])


def key4(s: Step) -> tuple[str, ...]:
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"])


def canonical(steps: list[Step]) -> bool:
    """True when the steps are in the contract's order and numbered 1..n."""
    from audit.steps import order_steps

    again = order_steps([Step(**s) for s in steps])
    return [key4(s) for s in again] == [key4(s) for s in steps] and [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))


def exact_cases(pred: Rows, gold: Rows) -> list[str]:
    return sorted(c for c, g in gold.items() if c in pred and pred[c]["deletion_status"] == g["deletion_status"]
                  and json.loads(pred[c]["steps_json"]) == json.loads(g["steps_json"]))


def field_counts(ps: list, gs: list) -> tuple[int, int]:
    """(matching fields, total fields) over aligned steps; a missing or extra step counts all its fields as wrong."""
    ok = sum(a.get(f) == b[f] for a, b in zip(ps, gs) for f in FIELDS)
    return ok, len(FIELDS) * max(len(ps), len(gs))


def evidence_counts(ps: list, gs: list) -> tuple[int, int]:
    """(correct evidence ids, steps) among predicted steps whose operation/system/resource also appear in the gold."""
    gmap = {(s["operation"], s["system_id"], s["resource_id"]): s["evidence_id"] for s in gs}
    hits = [gmap[k] == s["evidence_id"] for s in ps if (k := (s["operation"], s["system_id"], s["resource_id"])) in gmap]
    return sum(hits), len(hits)


def na_confusion(pred_status: str, gold_status: str) -> dict[str, int]:
    """NOT_APPLICABLE as the positive class: true positive, false positive, false negative."""
    pn, gn = pred_status == "NOT_APPLICABLE", gold_status == "NOT_APPLICABLE"
    return {"tp": int(pn and gn), "fp": int(pn and not gn), "fn": int(gn and not pn)}


def print_diff(cid: str, gold_status: str, pred_status: str | None, gset: set, pset: set, out: Any) -> None:
    out(f"--- {cid}: gold {gold_status} / pred {pred_status}")
    for s in sorted(gset - pset):
        out(f"    missing  {s}")
    for s in sorted(pset - gset):
        out(f"    spurious {s}")


def score(pred: Rows, gold: Rows, only: Collection[str] | None = None, diff: bool = False, out: Callable[[str], object] = print) -> dict[str, Any]:
    cases = [c for c in sorted(gold) if not only or c in only]
    n = len(cases)
    st = row = seq = ordered = 0
    fld_ok = fld_tot = ev_ok = ev_tot = 0
    false_complete = false_incomplete = 0
    na: Counter[str] = Counter()
    per_op: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0, 0])   # operation -> [tp, fp, fn]
    miss_by_op: Counter[str] = Counter()
    spur_by_op: Counter[str] = Counter()
    for cid in cases:
        g, p = gold[cid], pred.get(cid)
        gs = json.loads(g["steps_json"])
        ps = json.loads(p["steps_json"]) if p else []
        s_ok = p is not None and p["deletion_status"] == g["deletion_status"]
        st += s_ok
        row += ps == gs
        q_ok = [key4(s) for s in ps] == [key4(s) for s in gs]
        seq += q_ok
        ordered += canonical(ps)
        if p:
            false_complete += p["deletion_status"] == "COMPLETE" and g["deletion_status"] != "COMPLETE"
            false_incomplete += p["deletion_status"] == "INCOMPLETE" and g["deletion_status"] != "INCOMPLETE"
            na.update(na_confusion(p["deletion_status"], g["deletion_status"]))
        gset, pset = {key6(s) for s in gs}, {key6(s) for s in ps}
        for i, keys in enumerate((gset & pset, pset - gset, gset - pset)):   # tp, fp, fn
            for k in keys:
                per_op[k[1]][i] += 1
        spur_by_op.update(k[1] for k in pset - gset)
        miss_by_op.update(k[1] for k in gset - pset)
        ok, tot = field_counts(ps, gs)
        fld_ok, fld_tot = fld_ok + ok, fld_tot + tot
        ok, tot = evidence_counts(ps, gs)
        ev_ok, ev_tot = ev_ok + ok, ev_tot + tot
        if diff and not (s_ok and q_ok and ps == gs):
            print_diff(cid, g["deletion_status"], p["deletion_status"] if p else None, gset, pset, out)
    tp, fp, fn = (sum(v[i] for v in per_op.values()) for i in range(3))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    na_den_p, na_den_r = na["tp"] + na["fp"], na["tp"] + na["fn"]
    return {
        "cases": n,
        "status_accuracy": st / n,
        "row_exact_match": row / n,
        "sequence_accuracy": seq / n,
        "step_precision": prec, "step_recall": rec, "step_f1": f1,
        "field_accuracy": fld_ok / max(fld_tot, 1),
        "evidence_accuracy": ev_ok / max(ev_tot, 1),
        "false_complete": false_complete,
        "false_incomplete": false_incomplete,
        "na_precision": na["tp"] / na_den_p if na_den_p else None,
        "na_recall": na["tp"] / na_den_r if na_den_r else None,
        "order_compliance": ordered / n,
        "per_operation": {op: {"tp": v[0], "fp": v[1], "fn": v[2],
                               "precision": v[0] / (v[0] + v[1]) if v[0] + v[1] else None,
                               "recall": v[0] / (v[0] + v[2]) if v[0] + v[2] else None} for op, v in sorted(per_op.items())},
        "missing_by_operation": dict(miss_by_op),
        "spurious_by_operation": dict(spur_by_op),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("predictions", nargs="?", default=str(HERE / "sample_predictions.csv"))
    ap.add_argument("--gold", default=str(ROOT / "dataset" / "sample_output.csv"))
    ap.add_argument("--cases")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--check-regression", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()
    pred, gold = load(args.predictions), load(args.gold)

    if args.update_baseline:
        BASELINE.write_text(json.dumps(exact_cases(pred, gold), indent=1), encoding="utf-8")
        print(f"baseline: {len(exact_cases(pred, gold))} exactly-right cases -> {BASELINE.name}")
        return 0
    if args.check_regression:
        base = set(json.loads(BASELINE.read_text(encoding="utf-8")))
        now = set(exact_cases(pred, gold))
        lost, gained = sorted(base - now), sorted(now - base)
        print(f"regression check: {len(base)} baseline cases, {len(lost)} lost, {len(gained)} gained")
        for c in lost:
            print("  REGRESSION", c)
        for c in gained:
            print("  improvement", c)
        return 1 if lost else 0

    only = set(args.cases.split(",")) if args.cases else None
    m = score(pred, gold, only, args.diff)
    for k, v in m.items():
        if isinstance(v, dict):
            continue
        print(f"{k:20s} {v:.3f}" if isinstance(v, float) else f"{k:20s} {v}")
    if args.detail:
        print("\nper operation (tp / fp / fn):")
        for op, d in m["per_operation"].items():
            print(f"  {op:28s} {d['tp']:3d} / {d['fp']:2d} / {d['fn']:2d}")
        print("missing by operation:", m["missing_by_operation"])
        print("spurious by operation:", m["spurious_by_operation"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
