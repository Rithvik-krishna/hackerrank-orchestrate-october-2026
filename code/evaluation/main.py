"""Evaluation entry point of the starter layout (code/evaluation/main.py): runs every check and writes the reports.

    python code/evaluation/main.py [--skip-mutation] [--update-baseline]

This is run_all.py under the name the starter layout expects: lint, mypy (when installed), the five test suites, the
full 518-request run, schema validation, the comparison with the trusted baseline, the labelled samples with the
regression check, the custom suite, the mutation check, the row-level error analysis, and the evaluation, confidence and
usage reports. See run_all.py for the details of each step; the exit code is 1 if a required check fails.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_all import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
