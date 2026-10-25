# Implementation Plan — Ghost Data Still Exists

Python 3.12, standard library only for the core. A model is used only where text parsing cannot work: the 11 PNG screenshots and unclear request intent.

> This file is a chronological record of the build, written section by section as the work went on. Where an early section
> disagrees with a later one, the later one and `README.md` are current (for example the model layer, the verifier, the
> parser salvage, the typing and the package audit all arrived after the first sections were written).

## 1. Design principles

1. **A deterministic core.** Parsing, linking, state reconstruction, policy and step ordering are all rule code. The same input always gives the same `output.csv`.
2. **Evidence decides state; activity rows only point.** `reported_status` and `reported_result` are never proof. Activity is used to find candidate resources and to read the explicit check events (`CHECK_DATA_FLOW`, `CHECK_RESTORE_ACCESS`).
3. **Every record's own timestamp applies, inclusive.** Observations are filtered by `observed_at <= audit_at` using the per-record time (`captured_at`, `recorded_at`, `observed_at`, log-line time, or `snapshot_scope.captured_at`), never the manifest time or the bundle's `exported_at`.
4. **No case IDs or labels anywhere in the code.** Sample answers are used only by the evaluator.
5. **Model calls are cached and accounted for.** Every call goes through one client that writes a usage ledger; `usage_report.md` is generated from that ledger.

## 2. Repository layout

```text
code/
├── main.py                  # CLI entry point
├── audit/
│   ├── paths.py             # repo-relative paths (works on any OS)
│   ├── loaders.py           # CSV loaders and time parsing
│   ├── identity.py          # identifier index valid as of a given time
│   ├── intent.py            # deletion vs non-deletion (rules, then optional model)
│   ├── model.py             # Observation, Step dataclasses and enums
│   ├── evidence/
│   │   ├── __init__.py      # parser registry: file -> list[Observation]
│   │   ├── sql_snapshots.py # in-memory sqlite3, primary/replica tables
│   │   ├── json_records.py  # bundles, service exports, flat record lists
│   │   ├── logs.py          # one normaliser for all 7 log dialects
│   │   ├── text_records.py  # pipe-delimited handover .txt
│   │   ├── pdf_text.py      # ReportLab ASCII85+Flate text extraction
│   │   └── images.py        # vision model -> cached JSON -> Observations
│   ├── activity.py          # signals from system_activity.csv
│   ├── linker.py            # which observations belong to the subject
│   ├── state.py             # latest state per resource at audit_at
│   ├── policy.py            # deletion_rules.csv -> required end state
│   ├── steps.py             # step builder and canonical sorter
│   ├── validate.py          # output contract checks
│   └── llm.py               # model client, disk cache, usage ledger
├── cache/vision/            # one JSON per PNG (committed so runs are reproducible)
├── evaluation/
│   ├── evaluate.py          # scores predictions against sample_output.csv
│   ├── custom_cases/        # hand-built fixtures with expected outputs
│   ├── evaluation_report.md
│   └── usage_report.md
├── requirements.txt         # empty for the core; `anthropic` optional
└── README.md
```

CLI:

```text
python code/main.py                 # all 518 cases -> ./output.csv
python code/main.py --samples       # 32 samples -> code/evaluation/sample_predictions.csv
python code/main.py --explain case_x  # print linked observations and the decision trail
python code/evaluation/evaluate.py  # metrics + custom cases -> evaluation_report.md
```

## 3. Data model

```python
Observation(
    evidence_id: str,          # file_id, or activity_id for check events
    observed_at: datetime,     # per-record time
    system_id: str,            # account_service, search_service, ...
    resource_id: str,
    category: str | None,      # USER_PROFILE, SUPPORT_MESSAGE, ... (rules vocabulary)
    status: Status,            # PRESENT | ABSENT | ACTIVE | DISABLED | RESTORABLE | BLOCKED
    identifiers: set[str],     # normalised emails, phones, hashes, user_ids found in the payload
    link_keys: set[str],       # lookup_key / subject_key / match_key / correlation_key / subject_reference
    attrs: dict,               # enabled, restore_allowed, retention_until, source_resource_id, replica_role, ...
)
```

Normalisation, applied everywhere: lowercase emails; strip spaces from phones; `user_007` and `user_7` become the same key; drop null-like values (`null`, `None`, empty).

## 4. Module specifications

### 4.1 Identity (`identity.py`)

