# Row-level error analysis

32 labelled cases; 27 identical to the gold as data, 5 differ (row exact 0.844); 193 gold steps, 190 predicted.
rows whose steps_json text is also byte-for-byte the gold's: 20 of 32 (the others differ only in key order or whitespace, which the grader ignores)

| category | differences | cases affected |
|---|---|---|
| status | 0 | 0 |
| missing_step | 3 | 3 |
| extra_step | 0 | 0 |
| resource_substitution | 2 | 2 |
| system_mismatch | 0 | 0 |
| evidence_mismatch | 0 | 0 |
| field_mismatch | 0 | 0 |
| renumbering_cascade | 5 | 3 |
| ordering | 0 | 0 |
| serialization | 7 | 7 |

Per case:

- case_04: renumbering_cascade (VERIFY_RECORD_ANONYMIZED analytics_901: step_no 1 instead of 2); missing_step (VERIFY_RECORD_ABSENT account_service account_004)
- case_18: renumbering_cascade (VERIFY_RECORD_REDACTED support_5569: step_no 11 instead of 12); renumbering_cascade (VERIFY_RECORD_ANONYMIZED analytics_037: step_no 12 instead of 13); renumbering_cascade (VERIFY_DATA_FLOW_DISABLED data_flow_4890: step_no 13 instead of 14); missing_step (VERIFY_RECORD_PRESENT security_service audit_record_6645)
- case_20: renumbering_cascade (VERIFY_DATA_FLOW_DISABLED data_flow_1770: step_no 7 instead of 8); missing_step (VERIFY_RECORD_PRESENT security_service audit_record_5569)
- case_23: resource_substitution (VERIFY_RECORD_ABSENT account_service: account_7015 instead of account_410)
- case_32: resource_substitution (VERIFY_RECORD_ABSENT account_service: account_2381 instead of account_8858)
