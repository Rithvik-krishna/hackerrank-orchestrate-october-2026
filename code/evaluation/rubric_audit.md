# Rubric audit (honest self-assessment)

Rubric for the code zip: agent architecture, prompt/tool craft, robustness, engineering rigor.

## Agent architecture
- The core is a **deterministic workflow**, not an LLM agent: intent rules, identity index, evidence parsers, linker,
  state reconstruction, policy, step builder, validator. This is deliberate: every step cites evidence and is reproducible.
- An optional model layer exists (intent arbitration, vision, adjudicator, tool-calling investigator) behind
  deterministic guardrails. **Measured effect on accuracy: none.** The real-model run (free-tier Gemini) returned NONE on
  every ambiguous group it finished; the investigator mostly stopped on quota (HTTP 429). `output.csv` does not depend on it.
- Separation of concerns: an independent verifier (`audit/verifier.py`) re-checks every answer without reusing the
  engine's state or policy code, and gates the run.

## Prompt / tool craft
- Forced tool calls with JSON-schema validation, a repair loop, retries with Retry-After, caching, a usage ledger.
- Investigator tools are read-only and size-capped; model output is only a proposal and must cite ids returned by its own tool calls.
- Text from the audited systems is fenced as untrusted data.

## Robustness
- Seven log dialects, six languages, SQL/JSON/PDF/text/PNG evidence. Damaged items are skipped, reported with their position and flagged (a degraded file cannot prove absence and caps the affected cases at medium); unreadable files fail the run (test_parsers.py, 27 tests).
- Strict time rules (nothing after `audit_at` is used), exact identifier matching only.
- Adversarial/custom suite (23 cases: negation, an instruction injected in the request text and in evidence, replication
  drift, a Japanese request, shared attachments, time-cutoff traps) and 15 rule mutations, each caught by at least one case.

## Engineering rigor
- Tests: 52 + 22 + 9 + 5, all offline. Regression baseline of exactly-right samples. Lint gate: no unused imports.
- Top fixes made in this audit: (1) parse failures are visible and fatal; (2) every function now fits in 60 lines
  (10 were longer; the long report builders were split into section functions, `main.main`, `linker._align`,
  `validate_rows`, `investigator.resolve` and `provenance.build` into steps, with `output.csv` byte-identical before and
  after); (3) every function in `audit/` is type-annotated (170 of 170, from 0) and mypy with strict flags (`code/mypy.ini`) reports no
  issues in `audit/`; its first run found 260 errors, among them wrong annotations such as a function declared to return a
  dict that returns a tuple. mypy does not cover `evaluation/` or `tools/`, and passing it is not evidence of correctness.

## Known gaps (do not hide these)
5 of 32 sample cases still differ in one verification step each (all with the correct status). They were re-investigated
in this pass; none has a legitimate general fix, and none was patched:
- `account_004` (case_04): the id appears nowhere in `dataset/` except the answer key (searched all text evidence, the
  activity stream and the audit log). The gold cites `evidence_54`, a replica snapshot that lists other accounts only,
  so the only way to name it is to derive `account_004` from `user_04`; that does not hold for the other cases (case_18's
  account is `account_5521` for `user_50`), so it would be a guess dressed as a rule.
- `audit_record_6645` (case_18) and `audit_record_5569` (case_20): the ids occur nowhere in the data either. The digits
  recur in unrelated records (`account_6645`, `support_5569`), which are decoys for other cases, so matching on digits would
  create false links.
- case_23 (`account_410` vs `account_7015`): a genuine three-way tie in the worker rows; the data contains no signal that
  separates them, so the pick is a coin flip (the ledger marks it low confidence).
- case_32 (`account_8858` vs `account_2381`): separable only with ownership evidence dated after the cutoff. The lenient
  setting `AUDIT_OWNERSHIP=any` does that and fixes it, but the rules forbid later evidence from changing a case's answer,
  so the strict setting stays the default and the cost is one case.
Fitting any of these to the answer key would make the 518-case output worse in expectation, so they are left as measured
limits.

Re-checked when a peer reported 1.0 on every sample metric; every check is a subcommand of `code/tools/gap_probes.py`:
- `pdfs`: the four PDFs, decoded, hold none of the three ids (the security extract names `audit_record_003` for `user_03`, a
  different case). The security-audit screenshot (`evidence_37`) is for `user_03` as well.
- `conventions`: resource numbers do not follow user numbers. Over all parsed evidence only 4 of 42 account pairs and 1 of
  457 audit-record pairs share a number, so "account_<user number>" is not a rule the data supports. `account_004`,
  `account_5521`, `audit_record_6645` and `audit_record_5569` appear in no parsed evidence at all.
- `lag`: the worker row that belongs to a case lags `reported_at` by 45 to 74 minutes, and its rivals in the same window by
  44 to 75, so timing cannot separate the tied rows either.
