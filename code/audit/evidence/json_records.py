"""JSON evidence: snapshot bundles, service exports, flat record lists, single collections.

Invalid or truncated JSON is a file-level failure (the record boundaries cannot be trusted). Inside valid JSON a record
that is not an object, has no resource_id or cannot be turned into an observation is skipped and reported with its
position; its neighbours are kept. Nothing is filled in for a skipped record.
"""

from datetime import datetime
from pathlib import Path
import json

from ..diagnostics import ITEM, Diagnostic, Salvage, UnparseableFile, short
from ..loaders import extract_identifiers, parse_ts
from ..model import ABSENT, PRESENT, Coverage, Observation
from ..shapes import JsonObject, JsonValue
from .common import RESULT_STATUS, record_observation, system_for


def parse(evidence_id: str, path: Path, manifest_time: datetime) -> tuple:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UnparseableFile("invalid_json", f"{short(exc)} (a truncated or damaged file; record boundaries cannot be trusted)")
    if not isinstance(data, dict):
        raise UnparseableFile("unrecognised_layout", f"top level is {type(data).__name__}, expected an object")
    skipped: list = []
    if "snapshots" in data:
        result = _bundle(evidence_id, data, skipped)
    elif "service_exports" in data:
        result = _service_exports(evidence_id, data, manifest_time, skipped)
    elif "collection" in data:
        result = _single_collection(evidence_id, data, manifest_time, skipped)
    elif "records" in data:
        result = _flat_records(evidence_id, data, manifest_time, skipped)
    else:
        raise UnparseableFile("unrecognised_layout", f"unrecognised JSON layout in {path.name}")
    return (*result, Salvage(skipped)) if skipped else result


def _skip(skipped: list, evidence_id: str, where: str, category: str, message: str) -> None:
    skipped.append(Diagnostic(evidence_id, "json", ITEM, where, category, message))


def _record_ok(evidence_id: str, rec: JsonValue, where: str, skipped: list, need_id: bool = True) -> bool:
    """A record must be an object and, unless it is a flat history row, name its resource."""
    if not isinstance(rec, dict):
        _skip(skipped, evidence_id, where, "not_an_object", f"record is {type(rec).__name__}")
        return False
    if need_id and not rec.get("resource_id"):
        _skip(skipped, evidence_id, where, "missing_resource_id", "record has no resource_id")
        return False
    return True


def _bundle(evidence_id: str, data: JsonObject, skipped: list) -> tuple:
    obs, cov = [], []
    for i, snap in enumerate(data["snapshots"]):
        where = f"snapshots[{i}]"
        try:
            when = parse_ts(snap["captured_at"])
            system, collection, records = snap["data_store"], snap["collection"], snap["records"]
            if when is None or not isinstance(records, list):
                raise ValueError("captured_at is not a timestamp or records is not a list")
        except (KeyError, TypeError, ValueError) as exc:
            _skip(skipped, evidence_id, where, "bad_snapshot", short(exc))
            continue
        ids = set()
        for j, rec in enumerate(records):
            rwhere = f"{where}.records[{j}]"
            history = isinstance(rec, dict) and "recorded_at" in rec and "record_type" in rec
            if not _record_ok(evidence_id, rec, rwhere, skipped, need_id=not history):
                continue
            try:
                if history:
                    obs.append(_flat(evidence_id, rec, when))  # operational_record_history rows
                    continue
                obs.append(record_observation(evidence_id, when, rec, system=system, collection=collection,
                                              source="json.bundle"))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                _skip(skipped, evidence_id, rwhere, "bad_record", short(exc))
                continue
            ids.add(rec["resource_id"])
        if not records or ids:
            cov.append(Coverage(evidence_id, when, system, collection, frozenset(ids)))
    return obs, cov


def _service_exports(evidence_id: str, data: JsonObject, manifest_time: datetime, skipped: list) -> tuple:
    obs = []
    for system, collections in data["service_exports"].items():
        for collection, entries in collections.items():
            for k, e in enumerate(entries):
                where = f"service_exports.{system}.{collection}[{k}]"
                if not _record_ok(evidence_id, e, where, skipped):
                    continue
                try:
                    obs.append(_export_observation(evidence_id, system, collection, e, manifest_time))
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    _skip(skipped, evidence_id, where, "bad_record", short(exc))
    return obs, []


def _export_observation(evidence_id: str, system: str, collection: str, e: dict, manifest_time: datetime) -> Observation:
    when = parse_ts(e.get("observed_at")) or manifest_time
    rec = dict(e)
    link = set()
    if "query" in rec:
        link |= extract_identifiers({"q": rec["query"]})
    status = None
    if rec.get("status_code") == 404 or ("profile" in rec and rec["profile"] is None):
        status = ABSENT
    if isinstance(rec.get("profile"), dict):
        rec = {**rec.pop("profile"), **rec}
    hits = rec.get("hits")
    if isinstance(hits, dict):
        docs = [d for d in hits.get("documents", []) if d.get("resource_id") == rec["resource_id"]]
        status = PRESENT if docs else ABSENT
        if docs:
            rec = {**docs[0], **{k: v for k, v in rec.items() if k != "hits"}}
    return record_observation(evidence_id, when, rec, system=system, collection=collection,
                              status=status, link_keys=link, source="json.service_export")


def _flat(evidence_id: str, rec: JsonObject, default_time: datetime) -> Observation:
    when = parse_ts(rec.get("recorded_at")) or default_time
    result = str(rec.get("result") or "").upper()
    link = extract_identifiers({k: rec.get(k) for k in ("subject_reference", "lookup_value")})
    clean = {k: v for k, v in rec.items() if k not in ("subject_reference", "lookup_value")}
    obs = record_observation(evidence_id, when, clean, system=rec.get("system"),
                             record_type=rec.get("record_type"), status=RESULT_STATUS.get(result, PRESENT),
                             link_keys=link, source="json.flat",
                             extra_attrs={"result": result})
    # The reference identifies the record; for IDENTIFIABLE/FOUND it is exposed in it.
    if result in ("IDENTIFIABLE", "FOUND", "ACTIVE"):
        obs.identifiers |= link
    return obs


def _flat_records(evidence_id: str, data: JsonObject, manifest_time: datetime, skipped: list) -> tuple:
    obs = []
    for k, r in enumerate(data["records"]):
        if not _record_ok(evidence_id, r, f"records[{k}]", skipped):
            continue
        try:
            obs.append(_flat(evidence_id, r, manifest_time))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            _skip(skipped, evidence_id, f"records[{k}]", "bad_record", short(exc))
    return obs, []


def _single_collection(evidence_id: str, data: JsonObject, manifest_time: datetime, skipped: list) -> tuple:
    when = parse_ts(data.get("captured_at")) or manifest_time
    collection = data["collection"]
    obs, ids, system = [], set(), None
    for k, rec in enumerate(data["records"]):
        if not _record_ok(evidence_id, rec, f"records[{k}]", skipped):
            continue
        try:
            obs.append(record_observation(evidence_id, when, rec, collection=collection, source="json.collection"))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            _skip(skipped, evidence_id, f"records[{k}]", "bad_record", short(exc))
            continue
        system = system_for(rec["resource_id"])
        ids.add(rec["resource_id"])
    cov = [Coverage(evidence_id, when, system, collection, frozenset(ids))] if system else []
    return obs, cov
