"""Build code.zip for submission: the code/ directory without caches of model answers, bytecode, the dataset or secrets.

    python code/tools/make_zip.py --dry-run    audit and list what would be packed; writes nothing
    python code/tools/make_zip.py              audit, then write ./code.zip and list what it holds

Contents are code/ as `code/...`, so evaluation/evaluation_report.md and evaluation/usage_report.md sit under
code/evaluation/. Left out: generated predictions inside the custom suite (reproducible), the cache of model answers
(code/cache/llm: derived from prompts built out of the dataset, and never read by the deterministic run), bytecode, empty
placeholder files and `.env`. Kept on purpose: code/cache/vision (the transcribed screenshots the pipeline reads as
evidence; each file says how it was made) and the usage ledger (token counts only).

The audit refuses to pack when it finds a possible secret, a machine-specific path, a dataset file, a nested archive, or
when a required file (README, entry point, the two reports, the trusted baseline) is missing.
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE = ROOT / "code"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules"}
SKIP_PREFIXES = ("code/cache/llm/",)
SKIP_NAMES = {"sample_predictions.csv", "predictions.csv", "mut_predictions.csv", ".env"}
REQUIRED = ["README.md", "code/README.md", "code/main.py", "code/evaluation/main.py", "code/evaluation/evaluation_report.md", "code/evaluation/usage_report.md",
            "code/evaluation/baseline/output.json", "code/cache/vision"]
SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{10,}|AIza[0-9A-Za-z_\-]{30,}|AQ\.[A-Za-z0-9_\-]{30,}"
                    r"|(ANTHROPIC|GEMINI|GOOGLE)_API_KEY\s*=\s*['\"]?[A-Za-z0-9_.\-]{10,})")
MACHINE_PATH = re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}\w+|/Users/\w+/|/home/\w+/|AppData\\{1,2}Local")
TEXT = {".py", ".md", ".json", ".jsonl", ".txt", ".csv"}


def collect() -> list[Path]:
    keep = []
    for p in sorted(CODE.rglob("*")):
        rel = p.relative_to(ROOT).as_posix()
        if (p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and not rel.startswith(SKIP_PREFIXES)
                and p.name not in SKIP_NAMES and p.suffix != ".pyc" and p.stat().st_size > 0):  # empty placeholders carry nothing
            keep.append(p)
    return keep


def entries() -> list[tuple[str, Path]]:
    """(name inside the archive, file on disk). The archive also carries README.md at its root, as the challenge's own
    packaging command does (`zip -r code.zip README.md code`); it is the same file as code/README.md, which holds this
    project's setup and run instructions."""
    return [(p.relative_to(ROOT).as_posix(), p) for p in collect()] + [("README.md", CODE / "README.md")]


def audit(pairs: list[tuple[str, Path]]) -> list[str]:
    """Reasons the archive must not be built (empty when it is clean)."""
    problems = []
    names = [n for n, _ in pairs]
    for need in REQUIRED:
        if not any(n == need or n.startswith(need + "/") for n in names):
            problems.append(f"required file missing: {need}")
    for name, p in pairs:
        if name.startswith("dataset/") or p.suffix in {".zip", ".gz", ".tar", ".7z"}:
            problems.append(f"must not be packed: {name}")
        if p.suffix in TEXT:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if SECRET.search(text):
                problems.append(f"possible secret in {name}")
            if p.name != "make_zip.py" and MACHINE_PATH.search(text):
                problems.append(f"machine-specific path in {name}")
    return problems


def main() -> None:
    dry = "--dry-run" in sys.argv
    pairs = entries()
    problems = audit(pairs)
    for line in problems:
        print("AUDIT FAILED:", line)
    if problems:
        sys.exit("refusing to pack")
    total = sum(p.stat().st_size for _, p in pairs)
    if dry:
        for name, _ in pairs:
            print(name)
        print(f"\ndry run: {len(pairs)} files, {total / 1024:.0f} KiB uncompressed; audit clean; nothing written")
        return
    out = ROOT / "code.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, p in pairs:
            z.write(p, name)
    for name, _ in pairs:
        print(name)
    print(f"\n{len(pairs)} files, {out.stat().st_size / 1024:.0f} KiB -> {out}")


if __name__ == "__main__":
    main()