- Start with `user_identities.csv`, then add every `old_value` and `new_value` from `user_audit_log.csv` for `email`, `phone` and `email_hash`. Each value keeps its event time.
- `identifiers_for(user_id, as_of)` returns the current value plus any historical value whose change happened at or before `as_of`.
- `resolve(email, as_of)` uses exact lowercase equality across current and historical emails. If the email maps to more than one user, flag it as ambiguous and pick none.
- **Why this matters:** case_12 is linked only through the user's *old email hash* (`audit_283`); evidence_01's `analytics_9914` carries that hash as `correlation_key`.
- Never match on names or prefixes. Lookalike decoys exist, such as `smartinez6461.jr@` vs `smartinez6461@` and `g.silva76.jr@`.

### 4.2 Intent (`intent.py`)

1. **Rules.**
   - Deletion verbs in EN, ES, PT, FR, JA, KO (delete, erase, remove my account, close permanently, eliminación, exclusão, suppression, 削除, 삭제, ...).
   - Explicit negations ("not asking for deletion", "don't delete", "keep the account", "leave my profile").
   - Non-deletion intents (export, copy, correct, change email/phone, marketing only, reset login).
2. Output `DELETE`, `NOT_DELETE` or `UNSURE`. Negation and non-deletion cues override a bare "remove" (case_538: "remove me from marketing lists only").
3. Only `UNSURE` goes to the model (small JSON-only prompt). Disagreements are logged.
4. A `CLASSIFY_REQUEST ... non-deletion` row for the subject's account is checked afterwards as corroboration. A conflict is written to the explain trail; it never overrides the text.

### 4.3 Evidence parsers (`evidence/`)

Each parser returns `list[Observation]`. Every format below has been inspected in the dataset.

| Parser | Files | Key rules |
|---|---|---|
| `sql_snapshots` | 25 `.sql` | Execute the file in `sqlite3.connect(":memory:")` and read every table. Time comes from the `-- Captured at:` header or from the `snapshot_scope(origin_system, captured_at)` table, which gives **each system its own time**. There are three shapes: profile columns, `(resource_id, system, collection, JSON payload, captured)`, and the inventory tables (`customer_profiles`, `customer_search_documents`, `support_records`, `connector_subscriptions`, ...). Table name suffixes (`_primary`, `_read_replica`, `_failover_replica`, regional names) go into `attrs.replica_role`. |
| `json_records` | bundles, exports, flat lists | **Bundles:** `snapshots[].captured_at`, `data_store`, `collection`, `records`. **Service exports:** `service_exports.{system}.{collection}[]` with `observed_at` and `status_code` (404 means ABSENT). **Flat lists** (connector configs, export manifest, security ledger): `recorded_at, system, resource_id, record_type, result, subject_reference`. **Single collection** (analytics report, export jobs): a top-level `captured_at`. |
| `logs` | 22 `.log` | **One normaliser for 7 dialects:** `key=value`, JSON after a timestamp, RFC5424 syslog, pipe-separated, `[LEVEL] [service] [trace]`, cloud-runtime `message="..."` (decode the escaped inner pairs), and flat `record_lookup` lines. Each line becomes `(ts, service, trace_id, event, kv)`. Lines are grouped by `trace_id`: the *started* line supplies the link key (`lookup_key`, `subject_key`, `match_key`, `scan_key`), and the later lines supply `resource_id` and the result. |
| `text_records` | handover `.txt` | `ts \| system \| resource \| CATEGORY \| STATE \| identifier` gives the category and state directly. |
| `pdf_text` | 4 PDFs | Inflate the ASCII85+Flate streams, collect `(...) Tj` strings, then extract `user_\d+`, emails, phones, resource IDs and statement/ticket references (`NST-2026-0718-1842`, `support_001`). Already prototyped and working. |
| `images` | 11 PNGs | See §6. |

**Mapping log events to state** (one table in `logs.py`, extended as new events appear):

| Event or field | Status |
|---|---|
| `http_status=404`, `rows=0`, `result=NOT_FOUND`, `result=miss` | ABSENT |
| `rows>=1`, `result=hit/FOUND`, `marker_found=false`, `*_pii_scan` | PRESENT |
| `enabled=true`, `scheduler_registered=true` | ACTIVE (flow) |
| `enabled=false` | DISABLED |
| `restore_allowed=true`, `eligible=true deletion_block=false` | RESTORABLE |
| `restore_allowed=false` | BLOCKED |

**Inferring category, in order:**

1. An explicit `record_type` or `CATEGORY` field.
2. The event name: `message_pii_scan` → SUPPORT_MESSAGE, `attachment_pii_scan` → SUPPORT_ATTACHMENT, `connector_*` → DATA_FLOW, `restore_*` → BACKUP_ARCHIVE, `contact_lookup ... email_opt_in` → to be confirmed against case_12.
3. The table or collection name.
4. The resource-ID prefix as a last resort.

This ordering is what explains case_12: `analytics_9914` is *deleted*, which means its category does not come from the `analytics_` prefix.

### 4.4 Activity signals (`activity.py`)

