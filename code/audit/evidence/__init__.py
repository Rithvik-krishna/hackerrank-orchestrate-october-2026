"""Evidence registry: dispatch each manifest file to its parser and collect the results."""

from dataclasses import replace
from collections import Counter, defaultdict

from .. import paths
from ..diagnostics import FILE, Diagnostic, Salvage, short
from ..loaders import load_manifest
from ..model import ABSENT, UNKNOWN, Coverage, Observation
from ..shapes import ResourceKey
from . import images, json_records, logs, pdf_text, sql_snapshots, text_records

_PARSERS = {
    ".sql": sql_snapshots.parse,
    ".json": json_records.parse,
    ".txt": text_records.parse,
    ".log": logs.parse,
    ".pdf": pdf_text.parse,
    ".png": images.parse,
}
_PARSER_NAMES = {".sql": "sql", ".json": "json", ".txt": "text", ".log": "log", ".pdf": "pdf", ".png": "image"}


class EvidenceStore:
    def __init__(self) -> None:
        self.observations: list[Observation] = []
        self.coverage: list[Coverage] = []
        self.documents: list[dict] = []      # PDF documents (reference-only content)
        self.errors: dict[str, str] = {}         # evidence_id -> message
        self.per_file: dict[str, tuple[str, int, int, str]] = {}       # evidence_id -> (parser, n_obs, n_cov, note)
        self.by_resource: defaultdict[ResourceKey, list[Observation]] = defaultdict(list)
        self.diagnostics: list[Diagnostic] = []    # every skipped item and every unreadable file (audit/diagnostics.py)
        self.degraded: dict[str, list[Diagnostic]] = {}       # evidence_id -> item-level diagnostics of a file that was only partly readable

    def load(self) -> "EvidenceStore":
        for fid, row in load_manifest().items():
            path = paths.EVIDENCE_ROOT / row["file_path"]
            parser = _PARSERS.get(path.suffix.lower())
            if parser is None:
                self.errors[fid] = f"UnsupportedFileType: no parser for {path.suffix!r}"
                self.diagnostics.append(Diagnostic(fid, "?", FILE, "", "unsupported_type", self.errors[fid], row["file_path"]))
                continue
            try:
                result = parser(fid, path, row["captured_at_dt"])
            except Exception as exc:  # report, never crash the whole run
                self.errors[fid] = f"{type(exc).__name__}: {exc}"
                self.diagnostics.append(Diagnostic(fid, _PARSER_NAMES.get(path.suffix.lower(), "?"), FILE, "",
                                                   getattr(exc, "category", "parser_exception"), short(exc), row["file_path"]))
                continue
            obs, cov = result[0], result[1]
            note = ""
            if len(result) > 2:
                if isinstance(result[2], dict):
                    self.documents.append(result[2])
                elif isinstance(result[2], Salvage):
                    obs, cov, note = self._degrade(fid, row["file_path"], obs, result[2])
                else:
                    note = result[2]
            for item in (*obs, *cov):
                item.file_time = row["captured_at_dt"]
            self.observations.extend(obs)
            self.coverage.extend(cov)
            self.per_file[fid] = (path.suffix.lower(), len(obs), len(cov), note)
        for o in self.observations:
            self.by_resource[(o.system_id, o.resource_id)].append(o)
        return self

    def _degrade(self, fid: str, source_file: str, obs: list, skipped: Salvage) -> tuple[list, list, str]:
        """A partly readable file keeps its valid records but loses the power to prove absence.

        * its table/snapshot coverage is dropped, so a record that is merely missing from it is not "absent";
        * a record it explicitly shows as ABSENT is demoted to UNKNOWN (the line may have been half of a larger fact);
        both are recorded on the observation (`attrs["degraded"]`, `attrs["demoted_from"]`), nothing is invented."""
        self.diagnostics.extend(replace(d, source_file=source_file) for d in skipped)
        self.degraded[fid] = list(skipped)
        for o in obs:
            o.attrs["degraded"] = True
            if o.status == ABSENT:
                o.attrs["demoted_from"] = ABSENT
                o.status = UNKNOWN
        return obs, [], f"degraded: {len(skipped)} item(s) skipped"


def parse_report() -> int:
    store = EvidenceStore().load()
    by_type: Counter[str] = Counter()
    zero = []
    for fid, (ext, n, c, note) in store.per_file.items():
        by_type[ext] += n
        if n == 0:
            zero.append((fid, ext, note))
    print(f"files parsed: {len(store.per_file)}  errors: {len(store.errors)}")
    print("observations by type:", dict(by_type))
    print(f"total observations: {len(store.observations)}  coverage entries: {len(store.coverage)}")
    print("status counts:", dict(Counter(o.status for o in store.observations)))
    print("categories:", dict(Counter(o.category for o in store.observations)))
    for fid, msg in store.errors.items():
        print("ERROR", fid, msg)
    for fid, items in store.degraded.items():
        print(f"DEGRADED {fid}: {len(items)} item(s) skipped; " + "; ".join(f"{d.location}: {d.category}" for d in items[:5]))
    for fid, ext, note in zero:
        print("EMPTY", fid, ext, note)
    return 1 if store.errors else 0
