"""Identifier index valid as of a point in time.

Combines the current snapshot (user_identities.csv) with every historical value in
user_audit_log.csv. An identifier only counts for a cutoff if it already existed then:
an old value always existed; a new value exists from its change event onward.
"""

from datetime import datetime
from collections import defaultdict

from .loaders import norm_email, norm_phone, norm_user, parse_ts, read_csv

_FIELD_NORMALISERS = {
    "email": norm_email,
    "phone": norm_phone,
    "email_hash": lambda v: v.strip().lower(),
}


class IdentityIndex:
    def __init__(self) -> None:
        # user_id -> list of (kind, value, valid_from or None)
        self._ids: defaultdict[str, list[tuple[str, str, datetime | None, str]]] = defaultdict(list)
        # value -> set(user_id)
        self._owners: defaultdict[str, set[str]] = defaultdict(set)
        self.users: dict[str, dict[str, str]] = {}
        self.audit_events: defaultdict[str, list[dict[str, str]]] = defaultdict(list)  # user_id -> [audit rows]

        for row in read_csv("user_identities.csv"):
            uid = norm_user(row["user_id"]) or row["user_id"]
            self.users[uid] = row

        later_values: set[tuple[str, str, str]] = set()  # (uid, field, value) introduced by a change event
        for row in read_csv("user_audit_log.csv"):
            uid = norm_user(row["user_id"]) or row["user_id"]
            field = row["field_name"]
            self.audit_events[uid].append(row)
            norm = _FIELD_NORMALISERS.get(field)
            if not norm:
                continue
            when = parse_ts(row["event_time"])
            if row["old_value"]:
                self._add(uid, field, norm(row["old_value"]), None, f"{row['audit_id']} (previous value)")
            if row["new_value"]:
                v = norm(row["new_value"])
                self._add(uid, field, v, when, row["audit_id"])
                later_values.add((uid, field, v))

        for uid, row in self.users.items():
            self._add(uid, "user", uid, None, "user_identities.csv")
            for field, norm in _FIELD_NORMALISERS.items():
                if row.get(field):
                    v = norm(row[field])
                    if (uid, field, v) not in later_values:
                        self._add(uid, field, v, None, "user_identities.csv")

    def _add(self, uid: str, kind: str, value: str, valid_from: datetime | None, source: str = "") -> None:
        self._ids[uid].append((kind, value, valid_from, source))
        self._owners[value].add(uid)

    def identifier_facts(self, uid: str, as_of: datetime | None = None) -> list[tuple]:
        """[(kind, value, valid_from, source)] for the provenance graph: where each identifier of the user comes from."""
        return [(k, v, t, src) for k, v, t, src in self._ids.get(uid, []) if as_of is None or t is None or t <= as_of]

    def identifiers_for(self, uid: str, as_of: datetime | None = None) -> set[str]:
        """All identifiers the user had at or before `as_of` (all of them if None)."""
        return {
            value
            for kind, value, valid_from, _source in self._ids.get(uid, [])
            if as_of is None or valid_from is None or valid_from <= as_of
        }

    def owners(self, value: str) -> set[str]:
        return set(self._owners.get(value, ()))

    def resolve_email(self, email: str, as_of: datetime | None = None) -> tuple[str | None, list[str]]:
        """Return (user_id or None, candidates). Exact, case-insensitive match only."""
        e = norm_email(email)
        candidates = sorted(u for u in self.owners(e) if e in self.identifiers_for(u, as_of))
        return (candidates[0] if len(candidates) == 1 else None), candidates
