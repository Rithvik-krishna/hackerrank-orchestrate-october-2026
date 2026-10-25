"""Ghost Data Still Exists - deletion-audit agent.

Usage:
    python code/main.py                 audit all requests -> ./output.csv
    python code/main.py --samples       audit the 32 samples -> code/evaluation/sample_predictions.csv
    python code/main.py --parse-report  parse every evidence file and print per-file counts
    python code/main.py --dataset DIR --output FILE   audit another dataset folder (custom cases)
"""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# the dataset folder must be chosen before the audit modules read their paths
for _i, _arg in enumerate(sys.argv):
    if _arg == "--dataset" and _i + 1 < len(sys.argv):
        os.environ["AUDIT_DATASET"] = str(Path(sys.argv[_i + 1]).resolve())

from audit import diagnostics, ledger, loaders, paths  # noqa: E402
from audit.pipeline import Pipeline  # noqa: E402
from audit.shapes import Request  # noqa: E402
from audit.steps import dumps_steps  # noqa: E402
from audit.validate import HEADER, validate_rows  # noqa: E402
from audit.verifier import Verifier  # noqa: E402


def report_diagnostics(store: Any, out_path: Path) -> None:
    """Write the parse diagnostics next to the other run files and warn about partly readable evidence."""
    dpath = (paths.EVAL_DIR / "parse_diagnostics.csv" if out_path == paths.OUTPUT_CSV
             else out_path.with_name(out_path.stem + ".parse_diagnostics.csv"))
    diagnostics.write_csv(dpath, store.diagnostics)
    if store.degraded:
        skipped = sum(len(v) for v in store.degraded.values())
        print(f"WARNING {len(store.degraded)} evidence file(s) were only partly readable ({skipped} item(s) skipped): "
              f"{', '.join(sorted(store.degraded))}; they cannot prove absence and cap the affected cases at medium -> {dpath}")


