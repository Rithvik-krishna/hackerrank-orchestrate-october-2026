"""Tests of the provenance graph: identity sources, cutoffs, shared resources, ambiguity, replica drift, traceability.

    python code/evaluation/test_provenance.py
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SUITE = HERE / "custom_cases" / "suite"
sys.path.insert(0, str(HERE.parent))


def suite_graph(case_id):
    """Graph of a custom-suite case, built in a separate process because the suite is a different dataset."""
    r = subprocess.run([sys.executable, "-I", str(ROOT / "code" / "main.py"), "--dataset", str(SUITE), "--graph", case_id],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    path = HERE / "provenance" / f"{case_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class SuiteGraphs(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(HERE / "provenance", ignore_errors=True)

    def test_historical_identifier_carries_its_source_and_time(self):
        g = suite_graph("custom_01")                    # requested with the OLD address, in capitals
        link = next(e for e in g["edges"] if e["rel"] == "resolves_to")
        self.assertTrue(link["source"].startswith("audit_1"), link)       # the audit-log event that proves the old address
        self.assertIn("exact match", link["basis"])

    def test_the_current_identifier_comes_from_the_identity_snapshot_or_its_change_event(self):
        g = suite_graph("custom_01")
        facts = {(e["to"], e["source"]) for e in g["edges"] if e["rel"] == "has_identifier"}
        self.assertIn(("identifier:ana.new@example.com", "audit_1"), facts)

    def test_a_later_observation_is_kept_but_marked_unused(self):
        g = suite_graph("custom_02")                    # the account is "active again" in a record dated after audit_at
        late = [x for x in g["excluded"] if x["resource"] == "account_9002"]
        self.assertTrue(late and late[0]["reason"] == "recorded after audit_at", g["excluded"])
        used = [n for n in g["nodes"] if n["type"] == "observation" and "account_9002" in n["id"]]
        self.assertTrue(all("2026-03-15" not in n["id"] for n in used))

    def test_a_shared_resource_names_who_shares_it(self):
        g = suite_graph("custom_04")
        node = next(n for n in g["nodes"] if n["id"] == "resource:support_9040")
        self.assertEqual(node.get("shared_with"), ["user_5"])

    def test_every_finding_reaches_a_rule_and_an_evidence_file(self):
        g = suite_graph("custom_16")
        step = next(n for n in g["nodes"] if n["type"] == "step" and "REPORT_REMAINING_RECORD" in n["id"])
        rels = {(e["rel"], e["from"], e["to"]) for e in g["edges"]}
        self.assertIn(("governed_by", "resource:search_9016", "rule:rule_03"), rels)
        self.assertIn(("cited_by", "evidence:evidence_14", step["id"]), rels)


class SampleGraphs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from audit import loaders, provenance
        from audit.pipeline import Pipeline

        cls.provenance = provenance
        cls.pipe = Pipeline()
        cls.reqs = {r["case_id"]: r for r in loaders.load_requests(True)}

    def graph(self, cid):
        return self.provenance.build(self.pipe, self.reqs[cid])

    def test_replica_drift_is_recorded_as_a_conflict(self):
        _, _, g = self.graph("case_05")                 # present in the failover replica, absent from the primary
        drift = [c for c in g.conflicts if c.get("kind") == "replica drift"]
        self.assertTrue(drift, g.conflicts)
        self.assertEqual(drift[0]["resource"], "account_005")

    def test_ambiguity_is_visible_in_the_link_basis(self):
        _, _, g = self.graph("case_21")                 # an identifier-less account matched through the worker's activity
        bases = [e["basis"] for e in g.edges if e["rel"] == "links_via"]
        self.assertTrue(any(b.startswith("worker_alignment:") for b in bases), bases)

    def test_every_step_of_every_sample_traces_back_to_its_request(self):
        for cid in self.reqs:
            status, steps, g = self.graph(cid)
            for s in steps:
                node = f"step:{s['step_no']}:{s['operation']}:{s['resource_id']}"
                paths = g.paths_to(node)
                self.assertTrue(paths, f"{cid} step {s['step_no']} has no path")
                self.assertTrue(any(p and p[0]["from"] == f"request:{cid}" for p in paths), f"{cid} {node} does not reach the request")

    def test_a_cited_file_is_on_a_path_to_its_step(self):
        for cid in ("case_11", "case_26"):
            _, steps, g = self.graph(cid)
            for s in steps:
                if s["evidence_id"]:
                    node = f"step:{s['step_no']}:{s['operation']}:{s['resource_id']}"
                    self.assertIn(f"evidence:{s['evidence_id']}", {e["from"] for p in g.paths_to(node) for e in p})


if __name__ == "__main__":
    unittest.main(verbosity=1)
