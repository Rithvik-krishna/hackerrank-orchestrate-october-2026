"""Show the custom suite has teeth: break one rule at a time in a scratch copy of the code and
confirm the suite catches it.

    python code/evaluation/custom_cases/mutation_check.py
"""

import csv
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
SUITE = CODE / "evaluation" / "custom_cases" / "suite"

MUTATIONS = [
    ("catalog link may predate the request", "audit/linker.py",
     "if u == uid and requested_at <= when <= audit_at:", "if u == uid and when <= audit_at:"),
    ("records after audit_at count", "audit/state.py",
     "obs = sorted((o for o in everything if o.observed_at <= audit_at),", "obs = sorted((o for o in everything),"),
    ("look-alike addresses match by local-part prefix", "audit/linker.py",
     "hit = (o.identifiers | o.link_keys) & S",
     "hit = {x for x in (o.identifiers | o.link_keys) for s in S if '@' in s and '@' in x and x.split('@')[0].startswith(s.split('@')[0])}"),
    ("shared attachments are deleted", "audit/pipeline.py",
     "shared = len(self.linker.owners_at(rid, audit_at)) > 1", "shared = False"),
    ("emails are case-sensitive", "audit/loaders.py",
     "return str(value).strip().lower()\n\n\ndef norm_phone", "return str(value).strip()\n\n\ndef norm_phone"),
    ("old email hash is not an identifier", "audit/identity.py",
     '"email_hash": lambda v: v.strip().lower(),', '"email_hash": lambda v: "unused-" + v,'),
    ("an active sync is not a recreation risk", "audit/policy.py",
     "        if state.status == ACTIVE:\n            return Assessment(system, rid, category, action, False, \"REPORT_ACTIVE_DATA_FLOW\",",
     "        if False and state.status == ACTIVE:\n            return Assessment(system, rid, category, action, False, \"REPORT_ACTIVE_DATA_FLOW\","),
    ("a restorable backup is not a recreation risk", "audit/policy.py",
     "        if state.status in (RESTORABLE, ACTIVE):", "        if False and state.status in (RESTORABLE, ACTIVE):"),
    ("retained records are not re-verified", "audit/pipeline.py",
     'REVERIFIED = ("USER_PROFILE", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD")', 'REVERIFIED = ("USER_PROFILE",)'),
    ("a request that merely says 'remove' is a deletion", "audit/intent.py",
     r'r"\bmarketing\s+(?:lists?|emails?)?\s*only\b",', ""),
    ("an explicit 'do not delete' is still a deletion", "audit/intent.py",
     r'r"\b(?:don' + "'" + r'?t|do\s+not|please\s+don' + "'" + r'?t)\s+(?:delete|erase|close|remove|change)",', ""),
    ("a read/loaded log event is not evidence that the record exists", "audit/evidence/logs.py",
     "    if _READ_EVENT.search(event):", "    if False and _READ_EVENT.search(event):"),
    ("an INCOMPLETE answer does not re-verify an account that only the worker stream shows", "audit/pipeline.py",
     "            if op:\n                steps.append(make_step(op, system, rid, \"\"))", "            if False and op:\n                steps.append(make_step(op, system, rid, \"\"))"),
    ("a line with no state supersedes an earlier line that has one", "audit/state.py",
     "    obs = informative or obs", "    obs = obs"),
    ("a primary that omits the record outranks a replica that shows it", "audit/state.py",
     "(last is None or c.observed_at > last.observed_at)", "(last is None or c.observed_at >= last.observed_at)"),
]


def run(code_dir: Path) -> dict[str, bool]:
    out = code_dir / "mut_predictions.csv"
    subprocess.run([sys.executable, "-I", str(code_dir / "main.py"), "--dataset", str(SUITE), "--output", str(out)],
                   capture_output=True, text=True, check=False)
    if not out.exists():
        return {"crashed": True}
    pred = {r["case_id"]: r for r in csv.DictReader(open(out, encoding="utf-8", newline=""))}
    gold = {r["case_id"]: r for r in csv.DictReader(open(SUITE / "expected_output.csv", encoding="utf-8", newline=""))}
    import json

    return {cid: cid in pred and json.loads(pred[cid]["steps_json"]) == json.loads(g["steps_json"])
            and pred[cid]["deletion_status"] == g["deletion_status"] for cid, g in gold.items()}


def main() -> int:
    ok = True
    for name, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "code"
            shutil.copytree(CODE, copy, ignore=shutil.ignore_patterns("__pycache__", "suite", "cache"))
            shutil.copytree(CODE / "cache", copy / "cache")
            target = copy / rel
            text = target.read_text(encoding="utf-8")
            if old not in text:
                print(f"SKIP  {name}: pattern not found in {rel}")
                ok = False
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            res = run(copy)
            failed = sorted(k for k, v in res.items() if v is not True)
            caught = bool(failed)
            ok &= caught
            print(f"{'CAUGHT' if caught else 'MISSED'} {name}: failing cases {failed}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
