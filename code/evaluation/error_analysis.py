"""Row-level error analysis of the labelled samples: why step F1 is high while exact row agreement is lower.

    python code/evaluation/error_analysis.py [predictions.csv] [--markdown FILE]

Every case whose row is not identical to the gold is classified, and each difference is assigned to exactly one
category. Steps are first matched on (operation, system_id, resource_id); what is left over is then matched on
(operation, system_id), so a wrong resource is reported as a substitution rather than as an unrelated miss and an extra.

  missing_step            a gold step with no counterpart in the prediction
  extra_step              a predicted step with no counterpart in the gold
  resource_substitution   same operation and system, different resource_id (a step for the wrong record)
  system_mismatch         same operation and resource, different system_id
  evidence_mismatch       same step, different evidence_id
  field_mismatch          same step, different phase / state
  renumbering_cascade     step_no differs only because an earlier step is missing or extra (a consequence, not a cause)
  ordering                the same steps in a different order
  serialization (benign)  the same data, but the steps_json text differs from the gold's (key order / whitespace); the
                          grader ignores both, so this is reported but does not make a row wrong
  status                  deletion_status differs

The analysis reads only the prediction and the labelled sample answers; it changes nothing.
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CATEGORIES = ["status", "missing_step", "extra_step", "resource_substitution", "system_mismatch", "evidence_mismatch",
              "field_mismatch", "renumbering_cascade", "ordering", "serialization"]
BENIGN = {"serialization", "renumbering_cascade"}   # never make a row wrong on their own
KEY = ("operation", "system_id", "resource_id")


def load(path: Path) -> dict:
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def step_key(s: dict, fields: tuple = KEY) -> tuple:
    return tuple(s[f] for f in fields)


def pair_up(pred: list, gold: list, fields: tuple) -> tuple[list, list, list]:
    """Pair steps that agree on `fields` (first come, first served); returns (pairs, unmatched pred, unmatched gold)."""
    pool = defaultdict(list)
    for g in gold:
        pool[step_key(g, fields)].append(g)
    pairs, rest = [], []
    for p in pred:
        bucket = pool.get(step_key(p, fields))
        if bucket:
            pairs.append((p, bucket.pop(0)))
        else:
            rest.append(p)
    return pairs, rest, [g for bucket in pool.values() for g in bucket]


def classify(pred_row: dict | None, gold_row: dict) -> list[tuple[str, str]]:
    """[(category, detail)] for one case; empty when the row is identical."""
    out = []
    if pred_row is None:
        return [("missing_step", f"{len(json.loads(gold_row['steps_json']))} gold steps, no prediction")]
    if pred_row["deletion_status"] != gold_row["deletion_status"]:
        out.append(("status", f"{pred_row['deletion_status']} instead of {gold_row['deletion_status']}"))
    pred, gold = json.loads(pred_row["steps_json"]), json.loads(gold_row["steps_json"])
    pairs, p_rest, g_rest = pair_up(pred, gold, KEY)
    for p, g in pairs:
        if p["evidence_id"] != g["evidence_id"]:
            out.append(("evidence_mismatch", f"{p['operation']} {p['resource_id']}: {p['evidence_id']!r} instead of {g['evidence_id']!r}"))
        diff = [f for f in ("phase", "state") if p[f] != g[f]]
        if diff:
            out.append(("field_mismatch", f"{p['operation']} {p['resource_id']}: {', '.join(diff)}"))
        if p["step_no"] != g["step_no"]:
            out.append(("renumbering_cascade" if len(pred) != len(gold) else "field_mismatch",
                        f"{p['operation']} {p['resource_id']}: step_no {p['step_no']} instead of {g['step_no']}"))
    sub_pairs, p_rest, g_rest = pair_up(p_rest, g_rest, ("operation", "system_id"))
    for p, g in sub_pairs:
        out.append(("resource_substitution", f"{p['operation']} {p['system_id']}: {p['resource_id']} instead of {g['resource_id']}"))
    sys_pairs, p_rest, g_rest = pair_up(p_rest, g_rest, ("operation", "resource_id"))
    for p, g in sys_pairs:
        out.append(("system_mismatch", f"{p['operation']} {p['resource_id']}: {p['system_id']} instead of {g['system_id']}"))
    out += [("missing_step", f"{g['operation']} {g['system_id']} {g['resource_id']}") for g in g_rest]
    out += [("extra_step", f"{p['operation']} {p['system_id']} {p['resource_id']}") for p in p_rest]
    if not out and [step_key(s) for s in pred] != [step_key(s) for s in gold]:
        out.append(("ordering", "same steps, different order"))
    if not out and pred_row["steps_json"] != gold_row["steps_json"] and pred != gold:
        out.append(("field_mismatch", "steps differ in a field"))
    if not out and pred_row["steps_json"] != gold_row["steps_json"]:
        out.append(("serialization", "same data, different text"))
    return out


def analyse(pred: dict, gold: dict) -> dict:
    cases: dict[str, list[tuple[str, str]]] = {}
    per_category: Counter[str] = Counter()
    steps_gold = steps_pred = 0
    wrong: list[str] = []
    exact_json = sum(1 for c, g in gold.items() if c in pred and pred[c]["steps_json"] == g["steps_json"])
    for cid in sorted(gold):
        steps_gold += len(json.loads(gold[cid]["steps_json"]))
        steps_pred += len(json.loads(pred[cid]["steps_json"])) if cid in pred else 0
        found = classify(pred.get(cid), gold[cid])
        if found:
            cases[cid] = found
            per_category.update(cat for cat, _ in found)
            if any(cat not in BENIGN for cat, _ in found):
                wrong.append(cid)
    return {"cases": cases, "wrong": wrong, "per_category": per_category, "n": len(gold), "steps_gold": steps_gold,
            "steps_pred": steps_pred, "byte_identical_rows": exact_json}


def render(result: dict) -> str:
    n, bad = result["n"], len(result["wrong"])
    out = [f"{n} labelled cases; {n - bad} identical to the gold as data, {bad} differ "
           f"(row exact {(n - bad) / n:.3f}); {result['steps_gold']} gold steps, {result['steps_pred']} predicted.",
           f"rows whose steps_json text is also byte-for-byte the gold's: {result['byte_identical_rows']} of {n} "
           "(the others differ only in key order or whitespace, which the grader ignores)", "",
           "| category | differences | cases affected |", "|---|---|---|"]
    for cat in CATEGORIES:
        count = result["per_category"].get(cat, 0)
        affected = sum(1 for found in result["cases"].values() if any(c == cat for c, _ in found))
        out.append(f"| {cat} | {count} | {affected} |")
    out += ["", "Per case:", ""]
    for cid in result["wrong"]:
        found = result["cases"][cid]
        out.append(f"- {cid}: " + "; ".join(f"{cat} ({detail})" for cat, detail in found))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("predictions", nargs="?", default=str(HERE / "sample_predictions.csv"))
    ap.add_argument("--markdown", metavar="FILE")
    args = ap.parse_args()
    result = analyse(load(Path(args.predictions)), load(ROOT / "dataset" / "sample_output.csv"))
    text = render(result)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text("# Row-level error analysis\n\n" + text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
