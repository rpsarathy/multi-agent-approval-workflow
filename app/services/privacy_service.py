"""Server-side redaction against a member's identifiers on record.

The identifiers are read and used here, inside the service layer, and are never returned:
callers get back only the redacted text and the kinds/counts of what was removed.
"""
from app import privacy
from app.db import session_scope
from app.repositories import DomainRepository


def _identifiers(member_id: str | None) -> dict[str, list[str]]:
    if not member_id:
        return {}
    with session_scope() as sess:
        m = DomainRepository(sess).member(member_id)
        if m is None:
            return {}
        dob = m.date_of_birth
        return {
            "NAME": [f"{m.first_name} {m.last_name}", f"{m.last_name}, {m.first_name}", m.last_name, m.first_name],
            "DOB": [dob.isoformat(), f"{dob:%m/%d/%Y}", f"{dob.month}/{dob.day}/{dob.year}", f"{dob:%B} {dob.day}, {dob.year}",
                    f"{dob:%b} {dob.day}, {dob.year}", f"{dob.day} {dob:%B} {dob.year}"],
            "EMAIL": [m.email or ""],
            "PHONE_DIGITS": [m.phone or ""],
            "SSN": [m.ssn or "", (m.ssn or "").replace("-", "")],
            "ADDRESS": [m.address_line1 or ""],
        }


def redact_for_member(member_id: str | None, text: str | None) -> privacy.Redaction:
    return privacy.redact(text, privacy.known_value_patterns(_identifiers(member_id)))