- `CHECK_DATA_FLOW "remains enabled"` gives ACTIVE evidence for a flow; `CHECK_RESTORE_ACCESS "can still be restored"` gives RESTORABLE. The activity_id is citable.
- `SYNC_RECORD` from `integration_service`, or `RESTORE_BACKUP "repopulated downstream resource X"`, is a recreation path for X.
- `LINK_RESOURCE_IDENTITY` maps resource → (email, user_id) at `event_time`. It is honoured only if `event_time <= audit_at`. activity_4000 is dated after several cutoffs.
- **Discovering the account.** A deletion-worker `DELETE_RECORD ... primary profile account_N` with `requested_at <= event_time <= reported_at` makes `account_N` a candidate. 13 of the 111 resources in the sample answers (all `account_*`) appear *only* here.

### 4.5 Linker (`linker.py`)

An observation belongs to the subject if any of these hold:

1. `identifiers ∩ subject_ids` is non-empty (current or historical, as of `audit_at`).
2. `link_keys ∩ subject_ids` is non-empty. This is how a 404 lookup with an empty payload is still linked (`account_7566` via `lookup_key=l.fontaine46@...`).
3. Its `resource_id` is linked by a dated `LINK_RESOURCE_IDENTITY` row, or by a PDF reference that such a row resolves (`NST-...-1842` → `export_1842`).
4. `attrs.source_resource_id` points at an already-linked resource (`search_001.source_resource_id = account_006`).
5. A resource with no identifiers at all can only be linked through deletion-worker co-batching. This is the weakest link: it is used only when nothing contradicts it, and it is reported separately in the evaluation.

**Exclusion:** an observation is dropped if its payload names a *different* known user (support_1066 → `user_42`).

### 4.6 State at `audit_at` (`state.py`)

- Group the linked observations by `(system_id, resource_id)` and keep those with `observed_at <= audit_at`. The latest one wins.
- **Replica drift:** if the primary is ABSENT but any replica at the same snapshot is PRESENT, the resource is PRESENT and the replica's file is cited (case_05 / evidence_54).
- **Absence by omission:** a known resource missing from a snapshot that covers its table counts as ABSENT. The citation is the latest covering file at or before the cutoff (case_01 / evidence_54). The covering-file rule is tuned against the samples.
- If two files qualify at the same time, the one that observed the resource directly wins over one that implies it by omission. Remaining ties go to the smaller `file_id`, decided by measurement and documented.

### 4.7 Policy (`policy.py`)

`deletion_rules.csv` is loaded as data, not hardcoded.

| Rule action | Compliant when | Violation produces |
|---|---|---|
| DELETE | ABSENT | REMAINING_RECORD → DELETE_RECORD |
| REDACT | PRESENT with no name, email, phone or hash (a tombstone keeps `subject_reference`) | EXPOSED_PII → REDACT_RECORD |
| ANONYMIZE | PRESENT with no user_id, email, phone or hash | EXPOSED_PII → ANONYMIZE_RECORD |
| RETAIN (audit, legal hold) | PRESENT and `retention_until > audit_at` | never remediated; verified PRESENT |
| RETAIN (backup) | `restore_allowed=false` | RESTORE_RISK → BLOCK_RESTORE |
| data flow | DISABLED | ACTIVE_DATA_FLOW → DISABLE_DATA_FLOW |

Conditional rules:

- **rule_06:** delete a support attachment only if it belongs solely to the subject. If it is shared, treat it as REDACT.
- **rule_05:** a support message is REDACT.
- **rule_15:** a tombstone keeps its user_id.
- **Expired retention:** an audit record past `retention_until` falls back to DELETE.

The status is `COMPLETE` if no violations remain, otherwise `INCOMPLETE`.

### 4.8 Step builder (`steps.py`)

- **COMPLETE:** verification steps only. Each one cites the file that proves the compliant state.
- **INCOMPLETE:**
  - Findings cite their evidence.
  - Remediation covers every violation.
  - Verification covers the primary account (ABSENT), every retained record (PRESENT) and every remediated item. Remediation and verification evidence is `""`.
- **Sort key:** `(phase, operation order from the spec table, system_id, resource_id)`, then renumber from 1. This was checked by hand against case_11, case_18 and case_29.
- **Remediation safety (AGENTS.md §6.5):** flows are disabled and restores blocked before records change. This holds automatically because `DISABLE_DATA_FLOW` and `BLOCK_RESTORE` come first in the operation order. The "downstream copies before source" rule is checked as an assertion. The spec's ordering is still authoritative, since the samples (case_16: account before search) follow it.
- **Valid citations:** a `file_id`, an `activity_id` (for example a `CHECK_DATA_FLOW` row) or an `audit_id` from `user_audit_log.csv` (for example the change that proves a historical identifier).