- `shape`: the 518 answers look like the labelled ones (mean 7.6 steps for INCOMPLETE against 7.2, 3.3 against 3.4 for
  COMPLETE, 70% INCOMPLETE in both) and none is INCOMPLETE without steps.
A perfect score on the 32 samples is only reachable by reading `sample_output.csv` back into the answers (the five ids exist
nowhere else). That is a hardcoded golden label, which `AGENTS.md` section 6.7 forbids, and it would not carry over to the
518 evaluation cases, whose ids differ.

## Overfitting audit (what was tuned on the 32 samples, and how much the answers depend on it)
- **Hardcoded ids:** none. `audit/` contains no case, user, account or evidence id in logic. Two prompts used real dataset ids as
  format examples (`evidence_92`, `account_5762`, `search_428`, `data_flow_1121`); they are now obviously synthetic
  (`evidence_00`, `account_0000`, ...). This changes the adjudicator and vision prompts, so cached model answers from the
  earlier prompts no longer replay (that cache is not packaged and never feeds `output.csv`).
- **Worker-alignment windows** (`ACCOUNT_WINDOW`, `POLICY_WINDOW` in `audit/linker.py`) are the only numeric constants that
  shape answers. They follow the data: 495 of the 504 accepted account rows lag `reported_at` by 30 to 75 minutes (median 59; the
  other nine were found through evidence, not alignment), and the window reaches 90. `gap_probes.py sensitivity` moves them: halving the account window drops the samples from 27 to 19
  exactly right and changes 242 of the 518 answers; doubling it keeps 27 and changes 36 (rivals enter the pool, no case
  gains an account); the policy window changes 18 to 31 answers over x0.5 to x4. No status changes in any variant. So the
  windows are neither arbitrary nor free: they sit just outside the observed lags and the answers depend on them.
- **Tie-break by case order** (`AUDIT_ZIP_MODE=order`) is the one rule whose benefit was measured on the samples only (row
  exact 0.844 against 0.625 for `unique`); it touches 128 steps of the 518, all labelled low confidence in the ledger.
- **Intent rules:** all 550 texts classify cleanly (540 DELETE, 10 NOT_DELETE); the 10 non-deletion texts are 10 different
  templates, each caught by its own negation pattern, so the patterns mirror the families present. Texts with neither a
  deletion nor a negation cue would go to the optional model, and none occurs. Generalisation to unseen phrasing is covered
  only by custom_11, custom_17 and the hand-written texts in `tools/live_check.py`.
- **Evidence-reading tables** (resource prefix to category, log event to category) come from the files present; every
  evidence file the 518 answers use is in `dataset/`, so there is no unseen evidence format, but a new source would need new
  entries.

## Ambiguities found and resolved in this pass
- A log line that names a record but carries no state (`message_read`, `retained_order_loaded`, `thumbnail_failed`) was
  treated as the latest word on the record, hiding earlier informative evidence and dropping the record from the answer.
  Two general rules fix it: a read/loaded event means the record exists (its content shows in the line, e.g.
  `message_text="[REDACTED]"`), and an observation whose state is UNKNOWN never supersedes one that has a state (the verifier
  applies the same rule independently). Effect: 6 of the 518 answers gain one verification step (5 `VERIFY_RECORD_REDACTED`,
  1 `VERIFY_RECORD_PRESENT`), no status changes, the 32 samples are unchanged. Custom cases 21 and 22 and two mutations pin it.
- 29 linked records in 23 of the 518 cases (11 of them answered COMPLETE) still have no observation at all (worker-matched
  accounts, support records, backups and the like). They are left out of the answer because the engine cannot assert what
  no evidence shows; this used to be silent, and one such COMPLETE case carried a "high" confidence label. They are now
  listed per case in `confidence.csv` (`unverified_records`) and cap the case at medium. Every one of them is a record that
  only the deletion worker's activity stream shows (no evidence file states it, and no table dump that could prove it absent
  existed by the audit time). In the 12 INCOMPLETE cases whose unverified record is an account, the answer now carries that
  account's re-verification step like every other account (all 22 labelled INCOMPLETE answers carry theirs; the check is
  future work, so it needs no evidence); the COMPLETE answers stay as they were, since a COMPLETE check must cite proof they
  lack. Whether those COMPLETE answers are right depends on records nothing in the data shows; no sample has this shape, so it
  cannot be tuned and is not.

## Other known limits
- Tie-breaks by order are a measured choice, not proof; the ledger lists those steps.
- mypy (strict flags) covers `audit/`, `main.py`, `evaluation/` and `tools/` (52 files); the five unittest files are checked only
  where annotated. A pass shows consistent signatures, not correctness.
- The Gemini key was pasted in chat and must be rotated.
