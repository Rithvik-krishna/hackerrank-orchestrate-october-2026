"""Helpers shared by the evidence parsers: system/category vocabularies and record -> Observation."""

from collections.abc import Iterable
from datetime import datetime
import re

from ..loaders import extract_identifiers, is_nullish
from ..model import ABSENT, ACTIVE, BLOCKED, DISABLED, PRESENT, RESTORABLE, UNKNOWN, Observation
from ..shapes import JsonObject, JsonValue

# Fields that carry direct identifiers; whether each is null decides redaction/anonymisation.
PII_FIELDS = {
    "name", "full_name", "display_name", "requester_name",
    "email", "owner_email", "requester_email", "customer_email", "uploaded_by", "lookup_value",
    "phone", "owner_phone", "requester_phone",
    "email_hash",
    "user_id", "customer_reference", "subject_reference", "subject_selector",
}

_PREFIX_SYSTEM = [
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("data_flow_", "integration_service"),
    ("backup_", "backup_service"),
    ("audit_record_", "security_service"),
    ("tombstone_", "identity_service"),
    ("legal_hold_", "legal_service"),
    ("marketing_", "marketing_platform"),
    ("email_delivery_", "email_delivery_service"),
    ("cache_", "cache_service"),
    ("object_", "object_storage"),
]

# collection / table name -> deletion_rules.csv category
COLLECTION_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "profile_lookups": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents": "SEARCH_INDEX_ENTRY",
    "index_queries": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "profiles": "ANALYTICS_PROFILE",  # analytics_platform.profiles in service exports
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "tickets": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "connectors": "DATA_FLOW",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "audit_records": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "retained_order_records": "LEGAL_HOLD_RECORD",
}

PREFIX_CATEGORY = {
    "account_": "USER_PROFILE",
    "search_": "SEARCH_INDEX_ENTRY",
    "analytics_": "ANALYTICS_PROFILE",
    "export_": "USER_EXPORT",
    "data_flow_": "DATA_FLOW",
    "backup_": "BACKUP_ARCHIVE",
    "audit_record_": "SECURITY_AUDIT_LOG",
    "tombstone_": "ACCOUNT_TOMBSTONE",
    "legal_hold_": "LEGAL_HOLD_RECORD",
    "marketing_": "MARKETING_PROFILE",
    "email_delivery_": "EMAIL_DELIVERY_LOG",
    "cache_": "CACHE_ENTRY",
    "object_": "SUPPORT_ATTACHMENT",
}

# explicit result/state words used by flat records and the handover text
RESULT_STATUS = {
    "FOUND": PRESENT, "PRESENT": PRESENT, "IDENTIFIABLE": PRESENT,
    "REDACTED": PRESENT, "ANONYMIZED": PRESENT,  # record exists; identifiers decide treatment
    "NOT_FOUND": ABSENT, "ABSENT": ABSENT, "DELETED": ABSENT,
    "ACTIVE": ACTIVE, "ENABLED": ACTIVE, "DISABLED": DISABLED,
    "RESTORABLE": RESTORABLE, "BLOCKED": BLOCKED,
}

RESOURCE_ID_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"legal_hold|marketing|email_delivery|cache|object)_\d+\b"
)


def system_for(resource_id: str) -> str:
    for prefix, system in _PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return "unknown"


def category_for(resource_id: str, collection: str | None = None, record_type: str | None = None) -> str | None:
    if record_type:
        return record_type
    if collection:
        base = re.sub(r"_(primary|replica.*|.*_replicas?)$", "", collection)
        if base in COLLECTION_CATEGORY:
            return COLLECTION_CATEGORY[base]
    for prefix, cat in PREFIX_CATEGORY.items():
        if resource_id.startswith(prefix):
            return cat
    if resource_id.startswith("support_"):
        return "SUPPORT_MESSAGE"
    return None


def to_bool(v: JsonValue) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def pii_fields(record: JsonObject) -> JsonObject:
    """{field: value-or-None} for every direct-identifier field the record exposes, flattened one level."""
    out = {}
    for k, v in record.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if k2 in PII_FIELDS:
                    out[k2] = None if is_nullish(v2) else v2
        elif k in PII_FIELDS:
            out[k] = None if is_nullish(v) else v
    return out


def status_from_record(record: JsonObject, default: str = PRESENT) -> str:
    """Derive a status from flow/backup flags; otherwise the record simply exists."""
    enabled = to_bool(record.get("enabled"))
    if enabled is not None:
        return ACTIVE if enabled else DISABLED
    restore = to_bool(record.get("restore_allowed"))
    if restore is not None:
        return RESTORABLE if restore else BLOCKED
    acc = str(record.get("account_status") or "").upper()
    if acc == "DELETED" and record.get("resource_id", "").startswith("account_"):
        return ABSENT
    return default


def record_observation(evidence_id: str, observed_at: datetime, record: JsonObject, *, system: str | None = None, collection: str | None = None, record_type: str | None = None, status: str | None = None, link_keys: Iterable[str] = (), source: str = "", extra_attrs: JsonObject | None = None) -> Observation:
    rid = record["resource_id"]
    attrs = {"pii_fields": pii_fields(record)}
    for key in ("retention_until", "retained_until", "source_resource_id", "connector_name",
                "account_status", "download_available", "expires_at", "event_code", "event_name"):
        if key in record and not is_nullish(record[key]):
            attrs[key] = record[key]
    if extra_attrs:
        attrs.update(extra_attrs)
    return Observation(
        evidence_id=evidence_id,
        observed_at=observed_at,
        system_id=system or record.get("system") or record.get("origin_system") or system_for(rid),
        resource_id=rid,
        status=status or status_from_record(record),
        category=category_for(rid, collection, record_type),
        identifiers=extract_identifiers(record),
        link_keys={k for k in link_keys if k},
        attrs=attrs,
        source=source,
    )


__all__ = [
    "ABSENT", "ACTIVE", "BLOCKED", "DISABLED", "PRESENT", "RESTORABLE", "UNKNOWN",
    "RESOURCE_ID_RE", "RESULT_STATUS", "category_for", "pii_fields", "record_observation",
    "status_from_record", "system_for", "to_bool",
]
