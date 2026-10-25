"""Parse diagnostics: what an evidence parser skipped or could not read, and where.

Two levels, kept apart on purpose:
  item   one line, statement or record is malformed; its neighbours are intact, so the rest of the file is kept.
  file   the file cannot be read, or the boundaries between items cannot be trusted (truncated or invalid JSON, a
         SQL dump with no executable statement, ...); nothing is taken from it and the run is reported as failed.

A file with item-level diagnostics is *degraded*: its valid records are used, but it can no longer prove that a record
is absent (see `EvidenceStore`), and the cases it documents are capped in the uncertainty ledger. Nothing is ever filled
in for a skipped item: no resource id, timestamp, identifier or evidence reference is invented.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

ITEM, FILE = "item", "file"
FIELDS = ["evidence_id", "source_file", "parser", "level", "location", "category", "message"]


@dataclass(frozen=True)
class Diagnostic:
    evidence_id: str
    parser: str       # "log", "sql", "json", "text"
    level: str        # ITEM or FILE
    location: str     # "line 12", "statement 3 (line 9)", "snapshots[1].records[0]", "" for a whole file
    category: str     # machine-readable: malformed_line, sql_error, missing_resource_id, truncated_json, ...
    message: str
    source_file: str = ""


class Salvage(list):
    """Third element of a parser result: the item-level diagnostics of a file that was only partly readable."""


class UnparseableFile(ValueError):
    """Raised by a parser when the file as a whole cannot be trusted; `category` says why."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:120]}"


def write_csv(path: Path, diagnostics: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for d in diagnostics:
            w.writerow([d.evidence_id, d.source_file, d.parser, d.level, d.location, d.category, d.message])
