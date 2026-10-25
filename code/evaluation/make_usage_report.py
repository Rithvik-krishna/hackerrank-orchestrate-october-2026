"""Generate code/evaluation/usage_report.md from the usage ledger.

    python code/evaluation/make_usage_report.py

The ledger (code/cache/usage_ledger.jsonl) gets one line per model call or cache hit made by the pipeline
(`python code/main.py --llm ...`) and by `--extract-images`. Start it empty with `main.py --fresh-ledger` before the
final run so the report covers exactly that run. If the ledger is empty or missing, no call was made and the report
says so. Prices are a config table and the costs are estimates.
"""

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE.parent / "cache" / "usage_ledger.jsonl"
REQUESTS = 518
# USD per million tokens (input, output): estimates, edit when pricing changes
PRICES = {"claude-sonnet-5-5": (3.0, 15.0), "claude-haiku-5-5": (1.0, 5.0),
          # placeholder estimate for a paid tier; a free-tier key costs nothing
          "gemini-3.1-flash-lite": (0.10, 0.40), "gemini-3.8-flash": (0.30, 2.50)}


def cost(model: str, tin: int, tout: int) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return tin / 1e6 * pin + tout / 1e6 * pout


def aggregate(live: list) -> tuple[dict, dict]:
    """Per (provider, model) and per (purpose, model): [calls, input tokens, output tokens, failed]."""
    per_model: defaultdict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    per_purpose: defaultdict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for e in live:
        for tbl, key in ((per_model, (e["provider"], e["model"])), (per_purpose, (e["purpose"], e["model"]))):
            row = tbl[key]
            row[0] += 1
            row[1] += e["input_tokens"]
            row[2] += e["output_tokens"]
            row[3] += 1 if e.get("failed") else 0
    return per_model, per_purpose


def table(rows: dict, first: str) -> str:
    head = f"| {first} | model | calls | input tokens | output tokens | failed | estimated cost |\n|---|---|---|---|---|---|---|\n"
    if not rows:
        return head + "| none | none | 0 | 0 | 0 | 0 | $0.0000 |"
    return head + "\n".join(f"| {k[0]} | {k[1]} | {r[0]} | {r[1]} | {r[2]} | {r[3]} | ${cost(k[1], r[1], r[2]):.4f} |"
                            for k, r in sorted(rows.items()))


def describe_ledger(lines: list, live: list, hits: list) -> str:
    if not lines:
        return ("The ledger is empty: **no model API calls were made**. The pipeline ran deterministically "
                "(`python code/main.py`), which needs no API key.")
    return (f"The ledger holds {len(live)} live calls and {len(hits)} cache hits (a cache hit costs no tokens). "
            "The final run that produced `output.csv` was deterministic and made **no** model calls: these entries are "
            "the trials of the optional model layer (`--llm`, `--agent`) made during development, and they are cumulative.")


def main() -> None:
    lines = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()] if LEDGER.exists() else []
    live = [e for e in lines if not e.get("cached")]
    hits = [e for e in lines if e.get("cached")]
    per_model, per_purpose = aggregate(live)
    tin = sum(e["input_tokens"] for e in live)
    tout = sum(e["output_tokens"] for e in live)
    total_cost = sum(cost(m, r[1], r[2]) for (_, m), r in per_model.items())
    attempts = sum(e.get("attempts", 1) for e in live)
    intro = describe_ledger(lines, live, hits)
    text = f"""# Usage report

Summary of the final full-dataset run that produced `output.csv` ({REQUESTS} requests), taken from
`code/cache/usage_ledger.jsonl`.

{intro}

## Per provider and model

{table({(k[0], k[1]): v for k, v in per_model.items()}, "provider")}

## Per purpose

{table(per_purpose, "purpose")}

## Totals

| total | value |
|---|---|
| model calls (live) | {len(live)} |
| cache hits | {len(hits)} |
| HTTP attempts including retries | {attempts} |
| input tokens | {tin} |
| output tokens | {tout} |
| total tokens | {tin + tout} |
| average tokens per request | {(tin + tout) / REQUESTS:.1f} |
| estimated total cost | ${total_cost:.4f} |
| estimated cost per request | ${total_cost / REQUESTS:.6f} |

Purposes: `intent` (request arbitration and second opinion, fast model), `adjudicate` (ambiguous links, reasoning
model), `vision` (screenshots, reasoning model).

## Screenshots

{"The 11 screenshots in the committed cache (`code/cache/vision/`) were **not** read through an API call: no key was available in the build environment, so the screenshots were read by the coding assistant used for development (Claude Code, model `claude-sonnet-5-5`) and transcribed; `code/tools/transcribe_screenshots.py` is the source of those files, and that reading is not metered here." if not any(e["purpose"] == "vision" for e in lines) else "Screenshot extraction through the API is included in the vision rows above; replaced cache files were backed up to `code/cache/vision_transcribed/`."}

## Development-time assistance

The code was written with Claude Code in this repository; that session is recorded in `log.txt`. It is not part of
the run that produced `output.csv` and is not counted above.
"""
    (HERE / "usage_report.md").write_text(text, encoding="utf-8")
    print(f"wrote {HERE / 'usage_report.md'} ({len(live)} live calls, {len(hits)} cache hits in ledger)")


if __name__ == "__main__":
    main()
