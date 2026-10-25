# What is in code/cache

The code judge ignores raw data files (`.json`, `.jsonl`, `.csv`), so this note describes them.

| Path | What it is | Used by the deterministic run that makes `output.csv`? |
|---|---|---|
| `vision/evidence_NN.json` (11 files) | One transcription per screenshot in `dataset/evidence/images/`: the customer records the screen shows (`system_id`, `resource_id`, `category`, `status`, identifiers, `quote`, `identity_mismatch`). The pipeline reads them like any other evidence (`audit/evidence/images.py`). Each file's `extracted_by` says how it was made: the development session (Claude Code, `claude-sonnet-5-5`) read the screenshot directly, with no API call. `tools/transcribe_screenshots.py` is the source of these files; `audit/vision.py` is the API route that can replace them (`main.py --extract-images --force`, which backs the old file up to `vision_transcribed/` and prints how many rows agree). That API route was run live against a small model on 26 Oct 2026 (9 of 11 screenshots read, the 32 labelled samples unchanged; see `evaluation/live_validation_notes.md`), which is why the committed readings stay. | **Yes.** Without them seven deletion cases would have no evidence at all. |
| `usage_ledger.jsonl` | One line per model call made while developing the optional model layer (provider, model, purpose, tokens, attempts, seconds). It holds no prompts or answers. `evaluation/usage_report.md` is generated from it. The submitted `output.csv` was produced without any model call. | No |
| `llm/` | Cached model answers (one file per prompt). Built from prompts made out of the dataset and never read by the deterministic run, so it is **left out of `code.zip`** (`tools/make_zip.py`). | No |

To regenerate the screenshot readings with a model, set `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` in the environment or in an
uncommitted `.env`, then run `python code/tools/live_check.py` (scratch copy, nothing is overwritten) or
`python code/main.py --extract-images --force` (replaces the committed readings; compare `output.csv` afterwards with
`python code/evaluation/run_all.py`).
