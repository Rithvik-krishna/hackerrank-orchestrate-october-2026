"""Stage the Claude Code session traces of this challenge as agent-trace.zip (see agent_trace_instructions.md).

    python code/tools/make_trace.py --dry-run [--match TEXT ...] [--export-dir DIR]   list what would go in; writes nothing
    python code/tools/make_trace.py           [--match TEXT ...] [--export-dir DIR]   write ./agent-trace.zip

What it does
  * looks in ~/.claude/projects (override with --projects-dir) for the project folders whose name contains one of the
    --match texts (default "orchetrate": the folder name encodes the checkout path) and takes every *.jsonl in them,
    subagent folders included, with the original names and structure under agent-trace/claude-code/;
  * adds the readable transcripts you saved with `/export` (--export-dir): that command can only be run inside each
    session, so this tool cannot create them;
  * redacts secrets in the copies: the literal values in the repository's .env and anything shaped like an API key become
    [REDACTED] (the originals under ~/.claude are never touched); the zip is then re-read and re-scanned;
  * never takes authentication, settings or credential files, and refuses an unreadable or still-open session.
It does not summarise or convert the structured logs. Quit the app first so the session files are complete; a file written
in the last two minutes is reported as probably still open.
"""

import argparse
import os
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET = re.compile(rb"(sk-ant-[A-Za-z0-9_\-]{10,}|AIza[0-9A-Za-z_\-]{30,}|AQ\.[A-Za-z0-9_\-]{30,}"
                    rb"|(?:ANTHROPIC|GEMINI|GOOGLE)_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_.\-]{10,})")
REFUSED = re.compile(r"(auth|credential|settings|config|\.env|mcp)", re.IGNORECASE)
RECENT_SECONDS = 120
EXPORT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".html"}


def env_secret_values(env_file: Path) -> list[bytes]:
    """The values assigned in .env (never printed): each is redacted wherever it appears in a trace."""
    values = []
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*?)\s*$", line)
            if m and len(m.group(1).strip("'\"")) >= 8:
                values.append(m.group(1).strip("'\"").encode())
    return values


def redact(data: bytes, literals: list[bytes]) -> tuple[bytes, int]:
    """(redacted bytes, number of replacements). The replacement contains no quote or backslash, so JSON stays valid."""
    count = 0
    for value in literals:
        count += data.count(value)
        data = data.replace(value, b"[REDACTED]")
    data, n = SECRET.subn(b"[REDACTED]", data)
    return data, count + n


def project_dirs(projects: Path, matches: list[str]) -> list[Path]:
    return sorted(d for d in projects.iterdir() if d.is_dir() and any(m.lower() in d.name.lower() for m in matches)) if projects.exists() else []


def session_files(dirs: list[Path]) -> list[Path]:
    return sorted(p for d in dirs for p in d.rglob("*.jsonl") if not REFUSED.search(p.name))


def export_files(export_dir: Path | None) -> list[Path]:
    if export_dir is None or not export_dir.exists():
        return []
    return sorted(p for p in export_dir.rglob("*") if p.is_file() and p.suffix.lower() in EXPORT_SUFFIXES and not REFUSED.search(p.name))


def plan(projects: Path, matches: list[str], export_dir: Path | None) -> list[tuple[str, Path]]:
    """(name inside the archive, source file)."""
    pairs = []
    for d in project_dirs(projects, matches):
        for p in session_files([d]):
            pairs.append((f"agent-trace/claude-code/{d.name}/{p.relative_to(d).as_posix()}", p))
    pairs += [(f"agent-trace/claude-code/exports/{p.name}", p) for p in export_files(export_dir)]
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects-dir", default=str(Path(os.environ.get("USERPROFILE", Path.home())) / ".claude" / "projects"))
    ap.add_argument("--match", action="append", default=None, help="text a project folder name must contain (repeatable)")
    ap.add_argument("--export-dir", help="folder holding the transcripts saved with /export")
    ap.add_argument("--out", default=str(ROOT / "agent-trace.zip"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    projects, matches = Path(args.projects_dir), args.match or ["orchetrate"]
    export_dir = Path(args.export_dir) if args.export_dir else None
    print(f"project folders under {projects}:")
    for d in sorted(projects.iterdir()) if projects.exists() else []:
        if d.is_dir():
            hit = any(m.lower() in d.name.lower() for m in matches)
            print(f"  {'USE ' if hit else 'skip'} {d.name} ({sum(1 for _ in d.rglob('*.jsonl'))} session file(s))")
    pairs = plan(projects, matches, export_dir)
    if not pairs:
        print("nothing to stage: no session files matched")
        return 1
    literals, total, warnings = env_secret_values(ROOT / ".env"), 0, []
    staged = []
    for name, p in pairs:
        data, n = redact(p.read_bytes(), literals)
        total += n
        staged.append((name, data))
        if time.time() - p.stat().st_mtime < RECENT_SECONDS:
            warnings.append(f"{name} was written in the last {RECENT_SECONDS} s: the session is probably still open (quit the app first)")
        print(f"  {name}  {len(data) / 1e6:.1f} MB, {n} secret(s) redacted")
    if not export_files(export_dir):
        warnings.append("no /export transcript included: run /export in each session, save it to a folder, pass --export-dir")
    for w in warnings:
        print("WARNING", w)
    if args.dry_run:
        print(f"dry run: {len(staged)} file(s), {total} redaction(s); nothing written")
        return 0
    out = Path(args.out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in staged:
            z.writestr(name, data)
    with zipfile.ZipFile(out) as z:   # re-read and re-scan what was written
        leaked = [n for n in z.namelist() if SECRET.search(z.read(n)) or any(v in z.read(n) for v in literals)]
    if leaked:
        out.unlink()
        print("a secret survived redaction in", leaked, "- the archive was deleted")
        return 2
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB); re-scanned: no secrets found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
