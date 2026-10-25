"""Decide whether a request authorises deletion.

Rules first (multilingual deletion cues, explicit negations, non-deletion intents).
Only text the rules cannot decide is returned as UNSURE for an optional model call.
CLASSIFY_REQUEST activity rows are recorded as corroboration but never override the text.
"""

from collections.abc import Callable
import re

DELETE, NOT_DELETE, UNSURE = "DELETE", "NOT_DELETE", "UNSURE"

# A negated or scoped request beats any deletion keyword it contains
# ("I am not asking for deletion", "remove me from marketing lists only").
_NEGATIONS = [
    r"\bnot\s+(?:asking|requesting)\s+(?:for\s+)?(?:deletion|erasure|to\s+(?:delete|erase|close))",
    r"\bnot\s+a\s+request\s+to\s+(?:delete|erase|remove|close)",
    r"\b(?:don'?t|do\s+not|please\s+don'?t)\s+(?:delete|erase|close|remove|change)",
    r"\bwithout\s+(?:removing|deleting|closing)",
    r"\bleave\s+(?:the|my)\s+(?:account|profile)",
    r"\bkeep\s+the\s+account",
    r"\bmarketing\s+(?:lists?|emails?)?\s*only\b",
    r"\bcontinue\s+using\b",
    r"\bi\s+still\s+(?:use|need)\b",
]

_DELETION = [
    r"\bdelet(?:e|ed|ing|ion)\b",
    r"\berase[ds]?\b|\berasure\b",
    r"\bremov(?:e|ed|al)\b",
    r"\bclose\s+(?:my|this|the)\s+account|\baccount\s+closed|\bclosed\s+my\s+account",
    r"\bprofile\s+is\s+gone\b",
    r"\beliminaci[oó]n\b|\beliminar\b",          # es
    r"\bexclus[aã]o\b|\bexcluir\b",               # pt
    r"\bsuppression\b|\bsupprimer\b",             # fr
    "削除",                                        # ja
    "삭제",                                        # ko
]

_NON_DELETION = [
    r"\b(?:like|want|need)\s+to\s+download\b",
    r"\bsend\s+me\s+a\s+copy\b|\bneed\s+a\s+copy\b",
    r"\bwhat\s+personal\s+information\b",
    r"\bchange\s+the\s+email\b|\bcorrect\s+it\b|\bmisspelled\b|\bout\s+of\s+date\b",
    r"\breset(?:ting)?\s+the\s+login\b",
    r"\bstop\s+the\s+promotional\b|\bmarketing\b",
]


def _any(patterns: list[str], text: str) -> list[str]:
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


def classify(text: str) -> tuple[str, str]:
    """Return (label, reason)."""
    t = " ".join((text or "").split())
    neg = _any(_NEGATIONS, t)
    if neg:
        return NOT_DELETE, f"negation:{neg[0]}"
    dele = _any(_DELETION, t)
    non = _any(_NON_DELETION, t)
    if dele and not non:
        return DELETE, f"deletion:{dele[0]}"
    if non and not dele:
        return NOT_DELETE, f"non_deletion:{non[0]}"
    if dele and non:
        return UNSURE, "both deletion and non-deletion cues"
    return UNSURE, "no cue"


def decide(text: str, llm_fallback: Callable[[str], tuple[str, str]] | None = None) -> tuple[bool, str, str]:
    """Rules, then the optional model for UNSURE text. Returns (is_deletion, label, reason)."""
    label, reason = classify(text)
    if label == UNSURE and llm_fallback is not None:
        label, reason = llm_fallback(text)
    if label == UNSURE:
        # Every request arrives through the deletion intake; without a contrary cue,
        # treat it as a deletion request and record the uncertainty.
        return True, label, reason
    return label == DELETE, label, reason
