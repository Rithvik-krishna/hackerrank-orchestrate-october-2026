"""Regression tests: what was right stays right, nothing invalid is produced, bad inputs fail visibly.

    python code/evaluation/test_regression.py

  * every labelled sample answered exactly right when the baseline was recorded is still exactly right
  * the independent verifier finds no error in the 32 sample answers or the 518 answers
  * all custom cases still pass (they cover temporal leakage, historical identity, look-alikes, shared attachments,
    active recreation paths, mandatory retention, tombstones, and instructions planted in evidence)
  * an evidence file that cannot be parsed makes a run fail loudly instead of silently dropping its facts
"""

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SUITE = HERE / "custom_cases" / "suite"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))


def run_main(*args):
    return subprocess.run([sys.executable, "-I", str(ROOT / "code" / "main.py"), *args], capture_output=True, text=True)


class Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from audit import loaders
        from audit.pipeline import Pipeline
        from audit.steps import dumps_steps

        cls.pipe = Pipeline()
        cls.answers = {}
        for r in loaders.load_requests(True) + loaders.load_requests(False):
            status, steps, _ = cls.pipe.audit(r)
            cls.answers[r["case_id"]] = {"case_id": r["case_id"], "deletion_status": status, "steps_json": dumps_steps(steps)}

    def test_previously_exact_samples_stay_exact(self):
        from evaluate import exact_cases, load

        base = set(json.loads((HERE / "regression_baseline.json").read_text(encoding="utf-8")))
        gold = load(ROOT / "dataset" / "sample_output.csv")
        now = set(exact_cases({c: self.answers[c] for c in gold}, gold))
        self.assertEqual(sorted(base - now), [], "answers that used to be exactly right changed")

    def test_no_false_complete_on_the_samples(self):
        from evaluate import load, score

        gold = load(ROOT / "dataset" / "sample_output.csv")
        self.assertEqual(score({c: self.answers[c] for c in gold}, gold)["false_complete"], 0)

    def test_the_independent_verifier_accepts_every_answer(self):
        from audit import loaders
        from audit.verifier import Verifier

        p = self.pipe
        ver = Verifier(p.store, p.manifest, p.activity, p.rules, p.identity, p.citable())
        bad = []
        for r in loaders.load_requests(True) + loaders.load_requests(False):
            a = self.answers[r["case_id"]]
            bad += [f for f in ver.verify_case(r, a["deletion_status"], json.loads(a["steps_json"]), p.linked.get(r["case_id"], []))
                    if f.severity == "error"]
        self.assertEqual([(f.case_id, f.code, f.message) for f in bad], [])

    def test_all_custom_cases_pass(self):
        out = Path(tempfile.mkdtemp()) / "p.csv"
        res = run_main("--dataset", str(SUITE), "--output", str(out))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        pred = {r["case_id"]: r for r in csv.DictReader(open(out, encoding="utf-8", newline=""))}
        gold = {r["case_id"]: r for r in csv.DictReader(open(SUITE / "expected_output.csv", encoding="utf-8", newline=""))}
        wrong = [c for c, g in gold.items() if c not in pred or pred[c]["deletion_status"] != g["deletion_status"]
                 or json.loads(pred[c]["steps_json"]) != json.loads(g["steps_json"])]
        self.assertEqual(wrong, [])

    def test_an_unparseable_evidence_file_fails_the_run_visibly(self):
        tmp = Path(tempfile.mkdtemp()) / "suite"
        shutil.copytree(SUITE, tmp)
        (tmp / "evidence" / "records" / "broken.json").write_text("{ this is not json", encoding="utf-8")
        with open(tmp / "evidence_files.csv", "a", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(["evidence_99", "evidence/records/broken.json", "records_export", "2026-03-01T00:00:00Z"])
        res = run_main("--dataset", str(tmp), "--output", str(tmp / "p.csv"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("evidence file evidence_99 failed to parse", res.stdout)


class ShapesMatchData(unittest.TestCase):
    """audit/shapes.py describes the rows the pipeline really reads and writes (the TypedDicts must not drift)."""

    def keys(self, typed) -> set:
        return set(typed.__required_keys__ | typed.__optional_keys__)

    def test_loaded_rows_have_exactly_the_described_keys(self):
        from audit import loaders
        from audit.shapes import ActivityRow, ManifestRow, RequestRow, RuleRow

        self.assertEqual(set(loaders.load_requests()[0]), self.keys(RequestRow))
        self.assertEqual(set(next(iter(loaders.load_manifest().values()))), self.keys(ManifestRow))
        self.assertEqual(set(loaders.load_activity()[0]), self.keys(ActivityRow))
        self.assertEqual(set(next(iter(loaders.load_rules().values()))), self.keys(RuleRow))

    def test_a_step_has_exactly_the_output_fields(self):
        from audit.model import STEP_FIELDS
        from audit.shapes import Step
        from audit.steps import make_step

        self.assertEqual(set(make_step("VERIFY_RECORD_ABSENT", "account_service", "account_1", "evidence_1")), self.keys(Step))
        self.assertEqual(self.keys(Step), set(STEP_FIELDS))

    def test_the_pipeline_adds_only_the_described_request_fields(self):
        from audit.pipeline import Pipeline
        from audit.shapes import Request

        pipe = Pipeline()
        req = pipe.linker.requests[0]
        self.assertTrue(set(req) <= self.keys(Request), set(req) - self.keys(Request))
        self.assertTrue({"is_deletion", "intent", "uid"} <= set(req))

    def test_an_ambiguous_group_has_the_described_keys(self):
        from audit.linker import Linker
        from audit.shapes import AmbiguousCase, AmbiguousGroup, AmbiguousRow

        seen = []

        class Spy:
            def resolve(self, amb):
                seen.append(amb)
                return []

        from audit.pipeline import Pipeline

        pipe = Pipeline()
        linker = Linker(pipe.identity, pipe.store, pipe.activity, pipe.linker.requests, Spy())
        linker.violating = pipe.linker.violating
        import audit.linker as linker_mod

        old, linker_mod.ZIP_MODE = linker_mod.ZIP_MODE, "llm"
        try:
            linker.link(pipe.linker.requests[0], pipe.linker.requests[0]["uid"] or "user_1")
        finally:
            linker_mod.ZIP_MODE = old
        self.assertTrue(seen, "the spy resolver was never asked")
        amb = seen[0]
        self.assertEqual(set(amb), self.keys(AmbiguousGroup))
        self.assertEqual(set(amb["cases"][0]), self.keys(AmbiguousCase))
        self.assertEqual(set(amb["rows"][0]), self.keys(AmbiguousRow))


class PackagingGuards(unittest.TestCase):
    """The package audit and the trace stager (code/tools) fail closed and never leak a secret."""

    FAKE_KEY = "AQ." + "x" * 44   # shaped like an API key, not a real one

    def setUp(self):
        sys.path.insert(0, str(ROOT / "code" / "tools"))
        import make_trace
        import make_zip

        self.make_zip, self.make_trace = make_zip, make_trace
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_archive_carries_a_root_readme_and_no_caches(self):
        names = [n for n, _ in self.make_zip.entries()]
        self.assertIn("README.md", names)
        self.assertIn("code/README.md", names)
        self.assertIn("code/evaluation/main.py", names)
        self.assertFalse([n for n in names if n.startswith("dataset/") or ".mypy_cache" in n or "__pycache__" in n
                          or n.startswith("code/cache/llm/") or n.endswith((".zip", ".env"))])

    def test_the_audit_is_clean_on_the_real_tree(self):
        self.assertEqual(self.make_zip.audit(self.make_zip.entries()), [])

    def test_the_audit_rejects_a_secret_a_machine_path_and_a_missing_file(self):
        bad = self.tmp / "notes.md"
        machine_path = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "Desktop"   # built at run time so this file itself stays clean
        bad.write_text(f"key {self.FAKE_KEY}\nsee {machine_path}", encoding="utf-8")
        problems = self.make_zip.audit([("code/notes.md", bad)])
        self.assertTrue(any("possible secret" in p for p in problems))
        self.assertTrue(any("machine-specific path" in p for p in problems))
        self.assertTrue(any("required file missing" in p for p in problems))

    def test_redaction_removes_literals_and_key_shapes_and_keeps_json_valid(self):
        line = json.dumps({"text": f"my key is {self.FAKE_KEY} and the other is hunter2hunter2"}).encode()
        out, n = self.make_trace.redact(line, [b"hunter2hunter2"])
        self.assertEqual(n, 2)
        self.assertNotIn(self.FAKE_KEY.encode(), out)
        self.assertNotIn(b"hunter2hunter2", out)
        self.assertEqual(json.loads(out)["text"].count("[REDACTED]"), 2)

    def test_the_trace_zip_holds_sessions_and_exports_but_no_credentials(self):
        projects = self.tmp / "projects"
        mine = projects / "C--work-orchetrate"
        (mine / "sub").mkdir(parents=True)
        (mine / "session1.jsonl").write_text(json.dumps({"m": f"pasted {self.FAKE_KEY}"}) + "\n", encoding="utf-8")
        (mine / "sub" / "agent1.jsonl").write_text('{"m": "subagent"}\n', encoding="utf-8")
        (mine / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
        other = projects / "C--elsewhere"
        other.mkdir(parents=True)
        (other / "unrelated.jsonl").write_text('{"m": "not this project"}\n', encoding="utf-8")
        exports = self.tmp / "exports"
        exports.mkdir()
        (exports / "session1-export.md").write_text("# transcript\n", encoding="utf-8")
        (exports / "credentials.json").write_text("{}", encoding="utf-8")
        out = self.tmp / "agent-trace.zip"
        env = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"}
        run = subprocess.run([sys.executable, "-I", str(ROOT / "code" / "tools" / "make_trace.py"), "--projects-dir", str(projects),
                              "--export-dir", str(exports), "--out", str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        import zipfile

        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names)
        self.assertEqual(sorted(names), [
            "agent-trace/claude-code/C--work-orchetrate/session1.jsonl",
            "agent-trace/claude-code/C--work-orchetrate/sub/agent1.jsonl",
            "agent-trace/claude-code/exports/session1-export.md"])
        self.assertNotIn(self.FAKE_KEY.encode(), blob)
        self.assertIn(b"[REDACTED]", blob)
        self.assertIn("1 secret(s) redacted", run.stdout)


class RunAllGuards(unittest.TestCase):
    """run_all.py fails closed: bad outputs are rejected and the trusted baseline is never replaced by a check."""

    def setUp(self):
        import run_all

        self.run_all = run_all
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.good = self.tmp / "good.csv"
        shutil.copy(ROOT / "output.csv", self.good)
        self.ids = run_all.request_ids()
        self.baseline = self.tmp / "output.json"
        self.baseline.write_text(json.dumps(run_all.summarise(self.good)), encoding="utf-8")
        self.saved = run_all.BASELINE
        run_all.BASELINE = self.baseline
        self.addCleanup(setattr, run_all, "BASELINE", self.saved)

    def rewrite(self, edit):
        with open(self.good, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        edit(rows)
        bad = self.tmp / "bad.csv"
        with open(bad, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
        return bad

    def test_a_valid_output_passes_the_schema_check(self):
        self.assertEqual(self.run_all.check_schema(self.good, self.ids), [])

    def test_the_schema_check_rejects_damage(self):
        header = self.rewrite(lambda r: r[0].__setitem__(1, "status"))
        dropped = self.rewrite(lambda r: r.pop(5))
        broken = self.rewrite(lambda r: r[3].__setitem__(2, "[{not json"))
        na_with_steps = self.rewrite(lambda r: next(x for x in r[1:] if x[1] == "NOT_APPLICABLE").__setitem__(2, '[{"step_no":1}]'))
        for path in (header, dropped, broken, na_with_steps):
            self.assertTrue(self.run_all.check_schema(path, self.ids), path)

    def test_an_identical_output_matches_the_baseline_and_changes_nothing(self):
        before = self.baseline.read_bytes()
        step = self.run_all.step_baseline(self.good, update=False)
        self.assertTrue(step.metrics["current"]["sha256"] == json.loads(before)["sha256"])
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_a_changed_output_fails_and_the_baseline_stays_untouched(self):
        changed = self.rewrite(lambda r: r[2].__setitem__(1, "COMPLETE" if r[2][1] != "COMPLETE" else "INCOMPLETE"))
        before = self.baseline.read_bytes()
        step = self.run_all.step_baseline(changed, update=False)
        self.assertFalse(step.ok)
        self.assertIn("DIFFERS", step.detail)
        self.assertEqual(self.baseline.read_bytes(), before)   # a comparison never rewrites the trusted file

    def test_a_missing_baseline_fails(self):
        self.baseline.unlink()
        step = self.run_all.step_baseline(self.good, update=False)
        self.assertFalse(step.ok)
        self.assertIn("no trusted baseline", step.detail)
        self.assertFalse(self.baseline.exists())   # and is not created implicitly


if __name__ == "__main__":
    unittest.main(verbosity=1)