### 4.9 Validator (`validate.py`)

Runs on every output, and on any failure the program exits with a non-zero code. It checks:

- exact header `case_id,deletion_status,steps_json`;
- exactly 518 rows, one per input `case_id`, sorted by the numeric part of `case_id` (`case_33` comes before `case_100`; plain string sorting would put it after);
- each step has exactly the seven contract fields, and `steps_json` is serialised deterministically (`sort_keys`, compact separators);
- only allowed phase/operation/state combinations;
- contiguous `step_no`;
- the `""` evidence rule;
- every cited ID exists (file, activity or audit) and is dated at or before that case's `audit_at`;
- `NOT_APPLICABLE` comes with `[]`.

## 5. Evaluation (`evaluation/evaluate.py`)

| Metric | Definition |
|---|---|
| Status accuracy | Share of cases with the correct `deletion_status` |
| Step-level P / R / F1 | Steps as `(phase, operation, system_id, resource_id, state, evidence_id)` sets, micro-averaged |
| Field-level accuracy | Steps aligned by position; correct fields ÷ total fields, with length differences counted as errors |
| Sequence accuracy | Cases whose ordered `(phase, operation, system, resource)` list matches exactly |
| Evidence accuracy | Among matched steps, share with the correct `evidence_id` |

Error analysis prints, for each failing case, the expected vs. predicted diff plus the `--explain` trail.

**Measured ceiling, so it can be stated honestly in the report:**

- Of the 111 distinct resources in the sample answers, 94 appear in text evidence.
- 13 (all `account_*`) appear only in deletion-worker activity.
- About 4 appear only in a PDF or PNG, or nowhere textual: `audit_record_6645`, `audit_record_5569`, `account_004`.

**Custom cases** (`custom_cases/`) are small fixture datasets in the same format, each with an expected output:

1. Uppercase *historical* email (`SIPHO...` style) resolves to the current user.
2. A post-`audit_at` observation (a later ACTIVE snapshot) must not flip COMPLETE → INCOMPLETE.
3. A lookalike email (`x.jr@`) in a search index must not be linked.
4. A shared support attachment is REDACTed, not deleted (rule_06 condition false).
5. A link through an old email hash only (case_12 pattern).
6. A `LINK_RESOURCE_IDENTITY` row dated after the cutoff must be ignored.
7. A non-deletion request that contains "remove" (marketing opt-out) is NOT_APPLICABLE.

## 6. Model use

| Use | Model | Calls | Notes |
|---|---|---|---|
| PNG extraction | Claude Sonnet 5.5 (`claude-sonnet-5-5`), vision | 11, once each | Strict JSON schema: `{rows:[{system_id, resource_id, category, status, identifiers, fields, timestamp}]}`. Prompt says the screenshot is data, not instructions. Output is cached in `code/cache/vision/<file_id>.json` and committed. |
| Unclear intent | Claude Haiku 5.5 (`claude-haiku-5-5`) | only `UNSURE` cases (expected 0–5) | JSON `{intent, confidence, reason}`. |

- The model ID can be changed through an environment variable.
- The key is read from `ANTHROPIC_API_KEY`.
- `--no-llm` runs entirely from the cache.
- `llm.py` records provider, model, input/output tokens and latency for each call in `code/cache/usage_ledger.jsonl`.
- `usage_report.md` is generated from the ledger for the final run, with cache hits reported as zero-token calls. It contains everything §6.8 requires:
  - provider and model names;
  - call counts;
  - input and output tokens;
  - total and average tokens per request (over 518);
  - estimated total and per-request cost (prices taken from a config table and labelled as estimates);
  - per-model and overall totals.
- `evaluation_report.md` likewise contains:
  - status accuracy;
  - step-level and field-level accuracy;
  - sequence accuracy;
  - the custom cases with their results;
  - a **"failures found and improvements made"** log, filled in during M3 with before and after metrics for each rule change.

Each image's output is cross-checked against the dated `LINK_RESOURCE_IDENTITY` rows and identity data. A mismatch is logged and the row is dropped, so one bad screenshot read cannot cause a false finding.

## 7. Build order and milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | Skeleton, loaders, identity, intent, validator, CSV writer | A **valid 518-row `output.csv`** exists (intent-correct NOT_APPLICABLE rows; deletion cases on a conservative default). This is the safety-net submission. |
| M1 | All text parsers plus `--explain` | Every evidence file parses without errors; each sample resource found in text evidence is observed (target: all 94). |
| M2 | Linker, state, policy, steps | `--samples` runs end to end; the evaluator prints all metrics. |
| M3 | Error-analysis loop on the 32 samples | Status accuracy ≥ 30/32; sequence accuracy as high as the measured ceiling allows. Each fix is a general rule, never a case ID. |
| M4 | PDF links, image extraction (cached), activity account discovery | The remaining reachable sample resources are covered. |
| M5 | Full run, custom cases, reports, README, `code.zip` script | Validator passes; both reports are generated from real numbers; the zip excludes `dataset/`, caches of secrets and virtual environments. |
| M6 | Interview prep | A one-page architecture summary, three decisions with trade-offs, and the known limits. |

