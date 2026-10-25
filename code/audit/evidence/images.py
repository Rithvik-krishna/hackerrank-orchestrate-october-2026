"""Screenshot evidence (PNG). Facts come from an extraction cached per file.

code/cache/vision/<file_id>.json holds, for each screenshot, the resource the page is about:
    {"rows": [{"system_id", "resource_id", "category", "status", "identifiers": [...],
               "pii_fields": {...}, "notes", "identity_mismatch"}]}
The cache is produced once (code/tools/transcribe_screenshots.py documents how the committed
files were made; code/tools/vision_extract.py is the API route). Without a cache file an
image contributes nothing and is reported as pending.

Identifiers shown on screen link a resource to a subject like any other record. A row flagged
identity_mismatch shows a person who is not the one the data catalog links the resource to; its
on-screen identifiers are kept for exposure checks but never used to link.
"""

from datetime import datetime
from pathlib import Path
import json

from .. import paths
from ..loaders import extract_identifiers, parse_ts
from ..model import Observation
from .common import RESULT_STATUS, category_for, system_for


def parse(evidence_id: str, path: Path, manifest_time: datetime) -> tuple:
    cache = paths.VISION_CACHE / f"{evidence_id}.json"
    if not cache.exists():
        return [], [], "pending"
    obs = []
    for row in json.loads(cache.read_text(encoding="utf-8")).get("rows", []):
        rid = row["resource_id"]
        ids = extract_identifiers({"v": row.get("identifiers", [])})
        mismatch = bool(row.get("identity_mismatch"))
        obs.append(Observation(
            evidence_id=evidence_id,
            observed_at=parse_ts(row.get("timestamp")) or manifest_time,
            system_id=row.get("system_id") or system_for(rid),
            resource_id=rid,
            status=RESULT_STATUS.get(str(row.get("status", "")).upper(), "PRESENT"),
            category=row.get("category") or category_for(rid),
            identifiers=ids,
            link_keys=set() if mismatch else ids,
            attrs={"pii_fields": row.get("pii_fields", {}), "notes": row.get("notes", ""),
                   "identity_mismatch": mismatch},
            source="image",
        ))
    return obs, [], "cached"
