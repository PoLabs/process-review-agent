"""Guardrails: PHI/PII redaction and a deterministic prompt-injection screen.

CONTRACT (implemented by a subagent; signatures are fixed — tracing depends on them):

    redact(value)            -> same shape as value, every str passed through redact_text
    redact_text(text: str)   -> str with identifiers replaced by [EMAIL] [PHONE] [SSN] [DATE]
                                [MRN] [ADDRESS] [ZIP] [NAME] placeholders
    register_known_identifiers(values: Iterable[str]) -> None
                             exact-match redaction for identifiers the caller already knows
                             (eval cases register their synthetic PII so redaction is exact)
    clear_known_identifiers() -> None
    screen_injection(text: str) -> InjectionScreen(flagged: bool, signals: list[str])

Regex redaction is a backstop, not a de-identification service. Flows that handle patient
data also run traces in content mode "none" (see kit.tracing).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel


class InjectionScreen(BaseModel):
    flagged: bool
    signals: list[str]


# --- Known identifiers -------------------------------------------------------------------------

_known: set[str] = set()
_known_lock = threading.Lock()
_MIN_KNOWN_LEN = 3


def register_known_identifiers(values: Iterable[str]) -> None:
    with _known_lock:
        _known.update(v for v in values if isinstance(v, str) and len(v) >= _MIN_KNOWN_LEN)


def clear_known_identifiers() -> None:
    with _known_lock:
        _known.clear()


def _redact_known(text: str) -> str:
    with _known_lock:
        # Longest first, so "Marisol Quentin-Abernathy" wins over "Marisol Quentin".
        ordered = sorted(_known, key=len, reverse=True)
    if not ordered:
        return text
    pattern = re.compile("|".join(re.escape(v) for v in ordered), re.IGNORECASE)
    return pattern.sub("[PII]", text)


# --- Pattern table -----------------------------------------------------------------------------

_WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_STREET_SUFFIX = (
    "St|Street|Ave|Avenue|Rd|Road|Blvd|Ln|Lane|Dr|Drive|Ct|Court|Way|Pl|Place|Ter|Terrace|Hollow|Pkwy"
)
_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY"
    "|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
# A capitalized name word: 2+ letters (so a stray "I" is not swallowed) and not a weekday/month.
_NAME_WORD = rf"(?!(?:{_WEEKDAYS}|{_MONTH})\b)[A-Z][A-Za-z'\u2019-]+"
_NAME = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}"
_STREET_WORD = r"(?:[A-Z][A-Za-z.'-]*|\d+(?:st|nd|rd|th))"
_UNIT = r"(?:,?\s+(?:Apt|Apartment|Unit|Suite|Ste)\.?\s+[A-Za-z0-9-]+|,?\s+#\s?[A-Za-z0-9-]+)?"

# (placeholder, compiled pattern, replacement). Applied in order. Where a label or lead-in is kept
# (MRN:, "my name is", Dr.), group 1 captures it and the replacement re-emits it.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "EMAIL",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
        "[EMAIL]",
    ),
    # SSN and MRN before PHONE: a 10-digit MRN or dashed SSN must not be read as a phone number.
    ("SSN", re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])"), "[SSN]"),
    ("MRN", re.compile(r"\bSYN-\d+\b"), "[MRN]"),
    (
        "MRN",
        re.compile(r"\b(MRN\s*[:#]?\s*)(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{3,}", re.IGNORECASE),
        r"\1[MRN]",
    ),
    (
        "PHONE",
        re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?![\w-])"),
        "[PHONE]",
    ),
    ("DATE", re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "[DATE]"),
    ("DATE", re.compile(r"\b\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\b"), "[DATE]"),
    (
        "DATE",
        re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.IGNORECASE),
        "[DATE]",
    ),
    (
        "DATE",
        re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}}\b", re.IGNORECASE),
        "[DATE]",
    ),
    (
        "ADDRESS",
        re.compile(rf"\b\d{{1,6}}\s+(?:{_STREET_WORD}\s+){{1,4}}?(?i:{_STREET_SUFFIX})\b{_UNIT}"),
        "[ADDRESS]",
    ),
    (
        "ZIP",
        re.compile(rf"\b((?:{_STATES}|(?i:zip(?:\s?code)?))[\s:,]*)\d{{5}}(?:-?\d{{4}})?\b"),
        r"\1[ZIP]",
    ),
    ("NAME", re.compile(rf"(\b(?:Mr|Mrs|Ms|Mx|Miss|Dr)\b\.?\s+){_NAME}"), r"\1[NAME]"),
    (
        "NAME",
        re.compile(rf"(\b(?i:my name is\s+|this is\s+|patient(?: name)?:\s*)){_NAME}"),
        r"\1[NAME]",
    ),
]


def redact_text(text: str) -> str:
    """Replace known identifiers, then pattern-matched identifiers, with placeholders."""
    text = _redact_known(text)
    for _name, pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_patterns(text: str) -> tuple[str, list[str]]:
    """Pattern-only redaction, ignoring known identifiers, plus the placeholder types it applied.

    For flows that must find identifiers on their own: the eval harness registers each case's
    synthetic PII as known identifiers, so a flow redacting with `redact_text` would be graded
    against its own answer key.
    """
    found: set[str] = set()
    for name, pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        if n:
            found.add(name)
    return text, sorted(found)


def redact(value: Any) -> Any:
    """Recursively redact strings in dicts (values only), lists and tuples; other scalars pass."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    return value


# --- Injection screen --------------------------------------------------------------------------

_INJECTION_SIGNALS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "override_instructions": (
            r"\b(?:ignore|disregard|forget)\s+(?:(?:all|any|the|your|previous|prior|above|earlier)\s+){1,3}"
            r"(?:instructions|rules|prompts|guidelines)\b"
        ),
        "role_hijack": (
            r"\byou are now\b|\bact as (?:an? |the )?(?:system|developer|admin|administrator)\b"
            r"|\bdeveloper mode\b|\bjailbreak"
        ),
        "system_prompt_probe": (
            r"\bsystem prompt\b|\byour instructions\b|\breveal your (?:prompt|instructions|rules)\b"
        ),
        "fake_markup": r"</?(?:system|assistant|tool)>|\[/?INST\]|<\|im_start\|>",
        "tool_coercion": r"\bcall the \w+ tool\b|\buse the \w+ tool to\b",
        "encoded_payload": r"[A-Za-z0-9+/=]{40,}",
    }.items()
}


def screen_injection(text: str) -> InjectionScreen:
    """Deterministic signal screen. Flags for review; it does not prove or disprove an attack."""
    signals = [name for name, pattern in _INJECTION_SIGNALS.items() if pattern.search(text)]
    return InjectionScreen(flagged=bool(signals), signals=signals)
