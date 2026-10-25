"""Write code/cache/vision/<file_id>.json from a transcription of each screenshot.

The transcriptions below were made by reading the PNGs with the Claude Code session model
(claude-sonnet-5-5); no API call was involved. Only facts that are on screen are recorded:
the resource the page is about, whether it exists, and the identifiers it shows.
"""

import json
import sys
from pathlib import Path
from typing import Any

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
BY = "claude-sonnet-5-5 (Claude Code session, screenshot read directly; no API call)"

files: dict[str, dict[str, Any]] = {
    "evidence_04": {
        "page": "Customer Operations > Customers: Haruka Sato, user_11 . account_5762",
        "rows": [{
            "system_id": "account_service", "resource_id": "account_5762", "category": "USER_PROFILE",
            "status": "PRESENT",
            "identifiers": ["user_11", "haruka.s1838@zoho.com", "+81 90 7610 2507"],
            "pii_fields": {"name": "Haruka Sato"},
        }],
        "ignored": ["support_9538 / CS-8538 appears only as a ticket reference in the side panel"],
    },
    "evidence_09": {
        "page": "Search Console, query email:g.silva76@gmail.com, customer_directory_v3, 2 hits",
        "rows": [
            {"system_id": "search_service", "resource_id": "search_428", "category": "SEARCH_INDEX_ENTRY",
             "status": "PRESENT", "identifiers": ["g.silva76@gmail.com", "user_13"],
             "pii_fields": {"display_name": "Gabriel Silva"}},
            {"system_id": "search_service", "resource_id": "search_1874", "category": "SEARCH_INDEX_ENTRY",
             "status": "PRESENT", "identifiers": ["g.silva76.jr@gmail.com"],
             "notes": "look-alike address of another person; no customer reference"},
        ],
    },
    "evidence_14": {
        "page": "BridgeOps Connections, PRODUCTION eu-west, 07 Aug 2026 08:00 UTC, topology of 4 connectors",
        "rows": [
            {"system_id": "integration_service", "resource_id": "data_flow_1121", "category": "DATA_FLOW",
             "status": "ACTIVE", "identifiers": ["jain.aditi@gmail.com"],
             "notes": "Healthy, continuous sync to customer_analytics_warehouse, 1,284 events/hour"},
            {"system_id": "integration_service", "resource_id": "data_flow_9846", "category": "DATA_FLOW",
             "status": "ACTIVE", "identifiers": ["lucia-torres@gmail.com"],
             "notes": "continuous sync to customer_analytics_warehouse, 842 events/hour"},
        ],
        "ignored": ["Order notifications and Delivery tracking connectors carry no customer selector or resource id"],
    },
    "evidence_19": {
        "page": "Pulse Analytics > Users: Omar Darwish, user_20 . analytics_7438",
        "rows": [{
            "system_id": "analytics_platform", "resource_id": "analytics_7438", "category": "ANALYTICS_PROFILE",
            "status": "PRESENT", "identifiers": ["user_20", "darwish.omar@icloud.com"],
            "pii_fields": {"name": "Omar Darwish"},
        }],
    },
    "evidence_23": {
        "page": "Customer Download Center > Data exports, export_1260 selected, status Ready",
        "rows": [{
            "system_id": "export_service", "resource_id": "export_1260", "category": "USER_EXPORT",
            "status": "PRESENT", "identifiers": ["ingrid.dahl@outlook.com"],
            "notes": "customer_export_1260.zip, created 18 Jun 2026 06:42 UTC, expires 25 Jun, download count 0",
        }],
        "ignored": ["export_1261, export_1259, export_1258 belong to northstar.shop staff addresses"],
    },
    "evidence_28": {
        "page": "Helpline Console: CS-5034 Where is my delivery?, support_1034, customer Kofi Agyeman",
        "rows": [{
            "system_id": "support_platform", "resource_id": "support_1034", "category": "SUPPORT_MESSAGE",
            "status": "PRESENT", "identifiers": ["kagyeman8947@fastmail.com"],
            "pii_fields": {"name": "Kofi Agyeman", "customer": "Kofi Agyeman"},
            "notes": "conversation also carries order ORD-20265034, agent Maya Chen and carrier context",
        }],
    },
    "evidence_32": {
        "page": "Vault Recovery Manager > Archives, backup_9287 selected, account areed1433@gmail.com",
        "rows": [{
            "system_id": "backup_service", "resource_id": "backup_9287", "category": "BACKUP_ARCHIVE",
            "status": "RESTORABLE", "identifiers": ["areed1433@gmail.com"],
            "notes": ("archive present, eu-west-1, retention end 26 Jul 2026; the console shows no restore block, "
                      "and the activity stream shows a restore job from this archive. Restorability is inferred, not displayed."),
        }],
        "ignored": ["backup_9291, backup_9288, backup_9286 belong to company.com / outlook.com accounts of other parties"],
    },
    "evidence_37": {
        "page": "Sentinel > Account events, subject filter user_03, 18-26 Aug 2026, 6 signed events",
        "rows": [{
            "system_id": "security_service", "resource_id": "audit_record_003", "category": "SECURITY_AUDIT_LOG",
            "status": "PRESENT", "identifiers": ["user_03"],
            "notes": "account_closure_recorded, 25 Aug 2026 04:03 UTC, actor automated account workflow, integrity Valid",
        }],
        "ignored": ["audit_record_001, 002, 004, 005, 006 are routine events (login, session, password, email change) shown in the list"],
    },
    "evidence_42": {
        "page": "Customer Reference Registry > Customer references: user_17, Retained reference",
        "rows": [{
            "system_id": "identity_service", "resource_id": "tombstone_17", "category": "ACCOUNT_TOMBSTONE",
            "status": "PRESENT", "identifiers": ["user_17"],
            "notes": "customer key only; linked orders/support/finance references; no name, email, phone or hash shown",
        }],
    },
    "evidence_46": {
        "page": "Nimbus Object Storage > northstar-customer-data, eu-west-1/support-attachments/2026/02, 8 objects",
        "rows": [{
            "system_id": "object_storage", "resource_id": "object_19", "category": "SUPPORT_ATTACHMENT",
            "status": "PRESENT", "identifiers": [],
            "notes": "delivery_confirmation_19.pdf, 684 KB, v3, uploader support-service; no customer identity on screen",
        }],
    },
    "evidence_60": {
        "page": "Audience Operations > Audience Operations: Soo-jin Park, marketing_001",
        "rows": [{
            "system_id": "marketing_platform", "resource_id": "marketing_001", "category": "MARKETING_PROFILE",
            "status": "PRESENT", "identifiers": ["sj.park@naver.com", "+82 10-5837-9451"],
            "pii_fields": {"name": "Soo-jin Park"},
            "identity_mismatch": True,
            "notes": ("Live profile, email opt-in yes. The person on screen is not any known user, while the data "
                      "catalog (activity_4008) links marketing_001 to kwame-mensah@proton.me / user_09. The link is "
                      "used; the discrepancy is reported."),
        }],
    },
}

for fid, body in files.items():
    body = {"file_id": fid, "extracted_by": BY, **body}
    (out / f"{fid}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {len(files)} files to {out}")
