"""One reproducible entry point for every check in this repository.

    python code/evaluation/run_all.py                  run everything; exit 1 if a required check fails
    python code/evaluation/run_all.py --skip-mutation  leave out the 12-rule mutation check (about two minutes)
    python code/evaluation/run_all.py --update-baseline  accept the current output as the new trusted baseline

Steps, each run as its own process with its real exit code and duration recorded:
  lint             lint_check.py (unused imports, 60-line functions, annotations)
  types            mypy over audit/ (code/mypy.ini); skipped when mypy is not installed, required when it is
  tests            the five offline test suites (no model, no network)
  full run         main.py on the 518 requests, written to a scratch file (the submission file is never touched here)
  schema           header, row count, ordering, statuses and JSON of that output and of the root output.csv
  baseline         the scratch output against the trusted baseline (code/evaluation/baseline/output.json: sha256 and
                   per-status counts) and against the root output.csv; a difference FAILS unless --update-baseline
  samples          main.py --samples, evaluate.py metrics, and the regression check (exactly-right samples stay right)
  custom suite     the 20 hand-written cases through the same pipeline
  mutation         each rule broken in turn must be caught by the suite
  error analysis   row-level categories of the remaining sample differences (error_analysis.md)
  reports          evaluation_report.md, confidence_report.md, usage_report.md (reusing the results above)

The trusted baseline is only ever replaced by --update-baseline, which refuses to run while any other check fails and
prints the old and the new values. The summary is written to code/evaluation/run_all_report.json and .md.
"""

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
ROOT = CODE.parent
BASELINE = HERE / "baseline" / "output.json"
SUITES = ["test_llm_layer", "test_verifier", "test_provenance", "test_regression", "test_parsers"]
HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}


class Step:
    def __init__(self, name: str, required: bool = True) -> None:
        self.name, self.required, self.ok, self.detail, self.seconds = name, required, False, "", 0.0
        self.commands: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}