Order of risk: M0 first so a submission always exists, and M3 gets the most time because that is where the score comes from.

## 8. Decisions to measure, not assume

1. Which file to cite when several qualify: the latest at or before the cutoff, or a direct observation over absence by omission.
2. The rule for absence by omission: which snapshot "covers" a resource.
3. Category for `contact_lookup / email_opt_in` records (case_12).
4. Co-batch linking for records with no identifiers: on, or off with the recall loss accepted.
5. The default for a deletion case with no linked evidence at all.

Each choice is recorded in `evaluation_report.md` with its effect on the sample metrics.

## 9. Compliance checklist (AGENTS.md / problem_statement.md)

| Requirement | Where it is handled |
|---|---|
| Use only participant data under `dataset/`; no hardcoded labels or case-family shortcuts | Sample answers are read only by `evaluate.py`; no case IDs in `audit/` |
| Use only information at or before `audit_at`; never cite later evidence | §4.6 filtering plus the §4.9 citation-date check |
| Every relevant record treated per `deletion_rules.csv`, including tombstones, flows and restores | §4.7 |
| COMPLETE = verification only, with evidence; INCOMPLETE = findings + remediation + verification, with `""` for future work; NOT_APPLICABLE = `[]` | §4.8 |
| Only allowed phases, operations and states; no invented ones | Enums in `model.py`, checked in §4.9 |
| `output.csv` at the repo root, runnable from the terminal, deterministic | `main.py`, §4.9 |
| Secrets only from environment variables, never logged or zipped | `llm.py`; `.env` git-ignored |
| `README.md` with setup and run steps; `code/evaluation/` reports | M5 |
| Zip `code/` only, without `dataset/`, virtual environments, caches containing secrets or nested archives | M5 zip script |
| A `log.txt` entry for every turn with the exact `tool=` and `model=` | Done every turn |

## 10. Known limits (stated in the report and the interview)

- Some sample answers reference resources that are not in text evidence. Those depend on the image extraction or cannot be recovered.
- Records with no identifiers that are processed in overlapping deletion-worker batches (analytics_9406 vs analytics_5589 on 2026-09-15) cannot be attributed with certainty. Line adjacency in the trace logs is a decoy and is not used.
- The 32 samples are a small labelled set. Rules are kept general so they carry over to the 518 hidden answers.

## 11. M2 outcomes (measured on the 32 labelled samples)

Decisions that changed from the plan above, each driven by a sample failure:

| Decision | Result |
|---|---|
| Knowledge uses the record's own time; a *citation* must come from a file captured at or before `audit_at`, chosen separately from the decision | Fixes cases where a later-exported bundle or log holds an earlier fact |
| Evidence older than `requested_at` cannot link a resource | Stops an earlier request by the same person (other account) leaking in (case_17) |
| Absence by omission only for DELETE-category records; a table dump must be non-empty, a bundle snapshot counts only if its file also documents the subject | Fixes case_05 (empty inventory) and case_19 (partial bundle) while keeping case_01/02/31 |
| The worker's account result mirrors the reported status (SUCCESS/PARTIAL/FAILED = COMPLETE/PARTIALLY_COMPLETE/NOT_STARTED) and splits a batch into classes | Resolves most identifier-less accounts |
| Identifier-less worker rows are assigned by alignment inside a reported_at group (`AUDIT_ZIP_MODE=order`: equal-sized groups zipped by case order against time order; `unique`: only 1:1) | order: row-exact 78.1%, precision 0.995; unique: 68.8%, precision 1.000 |
| A deletion case with no evidence-backed state is INCOMPLETE with no steps, never COMPLETE | Waits for image extraction (M4) |

Known remaining gaps: account_004 (no trace anywhere), audit_record_6645 / audit_record_5569 (not in any text evidence; likely in a PNG), account_410 vs account_7015 (a 3-way FAILED class that case order resolves wrongly).

## 12. M4 outcomes (screenshots)

