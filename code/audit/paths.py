"""Repository-relative paths, so the pipeline runs from any OS and working directory."""

import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CODE_DIR.parent
# AUDIT_DATASET lets the custom evaluation cases run the same pipeline on a small dataset
DATASET = Path(os.environ.get("AUDIT_DATASET") or REPO_ROOT / "dataset")
EVIDENCE_ROOT = DATASET  # manifest paths are relative to dataset/
OUTPUT_CSV = REPO_ROOT / "output.csv"
CACHE_DIR = CODE_DIR / "cache"
VISION_CACHE = CACHE_DIR / "vision"
EVAL_DIR = CODE_DIR / "evaluation"