def run(step: Step, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run `python -I <args>` and record the command, its exit code and its duration on the step."""
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    step.seconds += time.time() - t0
    tmp_root = tempfile.gettempdir()
    shown = " ".join(str(Path(a).relative_to(ROOT)) if a.startswith(str(ROOT))
                     else f"<tmp>/{Path(a).name}" if a.startswith(tmp_root) else a for a in args)
    step.commands.append({"command": "python -I " + shown, "exit_code": proc.returncode})
    return proc


def rows_of(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        return header, [dict(zip(HEADER, r)) for r in reader]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarise(path: Path) -> dict:
    _, rows = rows_of(path)
    return {"sha256": sha256(path), "rows": len(rows), "statuses": dict(sorted(Counter(r["deletion_status"] for r in rows).items()))}


# --------------------------------------------------------------------------- the steps
def step_lint() -> Step:
    s = Step("lint")
    out = run(s, [str(HERE / "lint_check.py")])
    s.ok = out.returncode == 0
    s.detail = " | ".join(l.strip() for l in out.stdout.splitlines() if l.startswith(("unused", "functions", "audit/")))
    return s


def step_types() -> Step:
    """mypy over audit/ with code/mypy.ini. mypy is a development tool, so a missing install is a skip, not a failure."""
    s = Step("types", required=False)
    probe = run(s, ["-m", "mypy", "--version"])
    if probe.returncode != 0:
        s.detail = "skipped: mypy is not installed in this interpreter (pip install mypy)"
        return s
    out = run(s, ["-m", "mypy", "--config-file", str(CODE / "mypy.ini")], cwd=ROOT)
    s.required = True   # once mypy is there, the result counts
    s.ok = out.returncode == 0 and out.stdout.startswith("Success")
    s.detail = (out.stdout.strip().splitlines() or ["no output"])[-1] + f" ({probe.stdout.strip()})"
    s.metrics = {"mypy_exit_code": out.returncode}
    return s


def step_tests() -> Step:
    s = Step("tests")
    for name in SUITES:
        out = run(s, [str(HERE / f"{name}.py")])
        m = re.search(r"Ran (\d+) tests?", out.stdout + out.stderr)
        s.metrics[name] = {"tests": int(m.group(1)) if m else 0, "exit_code": out.returncode}
    s.ok = all(v["exit_code"] == 0 and v["tests"] > 0 for v in s.metrics.values())
    s.detail = ", ".join(f"{k} {v['tests']}" for k, v in s.metrics.items())
    return s


def step_full_run(scratch: Path) -> Step:
    s = Step("full run")
    out = run(s, [str(CODE / "main.py"), "--output", str(scratch)])
    s.ok = out.returncode == 0 and "validation passed" in out.stdout and scratch.exists()
    s.detail = next((l for l in out.stdout.splitlines() if "independent verifier" in l), out.stdout[-200:]).split(" -> ")[0]
    return s


def check_schema(path: Path, expected_ids: list[str]) -> list[str]:
    """Structural problems of one output file (empty when it is valid)."""
    header, rows = rows_of(path)
    problems = [] if header == HEADER else [f"header {header}"]
    ids = [r["case_id"] for r in rows]
    problems += [] if ids == expected_ids else [f"case ids differ from the {len(expected_ids)} requests (or are not sorted)"]
    for r in rows:
        try:
            steps = json.loads(r["steps_json"])
            assert isinstance(steps, list) and [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))
            assert r["deletion_status"] in STATUSES and (r["deletion_status"] != "NOT_APPLICABLE" or steps == [])
        except (ValueError, AssertionError, KeyError, TypeError):
            problems.append(f"{r.get('case_id')}: invalid status or steps_json")
    return problems


def request_ids() -> list[str]:
    sys.path.insert(0, str(CODE))
    from audit.loaders import case_sort_key

    with open(ROOT / "dataset" / "deletion_requests.csv", encoding="utf-8", newline="") as fh:
        return sorted((r["case_id"] for r in csv.DictReader(fh)), key=case_sort_key)


def step_schema(scratch: Path) -> Step:
    s = Step("schema")
    ids = request_ids()
    problems = {label: check_schema(p, ids) for label, p in (("fresh run", scratch), ("root output.csv", ROOT / "output.csv"))
                if p.exists()}
    s.ok = bool(problems) and all(not v for v in problems.values()) and (ROOT / "output.csv").exists()
    s.detail = f"{len(ids)} requests; " + "; ".join(f"{k}: {'valid' if not v else v[:2]}" for k, v in problems.items())
    return s


def step_baseline(scratch: Path, update: bool) -> Step:
    s = Step("baseline")
    now = summarise(scratch)
    trusted = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    root_same = (ROOT / "output.csv").exists() and sha256(ROOT / "output.csv") == now["sha256"]
    s.metrics = {"current": now, "trusted": trusted, "root_output_matches": root_same}
    same = trusted is not None and trusted["sha256"] == now["sha256"]
    s.ok = same and root_same
    s.detail = ("matches the trusted baseline" if same else "DIFFERS from the trusted baseline" if trusted else "no trusted baseline")
    s.detail += "; root output.csv " + ("is identical" if root_same else "is STALE or missing")
    s.metrics["update_requested"] = update
    return s


def accept_baseline(scratch: Path) -> str:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    old = BASELINE.read_text(encoding="utf-8") if BASELINE.exists() else "(none)"
    BASELINE.write_text(json.dumps(summarise(scratch), indent=1) + "\n", encoding="utf-8")
    return f"baseline replaced\n  old: {old.strip()}\n  new: {BASELINE.read_text(encoding='utf-8').strip()}"


def metrics_from(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([a-z_0-9]+)\s+(\S+)$", line.strip())
        if m:
            out[m.group(1)] = None if m.group(2) == "None" else float(m.group(2))
    return out


def step_samples() -> Step:
    s = Step("samples")
    pred = HERE / "sample_predictions.csv"
    main = run(s, [str(CODE / "main.py"), "--samples"])
    ev = run(s, [str(HERE / "evaluate.py"), str(pred)])
    reg = run(s, [str(HERE / "evaluate.py"), "--check-regression"])
    s.metrics = metrics_from(ev.stdout)
    s.ok = main.returncode == 0 and ev.returncode == 0 and reg.returncode == 0 and s.metrics.get("status_accuracy") == 1.0
    s.detail = f"{reg.stdout.strip().splitlines()[-1] if reg.stdout.strip() else 'no regression output'}; " \
               f"status {s.metrics.get('status_accuracy')}, row exact {s.metrics.get('row_exact_match')}, step F1 {s.metrics.get('step_f1')}"
    return s


def step_custom() -> Step:
    s = Step("custom suite")
    suite = HERE / "custom_cases" / "suite"
    build = run(s, [str(HERE / "custom_cases" / "build_suite.py")])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "custom.csv"
        main = run(s, [str(CODE / "main.py"), "--dataset", str(suite), "--output", str(out)])
        ev = run(s, [str(HERE / "evaluate.py"), str(out), "--gold", str(suite / "expected_output.csv")])
    s.metrics = metrics_from(ev.stdout)
    s.ok = build.returncode == 0 and main.returncode == 0 and ev.returncode == 0 and s.metrics.get("row_exact_match") == 1.0
    s.detail = f"{int(s.metrics.get('cases') or 0)} cases, row exact {s.metrics.get('row_exact_match')}"
    return s


def step_mutation(skip: bool) -> Step:
    s = Step("mutation", required=not skip)
    if skip:
        s.detail, s.metrics = "skipped (--skip-mutation)", {"output": "_skipped_"}
        return s
    out = run(s, [str(HERE / "custom_cases" / "mutation_check.py")])
    caught = out.stdout.count("CAUGHT")
    s.ok = out.returncode == 0 and "MISSED" not in out.stdout and "SKIP" not in out.stdout and caught > 0
    s.metrics = {"output": out.stdout.strip(), "caught": caught}
    s.detail = f"{caught} rules caught, {out.stdout.count('MISSED')} missed"
    return s


def step_error_analysis() -> Step:
    s = Step("error analysis")
    out = run(s, [str(HERE / "error_analysis.py"), "--markdown", str(HERE / "error_analysis.md")])
    s.ok = out.returncode == 0
    s.detail = out.stdout.splitlines()[0] if out.stdout else ""
    return s


def step_reports(results: list[Step]) -> Step:
    s = Step("reports")
    reuse = {"tests": {k: [v["tests"], v["exit_code"] == 0] for k, v in next(r for r in results if r.name == "tests").metrics.items()},
             "mutation": "\n".join("- " + l for l in str(next(r for r in results if r.name == "mutation").metrics.get("output", "")).splitlines())}
    path = HERE / "run_all_reuse.json"
    path.write_text(json.dumps(reuse), encoding="utf-8")
    codes = [run(s, [str(HERE / "make_report.py"), "--reuse", str(path)]).returncode,
             run(s, [str(HERE / "make_confidence_report.py")]).returncode,
             run(s, [str(HERE / "make_usage_report.py")]).returncode]
    path.unlink()
    s.ok = all(c == 0 for c in codes)
    s.detail = "evaluation, confidence and usage reports written" if s.ok else f"exit codes {codes}"
    return s


# --------------------------------------------------------------------------- orchestration
def write_summary(results: list[Step], passed: bool, note: str) -> None:
    data = {"passed": passed, "note": note, "steps": [
        {"name": r.name, "required": r.required, "ok": r.ok, "detail": r.detail, "seconds": round(r.seconds, 1),
         "commands": r.commands, "metrics": r.metrics if r.name != "mutation" else {"caught": r.metrics.get("caught")}}
        for r in results]}
    (HERE / "run_all_report.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = ["# run_all result", "", f"**{'PASSED' if passed else 'FAILED'}**" + (f" - {note}" if note else ""), "",
             "| step | result | seconds | detail |", "|---|---|---|---|"]
    lines += [f"| {r.name} | {'ok' if r.ok else ('FAIL' if r.required else 'not required')} | {r.seconds:.1f} | {r.detail} |" for r in results]
    lines += ["", "Commands and exit codes are in `run_all_report.json`."]
    (HERE / "run_all_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-mutation", action="store_true")
    ap.add_argument("--update-baseline", action="store_true", help="replace the trusted baseline with the current output (explicit only)")
    args = ap.parse_args()
    results: list[Step] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "output.csv"
        for make in (step_lint, step_types, step_tests, lambda: step_full_run(scratch), lambda: step_schema(scratch),
                     lambda: step_baseline(scratch, args.update_baseline), step_samples, step_custom,
                     lambda: step_mutation(args.skip_mutation), step_error_analysis):
            step = make()
            results.append(step)
            print(f"{'ok  ' if step.ok else 'FAIL' if step.required else 'skip'} {step.name:15s} {step.seconds:6.1f}s  {step.detail}")
        note = ""
        base = next(r for r in results if r.name == "baseline")
        if args.update_baseline:
            others = [r for r in results if r is not base and r.required and not r.ok and r.name != "schema"]
            if others:
                note = "baseline NOT updated: other checks failed (" + ", ".join(r.name for r in others) + ")"
            else:
                note = accept_baseline(scratch)
                base.ok = (ROOT / "output.csv").exists() and sha256(ROOT / "output.csv") == sha256(scratch)
                base.detail = "baseline replaced explicitly; root output.csv " + ("identical" if base.ok else "STALE (copy the new output to the root)")
            print(note)
        results.append(step_reports(results))
        print(f"{'ok  ' if results[-1].ok else 'FAIL'} {results[-1].name:15s} {results[-1].seconds:6.1f}s  {results[-1].detail}")
    passed = all(r.ok for r in results if r.required)
    write_summary(results, passed, note)
    print("run_all:", "PASSED" if passed else "FAILED -> " + ", ".join(r.name for r in results if r.required and not r.ok))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