- All 11 PNGs were transcribed into `code/cache/vision/<file_id>.json`, one row per resource the page is about. The session had no API key, so the transcription was made by reading the screenshots with the Claude Code session model (claude-sonnet-5-5), not by an API call; `code/tools/transcribe_screenshots.py` is the source of the committed cache and `audit/vision.py` is the API route (never validated live: see `evaluation/live_validation.md`). `usage_report.md` must say 0 vision API calls for this reason.
- Wired in: the image parser, a catalog-link pattern for "belongs only to" rows, user-id normalisation for links (`user_07` is `user_7`), and a rule that a catalog link dated before the request cannot belong to it (it stopped `object_19` leaking into sample case_07).
- Effect on the full run: deletion cases with no steps went from 7 to 0; sample metrics unchanged (status 1.000, row-exact 0.781).
- Judgement calls to disclose: `backup_9287` is shown with no restore block, so it is treated as restorable (inferred, not displayed); `marketing_001` shows Soo-jin Park while the catalog links it to kwame-mensah, so the catalog link is used and the mismatch is recorded in the cache.

## 13. M3 outcomes

- Error analysis on the samples is recorded in `code/evaluation/evaluation_report.md` (generated by `code/evaluation/make_report.py`): 5 of 32 sample cases still differ from the labels, all with the right status, each missing one verification step or holding one wrong account row.
- Strict by default: ownership learned after a case's audit_at is not used (`AUDIT_OWNERSHIP=cutoff`); the lenient `any` variant scores one sample case higher and stays available.
- Alignment of identifier-less records changed: accounts are matched across all cases at once; policy rows use only cases with no identifier-linked violation that are documented by the same file; the claimed status no longer splits policy rows.
- Custom suite: 13 hand-written cases in the real formats (`code/evaluation/custom_cases/`, run with `main.py --dataset`), all passing; `mutation_check.py` breaks seven rules one at a time and the suite catches every one.
- `usage_report.md` is still to do (M5), as are the README and the zip script.

## 14. Model-driven layer (added after M5)

- `audit/llm.py` (urllib client, forced tool calls, schema validation with repair, back-off retries, disk cache, usage ledger), `intent_llm.py`, `adjudicator.py`, `vision.py`; flags `--llm`, `--review`, `--extract-images`, `--force`, `--fresh-ledger`.
- Deterministic guardrails live in code, not prompts: clear rules are never overridden, low confidence keeps the safe default, adjudicated links need high confidence plus a substantive basis made of ids that were provided, extraction rows must quote their own id, the model can only add a link.
- Offline: `code/evaluation/test_llm_layer.py` (52 tests by now, local fake API) passes. The adjudicator and the investigator were later run against a real model (no accuracy effect, see section 7 of `evaluation/evaluation_report.md`); intent arbitration and API vision were run live later (`evaluation/live_validation_notes.md`). The run book is in `code/README.md`.

## 15. Verification, provenance and audit (added after the model layer)

- Uncertainty ledger (`audit/ledger.py`): every answer carries a confidence label and its tie-breaks (`confidence_report.md`).
- Independent verifier (`audit/verifier.py`): re-derives checks from raw evidence; gates `main.py`; 0 errors on gold, samples
  and all 518 answers; found the attachment-as-message bug in the engine.
- Provenance graph (`--graph`), regression baseline, custom cases (16 then; 22 now), mutation checks (10 then; 14 now), lint gate.
- Tool-calling investigator (`audit/investigator.py`): measured with a real model, no accuracy effect; output stays deterministic.
- `code/evaluation/rubric_audit.md` records the gaps honestly. `code.zip` is built once, last.

## 16. Gap review, function size, annotations, more adversarial cases

- The five remaining sample gaps were re-investigated against all of `dataset/`; none has a general rule (see
  `code/evaluation/rubric_audit.md`), so `output.csv` is unchanged.
- No function is longer than 60 lines (`lint_check.py`), `audit/` is fully annotated (155/155), and `output.csv` is
  byte-identical before and after the refactors.
- Custom suite grown from 16 to 20 cases (negation, instruction injected in the request text, replication drift,
  Japanese request) and the mutation check from 10 to 12 rules; all caught.

## 17. Parsers salvage damaged items and flag the degradation

- `audit/diagnostics.py`: a `Diagnostic` (evidence id, source file, parser, level, location, category, message). Item-level
  damage (a log line, an SQL statement, a JSON record, a handover row) is skipped and reported with its position; the
  valid items around it are kept. Damage that makes record boundaries untrustworthy (invalid or truncated JSON, a dump in
  which no statement runs, an unrecognised layout) fails the whole file (`UnparseableFile`), as before, and the run
  exits non-zero.
- A partly readable ("degraded") file cannot prove absence: its table coverage is dropped and an explicit ABSENT it holds is
  demoted to UNKNOWN (`attrs["demoted_from"]`). Nothing is filled in for a skipped item.
- Cases that rely on a degraded file (it documents the case or holds a record linked to it) are capped at medium confidence
  in the ledger (`degraded_evidence` column) and show `degraded_by` edges in `--graph`. `parse_diagnostics.csv` is written
  with every run (header only when nothing was skipped).
