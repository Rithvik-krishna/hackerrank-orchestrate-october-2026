"""Direct tests of the "salvage and flag" parser behaviour (audit/diagnostics.py).

    python code/evaluation/test_parsers.py

Item-level damage (one line, statement or record) is skipped and reported with its position while the valid items
around it are kept; damage that makes record boundaries untrustworthy fails the whole file; a partly readable file can
no longer prove absence; the degradation reaches the ledger, the provenance graph and the diagnostics file. No model,
no network: everything runs on small files written to a temporary folder.
"""

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
SUITE = Path(__file__).resolve().parent / "custom_cases" / "suite"
sys.path.insert(0, str(CODE))

from audit import ledger  # noqa: E402
from audit.diagnostics import ITEM, Salvage, UnparseableFile  # noqa: E402
from audit.evidence import EvidenceStore, json_records, logs, sql_snapshots, text_records  # noqa: E402
from audit.model import ABSENT, PRESENT, UNKNOWN, Coverage, Observation  # noqa: E402
from audit.state import resolve_state  # noqa: E402

T = datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc)
GOOD_START = "2026-03-08T09:10:00Z level=DEBUG service=account_service trace_id=t1 event=profile_lookup_started resource_id=account_9 lookup_key=a@example.com"
GOOD_DONE = "2026-03-08T09:10:01Z level=INFO service=account_service trace_id=t1 event=profile_lookup_completed resource_id=account_9 rows=0 http_status=404"
SECOND = ("2026-03-08T09:20:00Z level=INFO service=search_service trace_id=t2 event=index_query_completed resource_id=search_7 "
          "result=hit total_hits=1 email=a@example.com")
CREATE = ("CREATE TABLE customer_profiles_primary (resource_id TEXT PRIMARY KEY, user_id TEXT, full_name TEXT, email TEXT, "
          "phone TEXT, account_status TEXT, updated_at TEXT);")


def insert(n: int) -> str:
    return (f"INSERT INTO \"customer_profiles_primary\" VALUES('account_{n}','user_{n}','Name {n}','n{n}@example.com',"
            f"'+1 555 {n}','ACTIVE','2026-03-08T08:00:00Z');")


