"""Intake agent: normalises the business request into a service request (no tools, no LLM)."""
from app.schemas import BusinessRequest, RequestType


class IntakeError(ValueError):
    pass


def run(member_id: str, request: dict) -> dict:
    req = BusinessRequest.model_validate(request)
    if req.request_type == RequestType.PRIOR_AUTH_REVIEW:
        if not req.auth_request_number:
            raise IntakeError("PRIOR_AUTH_REVIEW requires auth_request_number")
    else:
        missing = [f for f in ("cpt_code", "icd10_code", "service_date") if getattr(req, f) is None]
        if missing:
            raise IntakeError(f"COVERAGE_INQUIRY requires {', '.join(missing)}")
    return {
        "member_id": member_id,
        "request_type": req.request_type.value,
        "auth_request_number": req.auth_request_number,
        "cpt_code": req.cpt_code,
        "icd10_code": req.icd10_code,
        "units": req.units or 1,
        "service_date": req.service_date.isoformat() if req.service_date else None,
        # Free text is carried as data only; it is never interpreted as instructions.
        "notes": req.notes,
    }