- `test_parsers.py` (27 tests, no model): damaged log JSON, malformed/unterminated/truncated SQL, truncated JSON, records
  without resource_id, surrounding records preserved, the end-to-end effect on a copy of the custom suite.
- The real dataset parses cleanly, so `output.csv` is byte-identical before and after (checked with `fc /b`).

## 18. One evaluation entry point, error analysis, degradation scenarios

- run_all.py runs lint, the five suites, the full run (to a scratch file), the schema check, the comparison with the trusted
  baseline (code/evaluation/baseline/output.json, sha256 and status counts), the samples with the regression check, the custom
  suite, the mutation check, the error analysis and the reports, records each command's real exit code and duration in
  run_all_report.json/.md, and exits 1 if a required check fails. The baseline is replaced only by --update-baseline, which
  refuses to run while another check fails (test_regression.py::RunAllGuards).
- error_analysis.py: of 32 labelled rows, 27 match as data and 5 differ (row exact 0.844). All 5 differences are in the
  VERIFICATION phase: 3 missing steps whose record id appears nowhere in dataset/, 2 substitutions of one account for another
  (a tie and a post-cutoff ownership case). Step numbers shift after a missing step (renumbering cascade, a consequence).
  There is no ordering, phase, evidence, system or status error. Step F1 is 0.982 because 5 of 193 gold steps are wrong;
  exact row agreement is a per-case measure, so each wrong step costs a whole row. 7 more rows differ from the gold only in JSON key
  order (the gold uses alphabetical keys, we use the specification order); the grader ignores key order, so nothing was changed.
- test_parsers.py gained DegradationScenarios: a clean file proves absence; a damaged log leaves the independent clean table
  dump able to prove it (cited, case capped at medium); a damaged table dump cannot; with both damaged nothing proves it.

## 19. Packaging audit (no archive built)

- make_zip.py --dry-run audits the file list: required files present, no dataset file, no nested archive, no secret, no
  machine-specific path; the cache of model answers (code/cache/llm) is excluded, the screenshot cache is kept and documented.
- verify_package.py copies the would-be package (or extracts a real code.zip) to a clean folder, runs main.py with no key
  and compares the sha256 with the trusted baseline, then runs run_all.py there. Rehearsal result: passed (116 files).
- run_all_report.json no longer records temp-folder paths. code.zip itself is still the old build until authorised.

## 20. Typed shapes, multi-line SQL values, live check tool

- audit/shapes.py: TypedDicts for request, manifest, activity and rule rows, steps and the ambiguous-group payload, plus
  JsonValue, ResourceKey, Links and a Resolver protocol. They are plain dicts at runtime (nothing is converted). The signatures
  in audit/ use them: Any went from 102 mentions to 74 in audit/, what is left is raw provider JSON and external payloads.
  Two wrong annotations from the first pass were corrected (to_bool returns bool | None, parse_report returns int,
  Linker._adjudicate returns the accepted activity ids). No static checker is installed here, so the annotations are
  checked at import time and by ShapesMatchData (the TypedDict keys equal the real CSV columns, step fields and the
  ambiguous-group payload), not by mypy.
- SQL: a dump that sqlite reads whole is never salvaged, so a multi-line string value (even one with a line that starts like a
  statement) cannot be misread; only a dump sqlite rejects goes through statement-by-statement salvage.
- tools/live_check.py runs intent arbitration (second opinion) and API vision extraction against a real provider on scratch
  copies; results, failures and quota errors go to evaluation/live_validation.md.

## 21. Static type check (mypy) of audit/

- Section 20 said no static checker was run; one has been since. mypy 2.4.0 (installed in a scratch venv outside the repo) found
  260 errors in audit/ on its first run with default settings. They are fixed, without weakening annotations or changing
  behaviour: 0 errors now, also with the strict flags in code/mypy.ini (disallow-untyped-defs, disallow-incomplete-defs,
  warn-return-any, no-implicit-optional, check-untyped-defs, warn-unused-ignores, warn-redundant-casts).
- What the checker found that was wrong, not just missing: _post_with_retries was annotated as returning a dict but returns
  (dict, attempts); category_for returns None for an unknown record but was annotated str; Linker._adjudicate and several
  other annotations from the first pass were inexact; an unsupported evidence file type reached a None parser and only
  failed through a TypeError (now an explicit unsupported_type diagnostic). A few were typing gaps in the data shapes: requests
  gain is_deletion/intent/uid after intent and identity resolution (Request), event and capture times are always present
  (loaders.require_ts raises on a blank instead of returning None), a parsed JSON object is JsonObject (checked field by field
  at runtime), and the loaders accept any scalar (object).
