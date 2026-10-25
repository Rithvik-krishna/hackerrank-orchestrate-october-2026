# Ghost Data Still Exists - deletion-audit agent

For every request in `dataset/deletion_requests.csv` the program decides whether deletion was authorised, rebuilds
what was true at `audit_at` from the supplied evidence, applies `deletion_rules.csv`, and writes
`COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE` plus the ordered findings, remediation and verification steps.

## Setup and run

Python 3.12, standard library only (the model client uses `urllib`; there is nothing to `pip install`).

```bash
python code/main.py                      # all requests -> ./output.csv (deterministic, no API key, validated before it exits)
python code/main.py --samples            # the 32 samples -> code/evaluation/sample_predictions.csv
python code/main.py --explain case_11    # decision trail for one case
python code/main.py --parse-report       # parse every evidence file and report counts
python code/evaluation/evaluate.py --diff   # score the samples (status, row, sequence, step, field, evidence)
python code/evaluation/test_llm_layer.py    # 52 offline tests of the model layer (local fake API, no key)
python code/evaluation/test_verifier.py     # 22 tests: the independent verifier accepts gold, rejects broken answers
python code/evaluation/test_provenance.py   # 9 tests: every step traces back to its request
python code/evaluation/test_regression.py   # 19 tests: exactly-right samples stay right; run_all.py, the package audit and the
                                            # trace stager fail closed; the TypedDict shapes match the data
python code/evaluation/test_parsers.py      # 39 tests: damaged log/SQL/JSON items are skipped, reported and flagged; what a log line
                                            # says about a record's state (no model)
python code/tools/gap_probes.py sensitivity # reproducible probes behind rubric_audit.md (pdfs, conventions, lag, shape, sensitivity)
python code/evaluation/main.py              # (same as run_all.py) everything in one command: lint, mypy if installed, 5 suites,
                                            # full run, schema, baseline, samples + regression, custom suite, mutation check,
                                            # error analysis, reports; exit 1 if a required check fails; --skip-mutation;
                                            # --update-baseline is the only way the trusted baseline is ever replaced
python code/evaluation/error_analysis.py    # row-level categories of the remaining sample differences
python code/evaluation/lint_check.py        # unused-import gate and function-size report
python -m mypy --config-file code/mypy.ini  # static types of audit/, main.py, evaluation/, tools/ (dev tool: pip install mypy; the pipeline needs only the stdlib)
python code/main.py --graph case_001        # provenance graph of one case (--explain CASE for a text trace)
# main.py also runs the verifier on every run (code/evaluation/verification.csv) and fails on any error;
# see code/evaluation/rubric_audit.md for the honest gap report
python code/evaluation/make_report.py       # regenerate evaluation/evaluation_report.md
python code/evaluation/make_confidence_report.py  # regenerate evaluation/confidence_report.md (uncertainty ledger)
python code/evaluation/make_usage_report.py # regenerate evaluation/usage_report.md from the usage ledger
```

Every run writes `code/evaluation/confidence.csv` next to `output.csv`: for each case, how firmly the answer rests on
evidence (see "Uncertainty ledger" below).

### With the model components

Put the key in the environment (or an uncommitted `.env` at the repository root); it is never read from code.

```bash
export ANTHROPIC_API_KEY=...            # PowerShell: $env:ANTHROPIC_API_KEY="..."
python code/main.py --extract-images [--force]     # read the 11 screenshots with the vision model
python code/main.py --fresh-ledger --llm --review  # full run with intent arbitration, link adjudication, second opinions
python code/main.py --samples --llm                # measure it on the labelled samples
```

`--llm` without a key prints a notice and runs deterministically. Providers: Anthropic (`ANTHROPIC_API_KEY`;
`claude-haiku-5-5` for intent, `claude-sonnet-5-5` for the rest) or Gemini (`GEMINI_API_KEY`; `gemini-3.1-flash-lite`),
chosen by whichever key is set or by `AUDIT_PROVIDER`; override models with `AUDIT_FAST_MODEL` / `AUDIT_MODEL`. The
review trail is written to `code/cache/model_review.json`; usage to `code/cache/usage_ledger.jsonl`.

Add `--agent` to send ambiguous links to the tool-calling investigator instead of the single-shot adjudicator. The model
is slow (seconds per turn); answers are cached by the content of each group, so a repeated run is fast.

Custom cases: `python code/evaluation/custom_cases/build_suite.py`, then
`python code/main.py --dataset code/evaluation/custom_cases/suite --output code/evaluation/custom_cases/suite/predictions.csv`.

## How it works

The core is a deterministic pipeline; the model components are optional and sit behind deterministic guardrails.

1. `audit/intent.py` - rules in EN/ES/PT/FR/JA/KO with negation cues decide whether the text authorises deletion.
   `audit/intent_llm.py` - for requests the rules call UNSURE, a forced tool call (two-value enum, confidence floor).
2. `audit/identity.py` - current and historical emails, phones and hashes, valid as of a cutoff; exact matches only.
3. `audit/evidence/` - one parser per format (SQL snapshots executed in SQLite, JSON bundles/exports, seven log
   dialects, handover text, PDF text, screenshot readings) producing normalised observations.
   `audit/vision.py` - screenshot extraction with validation and a cross-check against the data catalog.
