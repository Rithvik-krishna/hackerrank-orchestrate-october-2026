# Live validation notes (measured by hand on 26 Oct 2026)

`live_validation.md` is rewritten by `python code/tools/live_check.py` on every run and records its own scratch results.
This file records what was measured around it, once, with the same provider and model: Gemini free tier,
`gemini-3.1-flash-lite` for intent and vision. Nothing here changes `output.csv`; every run below used scratch caches or a
throwaway copy of the repository.

## Intent arbitration (classification of request text)
- 5 hand-written texts, 5 of 5 answered. The four with a clear reading were classified as a careful reader would (a deletion
  with a "copy first" clause: DELETE; "Do NOT delete anything": NOT_DELETE; a prompt-injection sentence ahead of a real erasure
  request: DELETE, the injected instruction ignored; a Spanish negation: NOT_DELETE). The fifth ("I would rather my details were
  not kept by you any more") is genuinely ambiguous and was read as DELETE.
- 21 real requests (all the rules call NOT_DELETE plus the first deletion requests): 19 answered, 2 failed on HTTP 429, and all
  19 agreed with the rules. The run is a sanity check of the contract and the guardrails, not an accuracy measurement: the
  rules call no request UNSURE on this dataset, so arbitration would never be consulted in the normal run.

## API vision extraction (screenshots into rows)
Two problems showed up the first time the route met a real model, and both are fixed in the code:
1. The model gave correct resource ids and categories, but the on-screen `quote` it attached did not contain the id (it quoted an
   email or "subject user_03"), so the deterministic guardrail dropped 7 of 10 screenshots to zero rows. The guardrail is now also a
   validator inside the call (`call_tool(check=quote_problems)`): its problems go back to the model as the tool's error and the
   model corrects itself. After the change the same screenshots came back with all rows accepted.
2. On the security-event page the model listed all six events; only the account-closure event is the retained audit record (the
   prompt now says so). Left as it was, case_45 would have gained five spurious `VERIFY_RECORD_PRESENT` steps.

Final run in a throwaway copy of the repository (every screenshot read by the API, committed readings replaced):
- 9 of 11 screenshots read; 7 are row-for-row identical to the committed readings. The other readings differ in three places:
  `evidence_04` and `evidence_23` list extra records of the same kind (`support_9538`; `export_1258`, `export_1259`,
  `export_1261`), and `evidence_32` shows `backup_9287` as PRESENT where the committed reading says RESTORABLE.
- 2 screenshots could not be read: `evidence_42` and `evidence_46` list order numbers, ticket numbers and file names, not resource
  ids, and the model either reported those (rejected by the id pattern, three tries) or reported nothing. The committed readings
  (`tombstone_17`, `object_19`) come from the page headers.
- Downstream effect, end to end: the 32 labelled samples score exactly as before (status 1.000, exact rows 0.844, step F1 0.982).
  Of the 518 answers exactly one changes, `case_55`, which loses the three restore-risk steps for `backup_9287`. The cause is the
  documented inference in the committed reading: the console shows no restore block and the activity stream shows a restore job
  from that archive, so the committed reading calls it RESTORABLE, while the model, told not to guess, says PRESENT. The committed
  reading stays in force; the choice is recorded here rather than hidden.

## Limits of this evidence
One provider, one small model, free-tier quota with several HTTP 429 failures in every run, one run per configuration. It shows
that the two routes work end to end and that the guardrails do their job; it does not show the model is accurate on unseen
screenshots or request texts. The earlier link-adjudication and investigator runs (no accuracy effect) are in
`evaluation_report.md`.
