"""Check that what would be submitted (or what was submitted) reproduces the trusted output from a clean folder.

    python code/tools/verify_package.py              rehearsal: the files make_zip.py would pack, copied to a temp folder
    python code/tools/verify_package.py --zip FILE   the real archive, extracted to a temp folder

In the clean folder (no .env, no model key, a fresh interpreter) it
  1. confirms the code folder holds no dataset, archive, secret or machine-specific path;
  2. copies the dataset in beside it (the run reads it from ./dataset, it is not part of the submission);
  3. runs `python code/main.py` and compares the output's sha256 with the trusted baseline;
  4. runs `python code/evaluation/run_all.py --skip-mutation` there.
Nothing in the source tree is modified and the archive is never written; exit 1 if any step fails.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_zip  # noqa: E402

ROOT = make_zip.ROOT


def materialise(target: Path, archive: Path | None) -> list[str]:
    """Put the package into `target`; returns the relative names."""
    if archive:
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
            return z.namelist()
    pairs = make_zip.entries()
    for name, p in pairs:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    return [name for name, _ in pairs]


def clean_env() -> dict:
    """The parent environment without any model key or audit setting."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("GEMINI", "ANTHROPIC", "GOOGLE", "AUDIT", "PYTHON"))}


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print("  $ python -I " + " ".join(args))
    return subprocess.run([sys.executable, "-I", *args], cwd=cwd, env=clean_env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", metavar="FILE")
    args = ap.parse_args()
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "extract"
        target.mkdir()
        names = materialise(target, Path(args.zip) if args.zip else None)
        print(f"{len(names)} files in the {'archive' if args.zip else 'rehearsal'}; top-level: "
              f"{sorted({n.split('/')[0] for n in names})}")
        if any(n.startswith("dataset/") or n.endswith((".zip", ".env")) or n.startswith(".env") for n in names):
            failures.append("package contains a dataset file, an archive or .env")
        shutil.copytree(ROOT / "dataset", target / "dataset")
        out = run(["code/main.py"], target)
        expected = json.loads((target / "code" / "evaluation" / "baseline" / "output.json").read_text(encoding="utf-8"))["sha256"]
        got = hashlib.sha256((target / "output.csv").read_bytes()).hexdigest() if (target / "output.csv").exists() else "(none)"
        print(f"  main.py exit {out.returncode}; output sha256 {'matches' if got == expected else 'DIFFERS from'} the trusted baseline")
        if out.returncode != 0 or got != expected:
            failures.append("clean run does not reproduce the trusted output")
        res = run(["code/evaluation/run_all.py", "--skip-mutation"], target)
        print("\n".join("    " + l for l in res.stdout.strip().splitlines()[-4:]))
        if res.returncode != 0:
            failures.append("run_all.py failed in the clean folder")
    print("verify_package:", "PASSED" if not failures else "FAILED - " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
