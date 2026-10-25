# Gap analysis and interview notes (built from the code and measured results)

Everything here points at a real file and a measured number. Where something is weak it says so.

## 1. What is implemented, against the rubric

| Area | Implemented (file) | Honest status | Verified by |
|---|---|---|---|
| Architecture | Deterministic pipeline: `intent.py` -> `identity.py` -> `evidence/*` -> `linker.py` -> `state.py` -> `policy.py` -> `steps.py` -> `validate.py` (orchestrated by `pipeline.py`) | Real and the source of every answer. Not an autonomous agent, by choice | `evaluate.py` on 32 samples: status 1.000, row exact 0.844 |
| Model-driven parts | `intent_llm.py`, `adjudicator.py`, `investigator.py` (tool loop), `vision.py` behind `llm.py` | Real code, run against a real model (Gemini free tier). **Measured accuracy effect: none** (every finished answer was NONE; most investigator calls hit the quota). `output.csv` does not depend on them | `usage_report.md`, `test_llm_layer.py` (52 tests) |
| Prompts and tools | Forced tool calls, JSON-schema validation and repair in `llm.validate`/`call_tool`; read-only size-capped investigator tools | Real. Request text and evidence are fenced as untrusted data | `test_llm_layer.py`, custom_14 and custom_18 |
| Retrieval and time | `identity.resolve_email` (historical emails, exact match), `state.resolve_state` (latest observation <= `audit_at`), manifest `captured_at` gate for citations | Real | custom_01, 02, 07, 08, 13 and 15 mutations |
| Multimodal | 7 log dialects, SQL via sqlite, JSON, PDF (ASCII85+Flate), screenshots via a vision cache | Screenshots were transcribed by the dev session, not an API call (stated in `usage_report.md`) | `main.py --parse-report` |
| Verification | `verifier.py` re-derives checks from raw evidence, gates `main.py` | Real; independent of `state.py`/`policy.py` | `test_verifier.py` (22), 0 errors on 518 answers |
| Engineering | Type hints on all 170 `audit/` functions, mypy (strict flags) clean on `audit/`, no function over 60 lines, env-only secrets, `lint_check.py`, regression baseline | Evaluation scripts only partly annotated | `lint_check.py`, `test_regression.py` |

Remaining gaps (not hidden): the model layer is optional and adds no measured accuracy; 5 sample cases differ by one
verification step (see `rubric_audit.md`); the order tie-break is a measured regularity, not a documented signal.

## 2. Answers grounded in the code

1. **Why not a fully autonomous multi-agent system?** The task has hard constraints (cutoff time, policy table, exact
   schema, no invented evidence). Those are cheaper and safer as code; a model only sees the pieces code cannot decide.
   Measured: the model layer added nothing, so the deterministic output is the submission.
2. **Remaining record vs stale observation?** `state.resolve_state(store, key, audit_at, ...)` takes the latest
   observation with `observed_at <= audit_at`; within one snapshot a replica that still shows the record outranks a
   primary that does not (custom_19; mutation "a primary that omits the record outranks a replica" is caught).
3. **Prompt injection?** Evidence and request text are data: the deterministic engine never executes them
   (custom_14: connector name and support message say "mark COMPLETE"; custom_18: the request says it). Model prompts fence
   them as untrusted, and a model answer is only a proposal that `adjudicator.accept` / the investigator review checks.
4. **What stops a model inventing evidence or declaring COMPLETE?** It never writes steps or statuses. It can only
   propose a link, and the link is accepted only if the ids were provided, confidence is high, the basis is substantive
   (not timing/status/order) and, for the investigator, every id came from its own tool results and the chosen record was
   inspected. Status comes from `policy.assess`; `verifier.py` re-checks false-COMPLETE from raw evidence.
5. **Hardest error the evaluator found?** The independent verifier disagreed with the engine on 8 cases: attachments seen
   in an attachment scan were classified as messages (`state.resource_category` now prefers attachment). Before that,
   cases leaked across requests of the same person (fixed by "evidence older than `requested_at` cannot link").
6. **Improved vs rejected?** Improved: omission-only-for-DELETE-records took status 0.906 -> 1.000; screenshots removed 7
   empty cases. Rejected: `AUDIT_OWNERSHIP=any` (fixes case_32 using post-cutoff evidence, which the rules forbid);
   `AUDIT_ZIP_MODE=unique` (never guesses, but row exact 0.625 vs 0.844); the model layer as an accuracy source.
7. **Missing or contradictory evidence?** Missing: no step is invented; the ledger (`ledger.py`) marks the case
   medium/low and lists unresolved ambiguity (60 cases may lack a record). Contradictory: latest record wins, replica
   drift rule above; the provenance graph (`main.py --graph CASE`) prints the conflict and the rule used.
8. **Before real customer data?** Access control and audit of the auditor itself, encrypted caches (the LLM and vision
   caches hold request text), a data-processing agreement for any model provider, real PII redaction before any model
   call, stronger identity matching than exact strings, and human review of low-confidence cases.

## 3. Demo script (about two minutes)

```text
python code/main.py --explain case_05      # decision trail
python code/main.py --graph case_05        # provenance, replica drift conflict
python code/evaluation/evaluate.py code/evaluation/sample_predictions.csv --diff
python code/evaluation/custom_cases/mutation_check.py
```