def run(requests: list[Request], out_path: Path, use_llm: bool = False, review: bool = False, agent: bool = False) -> int:
    pipe = Pipeline(use_llm=use_llm, review=review, agent=agent)
    rows = []
    # an evidence file that cannot be parsed must be visible: its facts are missing from every answer
    parse_failures = [f"evidence file {fid} failed to parse: {msg}" for fid, msg in sorted(pipe.store.errors.items())]
    pending = sorted(fid for fid, (_, n, _, note) in pipe.store.per_file.items() if note == "pending")
    for line in parse_failures:
        print("ERROR", line)
    report_diagnostics(pipe.store, out_path)
    if pending:
        print(f"WARNING {len(pending)} screenshot(s) have no cached reading and contribute nothing: {', '.join(pending)}")
    verifier = Verifier(pipe.store, pipe.manifest, pipe.activity, pipe.rules, pipe.identity, pipe.citable())
    findings = []
    for req in sorted(requests, key=lambda r: loaders.case_sort_key(r["case_id"])):
        status, steps, _trail = pipe.audit(req)
        rows.append({"case_id": req["case_id"], "deletion_status": status, "steps_json": dumps_steps(steps)})
        # an independent check of the answer from the raw evidence; it reports, it never repairs
        findings += verifier.verify_case(req, status, steps, pipe.linked.get(req["case_id"], []))

    errors = validate_rows(rows, requests, pipe.citable())
    bad = [f for f in findings if f.severity == "error"]
    vpath = (paths.EVAL_DIR / "verification.csv" if out_path == paths.OUTPUT_CSV
             else out_path.with_name(out_path.stem + ".verification.csv"))
    vpath.parent.mkdir(parents=True, exist_ok=True)
    with open(vpath, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "severity", "code", "message"])
        w.writerows([[f.case_id, f.severity, f.code, f.message] for f in findings])
    print(f"independent verifier: {len(bad)} errors in {len({f.case_id for f in bad})} cases, "
          f"{len(findings) - len(bad)} warnings -> {vpath}")
    errors += [f"verifier {f.case_id} [{f.code}] {f.message}" for f in bad] + parse_failures
    ledger_path = (paths.EVAL_DIR / "confidence.csv" if out_path == paths.OUTPUT_CSV
                   else out_path.with_name(out_path.stem + ".confidence.csv"))
    ledger.write_csv(ledger_path, rows, pipe.meta)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out_path}")
    if pipe.client:
        review_file = paths.CACHE_DIR / "model_review.json"
        review_file.write_text(json.dumps({
            "intent_disagreements": pipe.disagreements,
            "adjudications": pipe.adjudicator.decisions if pipe.adjudicator else [],
            "alignment": getattr(pipe.linker, "align_stats", {}),
        }, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"model review trail -> {review_file} "
              f"({len(pipe.disagreements)} intent disagreements, "
              f"{len(pipe.adjudicator.decisions) if pipe.adjudicator else 0} adjudicated groups)")
    if errors:
        print(f"VALIDATION FAILED ({len(errors)} errors):")
        for e in errors[:30]:
            print("  ", e)
        return 1
    print("validation passed")
    return 0


def _all_requests() -> dict[str, Request]:
    return {r["case_id"]: r for r in loaders.load_requests(True) + loaders.load_requests(False)}


def extract_images(force: bool) -> int:
    """Read the screenshots with the vision model into the cache."""
    from audit.llm import LLMClient
    from audit.vision import extract_all

    client = LLMClient()
    if not client.available:
        print("no model API key is set (GEMINI_API_KEY or ANTHROPIC_API_KEY)")
        return 2
    links = {}
    for a in loaders.load_activity():
        m = re.search(r"(?:linked|confirmed) \S+ \(\S+\) (?:to|belongs only to) \S+ and (user_\d+)", a["details"])
        user = loaders.norm_user(m.group(1)) if m else None
        if user:
            links[a["resource_id"]] = user
    summary = extract_all(client, force=force, links=links)
    print("vision:", summary)
    return 0 if not summary["failed"] else 1


def show_graph(case_id: str) -> int:
    """Print the provenance graph of one case and write it as JSON."""
    from audit import provenance

    status, steps, graph = provenance.build(Pipeline(), _all_requests()[case_id])
    print(f"{case_id}: {status}; {len(graph.nodes)} nodes, {len(graph.edges)} edges, "
          f"{len(graph.conflicts)} conflicts, {len(graph.excluded)} observations not used")
    provenance.explain(graph, steps)
    target = paths.EVAL_DIR / "provenance" / f"{case_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(graph.as_dict(), indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"graph -> {target}")
    return 0


def show_trail(case_id: str, use_llm: bool, agent: bool) -> int:
    """Print the decision trail and steps of one case."""
    status, steps, trail = Pipeline(use_llm=use_llm, agent=agent).audit(_all_requests()[case_id])
    print(f"{case_id}: {status}")
    print("\n".join("  " + t for t in trail))
    for s in steps:
        print("  ", s["step_no"], s["operation"], s["system_id"], s["resource_id"], s["evidence_id"] or '""')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", action="store_true", help="audit the sample requests instead")
    ap.add_argument("--parse-report", action="store_true", help="parse all evidence and report")
    ap.add_argument("--explain", metavar="CASE_ID", help="print the decision trail for one case")
    ap.add_argument("--graph", metavar="CASE_ID", help="print the provenance graph of one case (and write it as JSON)")
    ap.add_argument("--dataset", metavar="DIR", help="dataset folder to audit (default: ./dataset)")
    ap.add_argument("--output", metavar="FILE", help="where to write the predictions")
    ap.add_argument("--llm", action="store_true",
                    help="use the model components (needs GEMINI_API_KEY or ANTHROPIC_API_KEY): intent arbitration, link adjudication")
    ap.add_argument("--review", action="store_true", help="with --llm: also ask the model for a second opinion on every request")
    ap.add_argument("--agent", action="store_true",
                    help="with --llm: ambiguous links go to the tool-calling investigator instead of the single-shot adjudicator")
    ap.add_argument("--extract-images", action="store_true", help="read the screenshots with the vision model into code/cache/vision")
    ap.add_argument("--force", action="store_true", help="with --extract-images: replace existing cache files")
    ap.add_argument("--fresh-ledger", action="store_true", help="start code/cache/usage_ledger.jsonl empty (do this before the final run)")
    args = ap.parse_args()

    if args.fresh_ledger:
        ledger = paths.CACHE_DIR / "usage_ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("", encoding="utf-8")
    if args.extract_images:
        return extract_images(args.force)
    if args.graph:
        return show_graph(args.graph)
    if args.explain:
        return show_trail(args.explain, args.llm, args.agent)
    if args.parse_report:
        from audit.evidence import parse_report

        return parse_report()
    if args.samples:
        return run(loaders.load_requests(sample=True),
                   Path(args.output) if args.output else paths.EVAL_DIR / "sample_predictions.csv", args.llm, args.review, args.agent)
    return run(loaders.load_requests(), Path(args.output) if args.output else paths.OUTPUT_CSV, args.llm, args.review, args.agent)


if __name__ == "__main__":
    sys.exit(main())
