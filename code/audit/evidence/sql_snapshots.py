"""SQLite-compatible snapshot exports, executed in memory.

Three table shapes occur:
  * column tables (customer_profiles_primary, search_documents_primary, inventory tables)
  * generic rows (resource_id, origin_system, collection_name, record_json, updated_at)
  * snapshot_scope(origin_system, captured_at) giving each system its own capture time
Table suffixes (_primary, _read_replica, _failover_replica, _replica_<region>) become replica_role.
"""

from datetime import datetime
from pathlib import Path
import json
import re
import sqlite3

from ..diagnostics import ITEM, Diagnostic, Salvage, UnparseableFile, short
from ..loaders import parse_ts
from ..model import Coverage
from .common import category_for, record_observation, system_for

_CAPTURED_RE = re.compile(r"--\s*Captured at:\s*(\S+)")
_ROLE_RE = re.compile(r"_(primary|read_replica|failover_replica|reporting_replica|replica_[a-z_]+|replica)$")


def _role(table: str) -> str:
    m = _ROLE_RE.search(table)
    return m.group(1) if m else "primary"


def parse(evidence_id: str, path: Path, manifest_time: datetime) -> tuple:
    text = path.read_text(encoding="utf-8")
    m = _CAPTURED_RE.search(text)
    file_time = (parse_ts(m.group(1)) if m else None) or manifest_time

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.executescript(text)   # a dump that sqlite reads whole needs no salvage, whatever its string values contain
        skipped: list = []
    except sqlite3.Error:
        db.close()   # part of the dump may have run; start again, statement by statement
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        skipped = _execute_salvaging(evidence_id, db, text)
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]

    scope = {}
    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: parse_ts(r["captured_at"]) or file_time for r in db.execute("select * from snapshot_scope")}

    observations, coverage = [], []
    for table in tables:
        if table == "snapshot_scope":
            continue
        role = _role(table)
        rows = [dict(r) for r in db.execute(f'select * from "{table}"')]
        by_system: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            record = dict(row)
            payload = record.pop("record_json", None)
            if payload:
                record = {**json.loads(payload), **{k: v for k, v in record.items() if k != "resource_id"}}
            rid = record.get("resource_id")
            if not rid:
                continue
            system = record.get("origin_system") or system_for(rid)
            collection = record.get("collection_name") or table
            when = scope.get(system, file_time)
            if scope and system not in scope:
                continue  # outside this snapshot's declared scope
            observations.append(record_observation(
                evidence_id, when, record, system=system, collection=collection,
                source="sql", extra_attrs={"replica_role": role, "table": table},
            ))
            by_system.setdefault((system, collection), set()).add(rid)

        # Coverage: which systems this table speaks for, even when it has no row for a resource.
        systems = set(s for s, _ in by_system) or _systems_for_empty_table(table, scope)
        for system in systems:
            ids = set().union(*[v for (s, _), v in by_system.items() if s == system]) if by_system else set()
            when = scope.get(system, file_time)
            coverage.append(Coverage(evidence_id, when, system, table, frozenset(ids), scope="full"))
    return (observations, coverage, Salvage(skipped)) if skipped else (observations, coverage)


_STATEMENT_START = re.compile(r"^\s*(INSERT\s+INTO|CREATE\s+TABLE|BEGIN|COMMIT)\b", re.IGNORECASE)


def _is_comment(chunk: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in chunk.splitlines())


def _execute_salvaging(evidence_id: str, db: sqlite3.Connection, text: str) -> list:
    """Run the dump one statement at a time; a statement that fails is reported and skipped, the rest still run.

    Only reached for a dump sqlite rejects as a whole (a dump it accepts is never salvaged, so a multi-line string value,
    even one with a line that starts like a statement, cannot be misread there). A statement ends where sqlite says it is
    complete, which already copes with multi-line string values. If a new INSERT/CREATE/BEGIN/COMMIT starts while the
    previous statement is still open (a lost closing quote or parenthesis), that statement is reported as unterminated
    and the new one starts clean, so one damaged line cannot swallow the valid lines after it; that boundary is a
    heuristic, and a damaged dump whose string values contain lines that start like statements can be misjudged (the
    result is then reported as skipped items, never as a clean read). A dump in which not a single statement runs is not
    SQL we can trust at all and fails as a file."""
    skipped: list[Diagnostic] = []
    buffer: list[str] = []
    start = ran = number = 0

    def run(chunk: str, first_line: int) -> None:
        nonlocal ran, number
        number += 1
        try:
            db.execute(chunk)
            ran += 1
        except sqlite3.Error as exc:
            skipped.append(Diagnostic(evidence_id, "sql", ITEM, f"statement {number} (line {first_line})", "sql_error", short(exc)))

    for no, line in enumerate(text.splitlines(), 1):
        if buffer and _STATEMENT_START.match(line) and not _is_comment("\n".join(buffer)):
            number += 1
            skipped.append(Diagnostic(evidence_id, "sql", ITEM, f"statement {number} (line {start})", "unterminated_statement",
                                      "a new statement starts before this one is closed"))
            buffer = []
        if not buffer:
            start = no
        buffer.append(line)
        chunk = "\n".join(buffer)
        if sqlite3.complete_statement(chunk) and not _is_comment(chunk):
            run(chunk, start)
            buffer = []
    rest = "\n".join(buffer)
    if rest.strip() and not _is_comment(rest):
        number += 1
        skipped.append(Diagnostic(evidence_id, "sql", ITEM, f"statement {number} (line {start})", "truncated_statement",
                                  "the file ends inside a statement"))
    if skipped and ran == 0:
        raise UnparseableFile("not_sql", "no statement could be executed")
    return skipped


def _systems_for_empty_table(table: str, scope: dict[str, datetime]) -> set[str]:
    """An empty inventory table still proves absence for its system (e.g. customer_exports)."""
    cat = category_for("", table)
    guess = {
        "USER_EXPORT": "export_service", "USER_PROFILE": "account_service",
        "SEARCH_INDEX_ENTRY": "search_service", "ANALYTICS_PROFILE": "analytics_platform",
        "SUPPORT_MESSAGE": "support_platform", "DATA_FLOW": "integration_service",
        "BACKUP_ARCHIVE": "backup_service", "SECURITY_AUDIT_LOG": "security_service",
    }.get(cat) if cat else None
    if guess and (not scope or guess in scope):
        return {guess}
    return set()