- run_all.py has a types step (required when mypy is importable, a skip otherwise). tools/mypy_summary.py counts errors by
  file and code. .mypy_cache is ignored by git and by make_zip (the first dry run after installing mypy would have packed it).
- Not covered: evaluation/ and tools/ are outside the mypy scope. A passing run shows consistent signatures, not correctness;
  the behavioural checks are the suites and the baseline comparison, which still pass with output.csv unchanged.

## 22. Audit follow-up: package, trace and entry-point fixes

Found by a read-only scoring audit (nothing in the repository was written during it); fixed here.
- code.zip was stale (built before the verifier, parser salvage, typing and run_all existed). It is rebuilt only on the owner's
  authorisation; tools/make_zip.py now also adds README.md at the archive root, as the challenge's own packaging command does
  (zip -r code.zip README.md code), carrying this project's run instructions. Its audit requires that file, evaluation/main.py,
  both reports and the baseline, and refuses secrets, machine paths, datasets and nested archives (PackagingGuards tests).
- agent-trace.zip did not exist. tools/make_trace.py stages the Claude Code session files with original names and structure,
  adds /export transcripts from a folder, redacts the .env values and key-shaped strings in the copies, re-scans the zip and
  deletes it if anything survived. Its dry run on the real session file found 26 secret occurrences (the Gemini key pasted
  in chat is in the transcript), so redaction is not optional and that key should be rotated. /export can only be run
  inside each session, and the session file must be complete (quit the app first): the tool warns about both.
- code/evaluation/main.py was a 0-byte starter placeholder; it now runs the whole evaluation (run_all.py).
- code/cache/README.md describes the caches, because the code judge ignores .json files (README.md line 99 of the starter).
- Re-checked and unchanged: the five sample differences (three ids that exist nowhere in dataset/, two ties/ownership cases) have
  no legitimate fix; a third live-provider probe still met HTTP 429 (a later one, after the quota reset, worked: see section 24).

## 23. Prune, overfitting and ambiguity audit

- Pruned: seven superseded one-off tools (annotate_defs, count_adjudications, debug_align, trace_model_calls, probe_parsers,
  smoke_model, probe_schema), four ad-hoc probes folded into one reproducible tools/gap_probes.py, and dead code found by a
  scan for unreferenced names (Linker._FAR_FUTURE, validate.read_output, a no-op branch in logs.parse_line, an unused test
  helper). The optional model layer stays: it was asked for, it is tested (52 tests) and it is the only live-model surface, but
  it has no measured effect on output.csv and the docs say so.
- Overfitting: no case, user or evidence id in logic; real dataset ids used as prompt examples were replaced by synthetic ones;
  the two alignment windows were stress-tested (gap_probes.py sensitivity) and sit just outside the lag the data shows.
- Ambiguity: a log line with no state no longer hides earlier informative evidence, and read/loaded events mean the record
  exists; the verifier applies the same rule. Six of the 518 answers gain one verification step each, no status changes, the
  32 samples are unchanged, so the trusted baseline was replaced explicitly (run_all.py --update-baseline) after the diff was
  reviewed. Linked records with no observation at all are now listed per case (confidence.csv, unverified_records).
- Custom suite 22 cases, 14 mutations, test_parsers 39 tests.

## 24. Submission-readiness pass

- Unverifiable linked records: all 29 turned out to be records only the worker stream shows (accounts, support records,
  backups, flows), with no observation in any evidence file and no table dump that could prove them absent by the audit
  time. A linked account in an INCOMPLETE answer is now re-verified like every other account (its check is future work, no
  evidence_id), because all 22 labelled INCOMPLETE answers carry their account check: 12 of the 518 answers gain
  that step, no status changes, the 32 samples are unchanged. The records in COMPLETE answers stay left out (a COMPLETE
  check needs the evidence they do not have) and are listed per case in confidence.csv.
- Live model: the provider answered this time. Intent arbitration and the API screenshot route were run against the same free-tier
  model; two real defects surfaced and were fixed (guardrails now correct the model through the repair loop instead of silently
  dropping rows; the prompt says which audit event is the retained one). Results and limits: evaluation/live_validation.md and
  live_validation_notes.md. The committed screenshot readings stay in force.
- Static types: mypy now covers audit/, main.py, evaluation/ and tools/ (52 files, strict flags). Findings that were real:
  a mis-annotated return in make_report, a missing None guard in main.py, an unchecked norm_user result stored as a link.
  The five unittest files are type-checked only where annotated (their unannotated bodies are out of scope).
- Pruned: prefetch_investigations.py (tied to private internals and to a quota problem) and verify_run.py (covered by
  test_verifier and the verifier gate in main.py), plus a dead branch in policy.assess.
- Custom suite 23 cases, 15 mutations.