def run_parser(parser, name: str, content: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_text(content, encoding="utf-8")
        return parser("evidence_x", path, T)


def skipped_of(result) -> list:
    return list(result[2]) if len(result) > 2 and isinstance(result[2], Salvage) else []


class LogDamage(unittest.TestCase):
    def test_damaged_json_line_is_skipped_and_neighbours_kept(self):
        bad = '2026-03-08T09:15:00Z {"event": "profile_lookup_completed", "resource_id": '
        res = run_parser(logs.parse, "x.log", "\n".join([GOOD_START, bad, GOOD_DONE, SECOND]) + "\n")
        rids = {o.resource_id: o.status for o in res[0]}
        self.assertEqual(rids, {"account_9": ABSENT, "search_7": PRESENT})   # lines before and after survive
        (d,) = skipped_of(res)
        self.assertEqual((d.level, d.parser, d.location, d.category), (ITEM, "log", "line 2", "malformed_line"))
        self.assertEqual(d.evidence_id, "evidence_x")

    def test_clean_file_has_no_diagnostics(self):
        res = run_parser(logs.parse, "x.log", GOOD_START + "\n" + GOOD_DONE + "\n")
        self.assertEqual(len(res), 2)   # the contract is unchanged when nothing was skipped

    def test_blank_and_continuation_lines_are_not_errors(self):
        res = run_parser(logs.parse, "x.log", "\n   \nstack trace line\n" + GOOD_START + "\n" + GOOD_DONE + "\n")
        self.assertEqual(skipped_of(res), [])

    def test_entry_without_a_readable_timestamp_is_reported(self):
        res = run_parser(logs.parse, "x.log", "2026-99-99T99:99:99Z level=INFO event=x\n" + GOOD_START + "\n" + GOOD_DONE + "\n")
        (d,) = skipped_of(res)
        self.assertIn(d.category, ("malformed_line", "unrecognised_line"))
        self.assertEqual(d.location, "line 1")
        self.assertEqual(len(res[0]), 1)


class SqlDamage(unittest.TestCase):
    def dump(self, *lines: str) -> str:
        return "-- Captured at: 2026-03-08T09:00:00Z\n" + "\n".join(["BEGIN TRANSACTION;", CREATE, *lines, "COMMIT;"]) + "\n"

    def test_bad_statement_is_skipped_and_neighbours_kept(self):
        res = run_parser(sql_snapshots.parse, "x.sql", self.dump(insert(1), "INSERT INTO nowhere VALUES(1);", insert(2)))
        self.assertEqual({o.resource_id for o in res[0]}, {"account_1", "account_2"})
        (d,) = skipped_of(res)
        self.assertEqual((d.category, d.parser), ("sql_error", "sql"))
        self.assertIn("line 5", d.location)   # comment, BEGIN, CREATE, insert(1), then the bad one on line 5

    def test_unterminated_statement_cannot_swallow_the_next_lines(self):
        broken = "INSERT INTO \"customer_profiles_primary\" VALUES('account_5','user_5','Name"
        res = run_parser(sql_snapshots.parse, "x.sql", self.dump(insert(1), broken, insert(2)))
        self.assertEqual({o.resource_id for o in res[0]}, {"account_1", "account_2"})
        self.assertEqual([d.category for d in skipped_of(res)], ["unterminated_statement"])

    def test_truncated_file_reports_the_last_statement(self):
        text = self.dump(insert(1), insert(2))
        res = run_parser(sql_snapshots.parse, "x.sql", text[: text.index("COMMIT") - 40])
        self.assertEqual([d.category for d in skipped_of(res)], ["truncated_statement"])
        self.assertEqual({o.resource_id for o in res[0]}, {"account_1"})

    NOTE_TABLE = ("CREATE TABLE support_records (resource_id TEXT PRIMARY KEY, user_id TEXT, message_text TEXT, "
                  "updated_at TEXT);")

    def note(self, n: int, text: str) -> str:
        return f"INSERT INTO \"support_records\" VALUES('support_{n}','user_{n}','{text}','2026-03-08T08:00:00Z');"

    def test_multi_line_string_values_are_read_whole(self):
        tricky = "first line\nINSERT INTO fake VALUES(1);\nCOMMIT;\nlast line"   # lines that start like statements
        text = "BEGIN TRANSACTION;\n" + self.NOTE_TABLE + "\n" + self.note(1, tricky) + "\n" + self.note(2, "plain") + "\nCOMMIT;\n"
        res = run_parser(sql_snapshots.parse, "x.sql", text)
        self.assertEqual(skipped_of(res), [])   # a valid dump is not salvaged, so nothing is misjudged
        self.assertEqual({o.resource_id for o in res[0]}, {"support_1", "support_2"})
        self.assertEqual(len(res), 2)

    def test_multi_line_values_survive_damage_elsewhere(self):
        multi = "line one\nline two\n  indented line three"
        text = ("BEGIN TRANSACTION;\n" + self.NOTE_TABLE + "\n" + self.note(1, multi) + "\nINSERT INTO nowhere VALUES(1);\n"
                + self.note(2, "after") + "\nCOMMIT;\n")
        res = run_parser(sql_snapshots.parse, "x.sql", text)
        self.assertEqual({o.resource_id for o in res[0]}, {"support_1", "support_2"})
        (d,) = skipped_of(res)
        self.assertEqual(d.category, "sql_error")
        self.assertIn("line 6", d.location)   # BEGIN, CREATE, then the three physical lines of support_1's value, then the bad one

    def test_a_file_where_nothing_runs_fails_as_a_file(self):
        with self.assertRaises(UnparseableFile) as ctx:
            run_parser(sql_snapshots.parse, "x.sql", "this is not sql at all;\nneither is this;\n")
        self.assertEqual(ctx.exception.category, "not_sql")


class JsonDamage(unittest.TestCase):
    GOOD = {"recorded_at": "2026-03-08T08:00:00Z", "system": "account_service", "resource_id": "account_{}",
            "record_type": "USER_PROFILE", "result": "FOUND", "subject_reference": "a@example.com"}

    def rec(self, n: int) -> dict:
        return {**self.GOOD, "resource_id": f"account_{n}"}

    def flat(self, records: list) -> str:
        return json.dumps({"exported_at": "2026-03-08T10:00:00Z", "records": records})

    def test_record_without_resource_id_is_skipped_and_neighbours_kept(self):
        no_id = {k: v for k, v in self.rec(0).items() if k != "resource_id"}
        res = run_parser(json_records.parse, "x.json", self.flat([self.rec(1), no_id, self.rec(2)]))
        self.assertEqual([o.resource_id for o in res[0]], ["account_1", "account_2"])
        (d,) = skipped_of(res)
        self.assertEqual((d.location, d.category), ("records[1]", "missing_resource_id"))

    def test_record_that_is_not_an_object(self):
        res = run_parser(json_records.parse, "x.json", self.flat([self.rec(1), 42, "text"]))
        self.assertEqual([(d.location, d.category) for d in skipped_of(res)],
                         [("records[1]", "not_an_object"), ("records[2]", "not_an_object")])
        self.assertEqual(len(res[0]), 1)

    def test_truncated_json_fails_the_file(self):
        with self.assertRaises(UnparseableFile) as ctx:
            run_parser(json_records.parse, "x.json", self.flat([self.rec(1), self.rec(2)])[:-30])
        self.assertEqual(ctx.exception.category, "invalid_json")

    def test_unrecognised_layout_fails_the_file(self):
        for content in ("[1, 2, 3]", json.dumps({"something": "else"})):
            with self.assertRaises(UnparseableFile):
                run_parser(json_records.parse, "x.json", content)

    def test_bundle_snapshot_and_record_damage_keep_the_rest(self):
        bundle = {"exported_at": "2026-03-08T10:00:00Z", "snapshots": [
            {"captured_at": "2026-03-08T10:00:00Z", "data_store": "account_service", "collection": "customer_profiles",
             "records": [{"resource_id": "account_1"}, {"name": "no id"}, {"resource_id": "account_2"}]},
            {"data_store": "search_service", "collection": "x", "records": []},   # no captured_at
            {"captured_at": "2026-03-08T10:00:00Z", "data_store": "search_service", "collection": "search_documents",
             "records": [{"resource_id": "search_3"}]}]}
        res = run_parser(json_records.parse, "x.json", json.dumps(bundle))
        self.assertEqual([o.resource_id for o in res[0]], ["account_1", "account_2", "search_3"])
        self.assertEqual([(d.location, d.category) for d in skipped_of(res)],
                         [("snapshots[0].records[1]", "missing_resource_id"), ("snapshots[1]", "bad_snapshot")])

    def test_nothing_is_invented_for_a_skipped_record(self):
        no_id = {k: v for k, v in self.rec(0).items() if k != "resource_id"}
        res = run_parser(json_records.parse, "x.json", self.flat([no_id]))
        self.assertEqual(res[0], [])


class TextDamage(unittest.TestCase):
    def test_malformed_row_is_reported_and_valid_rows_kept(self):
        good = "- 2026-03-08T08:00:00Z | account_service | account_1 | USER_PROFILE | FOUND | a@example.com"
        res = run_parser(text_records.parse, "x.txt", "Handover notes\n" + good + "\n- 2026-03-08 | too | few\n" + good.replace("account_1", "account_2") + "\n")
        self.assertEqual([o.resource_id for o in res[0]], ["account_1", "account_2"])
        (d,) = skipped_of(res)
        self.assertEqual((d.location, d.category), ("line 3", "malformed_row"))


class DegradedFilesCannotProveAbsence(unittest.TestCase):
    def setUp(self):
        self.absent = Observation("evidence_x", T, "account_service", "account_9", status=ABSENT, category="USER_PROFILE")
        self.skipped = Salvage([])

    def test_explicit_absence_is_demoted_and_coverage_dropped(self):
        store = EvidenceStore()
        obs, cov, note = store._degrade("evidence_x", "evidence/logs/x.log", [self.absent], self.skipped)
        self.assertEqual(obs[0].status, UNKNOWN)
        self.assertEqual((obs[0].attrs["degraded"], obs[0].attrs["demoted_from"]), (True, ABSENT))
        self.assertEqual(cov, [])
        self.assertIn("degraded", note)
        self.assertIn("evidence_x", store.degraded)

    def test_a_demoted_observation_cannot_make_the_state_absent(self):
        store = EvidenceStore()
        store._degrade("evidence_x", "evidence/logs/x.log", [self.absent], self.skipped)
        store.by_resource[("account_service", "account_9")].append(self.absent)
        state = resolve_state(store, ("account_service", "account_9"), T, "USER_PROFILE", omit_ok=True)
        self.assertNotEqual(state.status, ABSENT)

    def test_the_same_observation_from_a_clean_file_does_prove_absence(self):
        store = EvidenceStore()
        store.by_resource[("account_service", "account_9")].append(self.absent)
        state = resolve_state(store, ("account_service", "account_9"), T, "USER_PROFILE", omit_ok=True)
        self.assertEqual(state.status, ABSENT)

    def test_omission_from_a_full_table_dump_is_ignored_when_the_file_is_degraded(self):
        store = EvidenceStore()
        cov = Coverage("evidence_x", T, "account_service", "customer_profiles_primary", frozenset({"account_1"}), scope="full")
        store.coverage.append(cov)
        before = resolve_state(store, ("account_service", "account_9"), T, "USER_PROFILE", omit_ok=True)
        self.assertEqual(before.status, ABSENT)   # a clean full dump does prove it
        store.coverage.clear()
        store._degrade("evidence_x", "evidence/databases/x.sql", [], self.skipped)   # a degraded file contributes no coverage
        after = resolve_state(store, ("account_service", "account_9"), T, "USER_PROFILE", omit_ok=True)
        self.assertEqual(after.status, UNKNOWN)


class ReadingStates(unittest.TestCase):
    """What a log line says about a record's state, and how a line that says nothing is treated."""

    def test_read_and_loaded_events_mean_the_record_exists(self):
        for event in ("message_read", "retained_order_loaded"):
            self.assertEqual(logs._status(event, {"resource_id": "r"}), PRESENT, event)

    def test_a_failure_event_says_nothing_about_the_state(self):
        self.assertEqual(logs._status("thumbnail_failed", {"resource_id": "r", "reason": "unsupported_media"}), UNKNOWN)

    def test_a_stateless_observation_does_not_supersede_an_informative_one(self):
        store = EvidenceStore()
        key = ("support_platform", "support_1")
        early = Observation("evidence_1", T.replace(hour=8), key[0], key[1], status=PRESENT, category="SUPPORT_ATTACHMENT")
        late = Observation("evidence_2", T.replace(hour=9), key[0], key[1], status=UNKNOWN, category="SUPPORT_ATTACHMENT")
        store.by_resource[key] += [early, late]
        state = resolve_state(store, key, T, "SUPPORT_ATTACHMENT")
        self.assertEqual((state.status, state.evidence_id), (PRESENT, "evidence_1"))

    def test_when_every_observation_is_stateless_the_state_stays_unknown(self):
        store = EvidenceStore()
        key = ("support_platform", "support_2")
        store.by_resource[key].append(Observation("evidence_2", T.replace(hour=9), key[0], key[1], status=UNKNOWN))
        self.assertEqual(resolve_state(store, key, T, "SUPPORT_ATTACHMENT").status, UNKNOWN)


class LedgerFlag(unittest.TestCase):
    def test_a_linked_record_that_could_not_be_verified_caps_the_confidence(self):
        meta = ledger.case_meta("COMPLETE", [], [], [], "DELETE", None, ["account_445 [USER_PROFILE]"])
        self.assertEqual(meta["unverified"], ["account_445 [USER_PROFILE]"])
        self.assertIn("unverifiable", " ".join(meta["notes"]))
        self.assertEqual(ledger.case_meta("NOT_APPLICABLE", [], [], [], "NOT_DELETE", None, ["x"])["confidence"], "high")

    def test_high_confidence_is_capped_at_medium(self):
        clean = ledger.case_meta("COMPLETE", [], [], [], "DELETE")
        degraded = ledger.case_meta("COMPLETE", [], [], [], "DELETE", ["evidence_02"])
        self.assertEqual(degraded["degraded"], ["evidence_02"])
        self.assertIn("evidence_02", " ".join(degraded["notes"]))
        self.assertEqual(clean["degraded"], [])

    def test_a_non_deletion_answer_is_not_affected(self):
        meta = ledger.case_meta("NOT_APPLICABLE", [], [], [], "NOT_DELETE", ["evidence_02"])
        self.assertEqual(meta["confidence"], "high")


class EndToEnd(unittest.TestCase):
    """The custom suite with one damaged log line: only the case that relied on the line changes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.clean = cls.tmp / "clean"
        cls.damaged = cls.tmp / "damaged"
        shutil.copytree(SUITE, cls.clean)
        shutil.copytree(SUITE, cls.damaged)
        for folder in (cls.clean, cls.damaged):
            # custom_19's full table dump would independently prove custom_02's account absent (a clean file may do that);
            # take it out so the damaged line is the only proof in play
            manifest = folder / "evidence_files.csv"
            with open(manifest, encoding="utf-8", newline="") as fh:
                rows = [r for r in csv.reader(fh) if r[0] != "evidence_17"]
            with open(manifest, "w", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerows(rows)
        log = cls.damaged / "evidence" / "logs" / "e02.log"
        lines = log.read_text(encoding="utf-8").splitlines()
        lines[1] = '2026-03-05T09:50:01Z {"event": "profile_lookup_completed", "resource_id": '   # the line that proved the 404
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for name, folder in (("clean", cls.clean), ("damaged", cls.damaged)):
            r = subprocess.run([sys.executable, "-I", str(CODE / "main.py"), "--dataset", str(folder),
                                "--output", str(cls.tmp / f"{name}.csv")], capture_output=True, text=True)
            setattr(cls, name + "_run", r)
        cls.rows = {}
        for n in ("clean", "damaged"):
            with open(cls.tmp / f"{n}.csv", encoding="utf-8", newline="") as fh:
                cls.rows[n] = {r["case_id"]: r for r in csv.DictReader(fh)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_clean_run_has_no_diagnostics(self):
        with open(self.tmp / "clean.parse_diagnostics.csv", encoding="utf-8", newline="") as fh:
            self.assertEqual(list(csv.DictReader(fh)), [])
        self.assertNotIn("partly readable", self.clean_run.stdout)

    def test_the_damage_is_reported_with_its_position(self):
        with open(self.tmp / "damaged.parse_diagnostics.csv", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["evidence_id"], rows[0]["location"], rows[0]["category"], rows[0]["level"]),
                         ("evidence_02", "line 2", "malformed_line", "item"))
        self.assertTrue(rows[0]["source_file"].endswith("e02.log"))
        self.assertIn("partly readable", self.damaged_run.stdout)

    def test_missing_evidence_is_not_proof_of_deletion(self):
        self.assertEqual(self.rows["clean"]["custom_02"]["deletion_status"], "COMPLETE")
        self.assertNotEqual(self.rows["damaged"]["custom_02"]["deletion_status"], "COMPLETE")

    def test_every_other_case_is_unchanged(self):
        for cid, row in self.rows["clean"].items():
            if cid != "custom_02":
                self.assertEqual(self.rows["damaged"][cid], row, cid)

    def test_the_ledger_names_the_degraded_file(self):
        with open(self.tmp / "damaged.confidence.csv", encoding="utf-8", newline="") as fh:
            meta = {r["case_id"]: r for r in csv.DictReader(fh)}
        self.assertIn("evidence_02", meta["custom_02"]["degraded_evidence"])
        self.assertNotEqual(meta["custom_02"]["confidence"], "high")
        self.assertEqual(meta["custom_01"]["degraded_evidence"], "")

    def test_the_provenance_graph_shows_the_degradation(self):
        r = subprocess.run([sys.executable, "-I", str(CODE / "main.py"), "--dataset", str(self.damaged), "--graph", "custom_02"],
                           capture_output=True, text=True)
        self.assertIn("degraded evidence", r.stdout)
        self.assertIn("evidence_02 line 2: malformed_line", r.stdout)


class DegradationScenarios(unittest.TestCase):
    """Who may prove an absence: a clean file may, a partly readable one may not.

    custom_02's account is proven absent by a 404 line in evidence_02 and, independently, by its omission from the full
    account table dump in evidence_17 (captured before the audit time). Damage is applied to either source or both."""

    tmp: Path
    result: dict[str, dict[str, Any]]
    ACCOUNT_ROW = "INSERT INTO \"customer_profiles_failover_replica\" VALUES('account_9019'"

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.result = {name: cls.run_variant(name, log=log, sql=sql)
                      for name, log, sql in (("clean", False, False), ("log_only", True, False), ("sql_only", False, True),
                                             ("both", True, True))}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_variant(cls, name: str, log: bool, sql: bool) -> dict:
        folder = cls.tmp / name
        shutil.copytree(SUITE, folder)
        if log:   # the 404 line of custom_02's account lookup is damaged
            path = folder / "evidence" / "logs" / "e02.log"
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[1] = '2026-03-05T09:50:01Z {"event": "profile_lookup_completed", "resource_id": '
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if sql:   # one INSERT of the account table dump loses its closing quote and parenthesis
            path = folder / "evidence" / "databases" / "accounts_20260308.sql"
            text = path.read_text(encoding="utf-8")
            start = text.index(cls.ACCOUNT_ROW)
            end = text.index("\n", start)
            path.write_text(text[:start] + text[start:end].split(",'Rui Sato'")[0] + text[end:], encoding="utf-8")
        out = cls.tmp / f"{name}.csv"
        proc = subprocess.run([sys.executable, "-I", str(CODE / "main.py"), "--dataset", str(folder), "--output", str(out)],
                              capture_output=True, text=True)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = {r["case_id"]: r for r in csv.DictReader(fh)}
        with open(cls.tmp / f"{name}.confidence.csv", encoding="utf-8", newline="") as fh:
            meta = {r["case_id"]: r for r in csv.DictReader(fh)}
        with open(cls.tmp / f"{name}.parse_diagnostics.csv", encoding="utf-8", newline="") as fh:
            diags = list(csv.DictReader(fh))
        return {"rows": rows, "meta": meta, "diags": diags, "stdout": proc.stdout}

    def steps(self, variant, case):
        return json.loads(self.result[variant]["rows"][case]["steps_json"])

    def test_clean_evidence_establishes_the_absence(self):
        self.assertEqual(self.result["clean"]["rows"]["custom_02"]["deletion_status"], "COMPLETE")
        self.assertEqual(self.result["clean"]["diags"], [])

    def test_a_damaged_log_alone_does_not_remove_the_independent_proof(self):
        # the 404 line is gone, but the clean table dump still shows the account is not in the table
        steps = self.steps("log_only", "custom_02")
        self.assertEqual(self.result["log_only"]["rows"]["custom_02"]["deletion_status"], "COMPLETE")
        self.assertEqual([(s["operation"], s["resource_id"], s["evidence_id"]) for s in steps],
                         [("VERIFY_RECORD_ABSENT", "account_9002", "evidence_17")])   # cites the clean file, not the damaged one
        self.assertIn("evidence_02", self.result["log_only"]["meta"]["custom_02"]["degraded_evidence"])
        self.assertNotEqual(self.result["log_only"]["meta"]["custom_02"]["confidence"], "high")

    def test_a_damaged_table_dump_cannot_prove_absence(self):
        sql = self.result["sql_only"]
        self.assertEqual([d["category"] for d in sql["diags"]], ["unterminated_statement"])
        self.assertEqual(sql["diags"][0]["evidence_id"], "evidence_17")
        # custom_02 is still proven by its own 404 line, which is clean
        self.assertEqual(sql["rows"]["custom_02"]["deletion_status"], "COMPLETE")
        self.assertEqual([s["evidence_id"] for s in self.steps("sql_only", "custom_02")], ["evidence_02"])

    def test_with_both_sources_damaged_nothing_proves_the_absence(self):
        both = self.result["both"]
        self.assertNotEqual(both["rows"]["custom_02"]["deletion_status"], "COMPLETE")
        self.assertEqual(self.steps("both", "custom_02"), [])
        self.assertEqual({d["evidence_id"] for d in both["diags"]}, {"evidence_02", "evidence_17"})

    def test_the_lost_row_is_never_replaced_by_a_claim(self):
        # custom_19's account row was the one that got damaged: it must not turn into "absent" or "compliant"
        steps = self.steps("sql_only", "custom_19")
        self.assertFalse(any(s["resource_id"] == "account_9019" and s["operation"] in ("VERIFY_RECORD_ABSENT", "DELETE_RECORD")
                             and s["evidence_id"] == "evidence_17" for s in steps))
        self.assertNotEqual(self.result["sql_only"]["rows"]["custom_19"]["deletion_status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
