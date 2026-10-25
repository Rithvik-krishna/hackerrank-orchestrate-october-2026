# Usage report

Summary of the final full-dataset run that produced `output.csv` (518 requests), taken from
`code/cache/usage_ledger.jsonl`.

The ledger holds 413 live calls and 5 cache hits (a cache hit costs no tokens). The final run that produced `output.csv` was deterministic and made **no** model calls: these entries are the trials of the optional model layer (`--llm`, `--agent`) made during development, and they are cumulative.

## Per provider and model

| provider | model | calls | input tokens | output tokens | failed | estimated cost |
|---|---|---|---|---|---|---|
| gemini | gemini-3.1-flash-lite | 413 | 444545 | 29034 | 182 | $0.0561 |

## Per purpose

| purpose | model | calls | input tokens | output tokens | failed | estimated cost |
|---|---|---|---|---|---|---|
| adjudicate | gemini-3.1-flash-lite | 217 | 227583 | 24168 | 0 | $0.0324 |
| investigate | gemini-3.1-flash-lite | 196 | 216962 | 4866 | 182 | $0.0236 |

## Totals

| total | value |
|---|---|
| model calls (live) | 413 |
| cache hits | 5 |
| HTTP attempts including retries | 499 |
| input tokens | 444545 |
| output tokens | 29034 |
| total tokens | 473579 |
| average tokens per request | 914.2 |
| estimated total cost | $0.0561 |
| estimated cost per request | $0.000108 |

Purposes: `intent` (request arbitration and second opinion, fast model), `adjudicate` (ambiguous links, reasoning
model), `vision` (screenshots, reasoning model).

## Screenshots

The 11 screenshots in the committed cache (`code/cache/vision/`) were **not** read through an API call: no key was available in the build environment, so the screenshots were read by the coding assistant used for development (Claude Code, model `claude-sonnet-5-5`) and transcribed; `code/tools/transcribe_screenshots.py` is the source of those files, and that reading is not metered here.

## Development-time assistance

The code was written with Claude Code in this repository; that session is recorded in `log.txt`. It is not part of
the run that produced `output.csv` and is not counted above.