4. `audit/linker.py` - which records belong to the subject: identifier match, dated catalog links, chains, and, for
   records with no identifier, alignment with the deletion worker's activity. Where several cases fit equally,
   `audit/adjudicator.py` asks the model, and accepts an answer only if it passes the guardrails.
5. `audit/state.py` - the latest state at `audit_at`; a record counts by its own time, a citation must come from a
   file captured by `audit_at`; replica drift and absence-by-omission are handled here.
6. `audit/policy.py` - the required treatment per category (delete, redact, anonymise, retain, disable flow, block restore).
7. `audit/steps.py`, `audit/validate.py` - canonical ordering and output-contract checks.
8. `audit/llm.py` - the one place that talks to the API: forced tool calls, schema validation with repair, retries with
   back-off, disk cache, usage ledger, and `AgentSession` for multi-turn tool calling (both providers).
9. `audit/investigator.py` - the investigating agent: it chooses among tools (`list_candidates`, `get_case`,
   `get_evidence`, `get_activity`, `get_state`), has a hard turn cap with a forced final submission, and its proposal is
   accepted only if a deterministic verifier agrees: every id in its basis was returned by one of its own tool calls, the
   chosen record was inspected, and the usual guardrails hold. Groups with nothing to investigate cost no call.
10. `audit/ledger.py` - the uncertainty ledger: each link is labelled (evidence, catalog, chain, alignment: unique /
    relaxed / order / model), each state as observed or inferred, and each case gets high / medium / low confidence plus a
    list of records it may be missing. `evaluation/confidence_report.md` shows how much of the answer rests on tie-breaks
    and checks the labels against the labelled samples.

The model can never remove a violation, override a clear rule decision, cite evidence, or decide a record's state:
states come from evidence, and its answers only add a link or settle an unclear request.

`AUDIT_ZIP_MODE` (`order`|`unique`|`llm`) and `AUDIT_OWNERSHIP` (`cutoff`|`any`) select the measured tie-break choices
described in `evaluation/evaluation_report.md`; the defaults are `order` and `cutoff` (`--llm` switches to `llm`).

## Damaged evidence

A malformed line, statement or record is skipped and reported (code/evaluation/parse_diagnostics.csv, with file, parser,
position and category); the valid items around it are kept. A file whose record boundaries cannot be trusted (invalid or
truncated JSON, a dump where no statement runs) fails as a whole and the run exits non-zero. A partly readable file can
not prove a record absent, and the cases that rely on it are capped at medium confidence (degraded_evidence in
confidence.csv, degraded_by edges in --graph).

## Screenshots

The committed cache `code/cache/vision/*.json` was produced by reading the screenshots with the coding assistant used
for development (no key was available then); `tools/transcribe_screenshots.py` is its source. `--extract-images --force`
replaces it with the API reading, backs the old files up to `code/cache/vision_transcribed/` and prints where the two
readings disagree.

## Evaluation

`evaluation/evaluation_report.md` has the sample metrics, 23 custom cases, a 15-rule mutation check, the failures found, the
model components and the judgement calls. `evaluation/usage_report.md` is generated from the usage ledger.

## Packaging and what was (not) verified live

```bash
python code/tools/make_zip.py --dry-run          # audit + file list, writes nothing
python code/tools/verify_package.py              # rehearsal: the would-be package in a clean folder, no key, fresh interpreter
python code/tools/make_zip.py                    # writes code.zip (the audit refuses secrets, machine paths, dataset files)
python code/tools/verify_package.py --zip code.zip   # the real archive, extracted and re-run
python code/tools/make_trace.py --dry-run [--export-dir DIR]   # agent-trace.zip: what would go in, secrets redacted in the copies
python code/tools/make_trace.py [--export-dir DIR]             # write ./agent-trace.zip (quit the app first; save /export transcripts to DIR)
```

The archive holds `code/` only. `code/cache/vision` (11 files) is intentional: the screenshots were transcribed by the
development session (Claude Code, no API call; each file says so in `extracted_by`) and the pipeline reads them like any
other evidence. The cache of model answers (`code/cache/llm`) is left out: it is never read by the deterministic run and
was built from prompts made out of the dataset. Live model validation is limited to the link adjudicator and the investigator,
run against a free-tier Gemini key (see `usage_report.md`); intent arbitration and API vision extraction were only
exercised against a local fake of the API (`test_llm_layer.py`) and then, on 26 Oct 2026, against the same free-tier model
(`code/tools/live_check.py`, `code/evaluation/live_validation.md`, `code/evaluation/live_validation_notes.md`): the classification
contract and guardrails held on a small sample, and the API screenshot route reads 9 of 11 screenshots and leaves the 32 labelled
samples exactly as they were (one of the 518 answers would differ). That is a sanity check of one small model, not an accuracy
measurement. None of the model components changes `output.csv`.

## Limits

- Five of the 32 sample cases still differ from the labels (right status, one verification step missing or wrong).
- Identifier-less records are attributed by timing and ordering regularities in the worker activity; where several
  cases fit equally the result is a tie-break, not proof.
- The model components have been tested offline against a fake API; their effect with a real model has not been measured
  unless `evaluation_report.md` says so.
- True accuracy on the hidden answers is unknown.
