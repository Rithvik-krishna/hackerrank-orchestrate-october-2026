# run_all result

**PASSED**

| step | result | seconds | detail |
|---|---|---|---|
| lint | ok | 0.4 | unused imports: 0 | functions over 60 lines: 0 | audit/ functions with any type annotation: 170/170 |
| types | ok | 1.7 | Success: no issues found in 52 source files (mypy 2.4.0 (compiled: yes)) |
| tests | ok | 41.3 | test_llm_layer 54, test_verifier 22, test_provenance 9, test_regression 19, test_parsers 39 |
| full run | ok | 1.2 | independent verifier: 0 errors in 0 cases, 0 warnings |
| schema | ok | 0.0 | 518 requests; fresh run: valid; root output.csv: valid |
| baseline | ok | 0.0 | matches the trusted baseline; root output.csv is identical |
| samples | ok | 1.2 | regression check: 27 baseline cases, 0 lost, 0 gained; status 1.0, row exact 0.844, step F1 0.982 |
| custom suite | ok | 0.5 | 23 cases, row exact 1.0 |
| mutation | ok | 12.9 | 15 rules caught, 0 missed |
| error analysis | ok | 0.1 | 32 labelled cases; 27 identical to the gold as data, 5 differ (row exact 0.844); 193 gold steps, 190 predicted. |
| reports | ok | 6.6 | evaluation, confidence and usage reports written |

Commands and exit codes are in `run_all_report.json`.
