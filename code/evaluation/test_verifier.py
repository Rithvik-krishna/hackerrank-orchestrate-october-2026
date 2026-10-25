"""Negative tests for the independent verifier: valid answers are accepted, deliberately broken ones are rejected.

    python code/evaluation/test_verifier.py

The valid answers are the reference answers of the labelled samples; each test breaks one thing about one of them
and checks that the verifier raises an error with the right code and a message that says what is wrong.
"""

import copy
import csv
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from audit import loaders, paths  # noqa: E402
from audit.pipeline import Pipeline  # noqa: E402
from audit.verifier import Verifier  # noqa: E402


class VerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipe = Pipeline()
        cls.ver = Verifier(cls.pipe.store, cls.pipe.manifest, cls.pipe.activity, cls.pipe.rules, cls.pipe.identity,
                           cls.pipe.citable())
        cls.reqs = {r["case_id"]: r for r in loaders.load_requests(True) + loaders.load_requests(False)}
        cls.gold = {}
        with open(paths.DATASET / "sample_output.csv", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                cls.gold[row["case_id"]] = (row["deletion_status"], json.loads(row["steps_json"]))
        for cid in cls.gold:
            cls.pipe.audit(cls.reqs[cid])  # fills pipe.linked

    # ------------------------------------------------------------------ helpers
    def check(self, cid, status=None, steps=None):
        st, sp = copy.deepcopy(self.gold[cid])
        return self.ver.verify_case(self.reqs[cid], status or st, sp if steps is None else steps, self.pipe.linked.get(cid, []))

    def gold_steps(self, cid):
        return copy.deepcopy(self.gold[cid][1])

    def renumber(self, steps):
        for i, s in enumerate(steps, 1):
            s["step_no"] = i
        return steps

    def errors(self, findings):
        return [f for f in findings if f.severity == "error"]

    def assertRejected(self, findings, code, fragment=""):
        hits = [f for f in self.errors(findings) if f.code == code and fragment in f.message]
        self.assertTrue(hits, f"expected a '{code}' error containing {fragment!r}, got "
                              f"{[(f.code, f.message) for f in findings]}")

    # ------------------------------------------------------------------ valid answers pass
    def test_every_reference_sample_answer_is_accepted(self):
        for cid in self.gold:
            self.assertEqual(self.errors(self.check(cid)), [], cid)

    # ------------------------------------------------------------------ evidence
    def test_unknown_evidence_id(self):
        steps = self.gold_steps("case_03")
        steps[0]["evidence_id"] = "evidence_999"
        self.assertRejected(self.check("case_03", steps=steps), "evidence", "unknown id")

    def test_evidence_that_did_not_exist_at_audit_at(self):
        steps = self.gold_steps("case_01")                       # audited 2026-09-03
        steps[0]["evidence_id"] = "evidence_94"                  # captured 2026-09-26
        self.assertRejected(self.check("case_01", steps=steps), "evidence", "did not exist at audit_at")

    def test_cited_file_that_does_not_contain_the_resource(self):
        steps = self.gold_steps("case_03")
        steps[0]["evidence_id"] = "evidence_92"                  # exists, was available, never mentions account_7566
        self.assertRejected(self.check("case_03", steps=steps), "evidence", "does not contain account_7566")

    def test_cited_file_shows_a_different_state(self):
        steps = self.gold_steps("case_26")
        # claim the backup is blocked while citing the Aug archive console that shows restorable archives
        steps[-1]["evidence_id"] = "evidence_32"
        self.assertTrue(self.errors(self.check("case_26", steps=steps)))

    # ------------------------------------------------------------------ false COMPLETE
    def test_complete_while_the_record_is_still_in_a_replica(self):
        # case_05 really is INCOMPLETE: the account is still in the failover replica (evidence_54)
        steps = [{"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_005",
                  "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "evidence_54"}]
        found = self.check("case_05", status="COMPLETE", steps=steps)
        self.assertRejected(found, "false-complete", "account_005")
        self.assertRejected(found, "evidence", "shows ['PRESENT']")

    def test_complete_with_no_proof(self):
        self.assertRejected(self.check("case_03", status="COMPLETE", steps=[]), "false-complete", "proves nothing")

    def test_complete_holding_a_finding(self):
        steps = self.gold_steps("case_11")                        # an INCOMPLETE answer relabelled COMPLETE
        self.assertRejected(self.check("case_11", status="COMPLETE", steps=steps), "status", "only evidence-backed verification")

    def test_complete_step_without_evidence(self):
        steps = self.gold_steps("case_03")
        steps[1]["evidence_id"] = ""
        found = self.check("case_03", steps=steps)
        self.assertRejected(found, "status")
        self.assertRejected(found, "schema")

    # ------------------------------------------------------------------ chain and coverage
    def test_finding_without_remediation(self):
        steps = [s for s in self.gold_steps("case_05") if s["operation"] != "DELETE_RECORD"]
        self.assertRejected(self.check("case_05", steps=self.renumber(steps)), "chain", "no DELETE_RECORD")

    def test_finding_without_verification(self):
        steps = [s for s in self.gold_steps("case_05") if s["phase"] != "VERIFICATION"]
        self.assertRejected(self.check("case_05", steps=self.renumber(steps)), "chain", "no VERIFY_RECORD_ABSENT")

    def test_remediation_without_finding(self):
        steps = [s for s in self.gold_steps("case_05") if s["phase"] != "FINDING"]
        self.assertRejected(self.check("case_05", steps=self.renumber(steps)), "chain", "without a finding")

    def test_future_work_must_not_cite_evidence(self):
        steps = self.gold_steps("case_05")
        steps[1]["evidence_id"] = "evidence_54"
        self.assertRejected(self.check("case_05", steps=steps), "chain", "future work must not cite")

    def test_wrong_treatment_for_the_category(self):
        steps = self.gold_steps("case_16")                        # a search entry: the rules say DELETE
        for s in steps:
            if s["resource_id"] == "search_1007" and s["operation"] == "DELETE_RECORD":
                s["operation"], s["state"] = "REDACT_RECORD", "REDACTED"
        self.assertRejected(self.check("case_16", steps=steps), "chain", "the rules require DELETE_RECORD")

    def test_a_violation_the_answer_does_not_mention(self):
        steps = [s for s in self.gold_steps("case_20") if s["resource_id"] != "data_flow_1770"]   # drop the active flow
        self.assertRejected(self.check("case_20", steps=self.renumber(steps)), "coverage", "data_flow_1770")

    def test_retained_record_not_verified(self):
        steps = [s for s in self.gold_steps("case_11") if s["operation"] != "VERIFY_RECORD_PRESENT"]
        self.assertRejected(self.check("case_11", steps=self.renumber(steps)), "retention", "audit_record_9394")

    # ------------------------------------------------------------------ order, numbering, status
    def test_steps_out_of_order(self):
        steps = self.gold_steps("case_11")
        steps[0], steps[1] = steps[1], steps[0]
        self.assertRejected(self.check("case_11", steps=self.renumber(steps)), "order")

    def test_non_consecutive_numbering(self):
        steps = self.gold_steps("case_05")
        steps[2]["step_no"] = 7
        self.assertRejected(self.check("case_05", steps=steps), "schema", "step_no")

    def test_not_applicable_with_steps(self):
        found = self.ver.verify_case(self.reqs["case_533"], "NOT_APPLICABLE", self.gold_steps("case_03"), [])
        self.assertRejected(found, "status", "no steps")

    def test_not_applicable_for_a_deletion_request(self):
        self.assertRejected(self.ver.verify_case(self.reqs["case_33"], "NOT_APPLICABLE", [], []), "status", "asks for deletion")

    def test_audited_a_request_that_is_not_a_deletion(self):
        self.assertRejected(self.ver.verify_case(self.reqs["case_533"], "INCOMPLETE", [], []), "status", "not a deletion request")

    def test_a_correct_not_applicable_is_accepted(self):
        self.assertEqual(self.errors(self.ver.verify_case(self.reqs["case_533"], "NOT_APPLICABLE", [], [])), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
