"""PII detection and redaction (pure functions, no database).

Policy for this application:
  * Direct identifiers - name, date of birth, SSN, street address, phone, e-mail, sex -
    never leave the service layer. Internal API response models do not contain them
    (enforced by tests over PII_FIELD_NAMES), so agents, the LLM, workflow state, the
    audit log and the public API never receive them.
  * Agents get only the minimum necessary: the pseudonymous member_id, state, plan
    data, clinical codes, and the member's AGE on the service date (computed server-side).
  * Free text that enters the system (business-request notes, reviewer comments) and free
    text the LLM produces is redacted before it is stored or forwarded: by pattern, and
    against the member's own identifiers on record (services/privacy_service.py).
"""
import re
from dataclasses import dataclass, field

# Field names that must never appear in an agent-facing (internal API) response model.
PII_FIELD_NAMES = frozenset({
    "first_name", "last_name", "full_name", "name", "date_of_birth", "dob", "birth_date", "sex", "gender",
    "ssn", "social_security_number", "email", "phone", "phone_number", "address", "address_line1",
    "address_line2", "street", "city", "zip", "postal_code",
})

REDACTED = "[REDACTED:{kind}]"

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = (r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{4}|"
         + _MONTHS + r"\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+" + _MONTHS + r"\s+\d{4})")

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b(?:ssn|social security(?: number)?)\W{0,3}\d{9}\b", re.I)),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    # a date is only treated as PII when labelled as a birth date (service dates are not PII)
    ("DOB", re.compile(r"\b(?:dob|d\.o\.b\.?|date of birth|birth ?date|born(?: on)?)\W{0,3}" + _DATE, re.I)),
    ("ADDRESS", re.compile(r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Boulevard|Ln|Lane|"
                           r"Dr|Drive|Ct|Court|Way|Pl|Place)\b\.?")),
]
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class Redaction:
    text: str
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.kinds)


def _sub(text: str, kind: str, rx: re.Pattern, kinds: dict) -> str:
    text, n = rx.subn(REDACTED.format(kind=kind), text)
    if n:
        kinds[kind] = kinds.get(kind, 0) + n
    return text


def known_value_patterns(identifiers: dict[str, list[str]]) -> list[tuple[str, re.Pattern]]:
    """Exact-match patterns for one person's identifiers on record, e.g. {"NAME": ["Ava", "Thornbury"]}."""
    out = []
    for kind, values in identifiers.items():
        for v in sorted({v for v in values if v and len(v) >= 2}, key=len, reverse=True):
            if kind == "PHONE_DIGITS":     # match the digits with any punctuation between them
                digits = re.sub(r"\D", "", v)
                out.append(("PHONE", re.compile(r"\D?".join(digits))))
            else:
                out.append((kind, re.compile(r"(?<!\w)" + re.escape(v) + r"(?!\w)", re.I)))
    return out


def redact(text: str | None, extra: list[tuple[str, re.Pattern]] = ()) -> Redaction:
    if not text:
        return Redaction(text or "")
    kinds: dict[str, int] = {}
    text = _CONTROL.sub(" ", text)
    for kind, rx in list(extra) + PATTERNS:        # known values first, so full names go before patterns split them
        text = _sub(text, kind, rx, kinds)
    return Redaction(text, kinds)


def find_pii(obj, known_values: list[str] = (), path: str = "$") -> list[tuple[str, str]]:
    """Deep-scan any JSON-like object. Returns (path, what) for every PII hit: a PII-named key holding a
    value, a pattern match, or an exact (word-boundary) occurrence of one of `known_values`."""
    hits = []
    known = [(v, re.compile(r"(?<!\w)" + re.escape(v) + r"(?!\w)", re.I)) for v in known_values if v]
    if isinstance(obj, dict):
        for k, v in obj.items():
            # a PII-named key holding text is a leak; a count keyed by kind (e.g. {"DOB": 1}) is not
            if str(k).lower() in PII_FIELD_NAMES and isinstance(v, str) and v:
                hits.append((f"{path}.{k}", f"PII field '{k}'"))
            hits += find_pii(v, known_values, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits += find_pii(v, known_values, f"{path}[{i}]")
    elif isinstance(obj, str):
        for kind, rx in PATTERNS:
            if rx.search(obj):
                hits.append((path, f"{kind} pattern"))
        for v, rx in known:
            if rx.search(obj):
                hits.append((path, f"known value '{v}'"))
    return hits
