"""Retrieval agents. They reach data only through their ToolClient (internal API)."""
from datetime import date

from app.agents.tools import ToolClient


def member_agent(tools: ToolClient, member_id: str) -> dict:
    """Customer information: demographics + enrollments (with the plan = insurance policy)."""
    return tools.call("get_member_profile", member_id=member_id).model_dump(mode="json")


def _select_enrollment(profile: dict, service_date: date, preferred_id: int | None) -> tuple[dict | None, list[str]]:
    notes = []
    if preferred_id is not None:
        for e in profile["enrollments"]:
            if e["enrollment_id"] == preferred_id:
                return e, notes
        notes.append(f"enrollment {preferred_id} referenced by the request is not on the member's profile")
    covering = [e for e in profile["enrollments"]
                if date.fromisoformat(e["coverage_start"]) <= service_date
                and (e["coverage_end"] is None or service_date <= date.fromisoformat(e["coverage_end"]))]
    pool = covering or profile["enrollments"]
    if not pool:
        return None, notes + ["member has no enrollments"]
    # Prefer ACTIVE, then latest start; ambiguity is reported by the validation agent.
    pool = sorted(pool, key=lambda e: (e["status"] == "ACTIVE", e["coverage_start"]), reverse=True)
    return pool[0], notes


def policy_agent(tools: ToolClient, intake: dict, profile: dict) -> dict:
    """Policy details and applicable policy rules for the requested service."""
    out: dict = {"auth_request": None, "enrollment": None, "provider": None, "policy_rules": None, "retrieval_notes": [],
                 "member_age": None}
    preferred_enrollment = None
    if intake["request_type"] == "PRIOR_AUTH_REVIEW":
        ar = tools.call("get_auth_request", member_id=intake["member_id"],
                        request_number=intake["auth_request_number"]).model_dump(mode="json")
        out["auth_request"] = ar
        service = {"cpt_code": ar["cpt_code"], "icd10_code": ar["primary_icd10"], "units": ar["units_requested"],
                   "service_date": ar["service_date"], "service_state": ar["service_state"],
                   "provider_id": ar["requesting_provider_id"]}
        preferred_enrollment = ar["enrollment_id"]
    else:
        service = {"cpt_code": intake["cpt_code"], "icd10_code": intake["icd10_code"], "units": intake["units"],
                   "service_date": intake["service_date"], "service_state": profile["state_code"], "provider_id": None}
    out["service"] = service
    dos = date.fromisoformat(service["service_date"])

    # Age on the service date is computed by the service layer; the agent never sees a date of birth.
    out["member_age"] = tools.call("get_member_age", member_id=intake["member_id"], on=dos).age_years
    enrollment, notes = _select_enrollment(profile, dos, preferred_enrollment)
    out["enrollment"], out["retrieval_notes"] = enrollment, notes
    if enrollment is None:
        return out
    plan = enrollment["plan"]

    if service["provider_id"] is not None:
        out["provider"] = tools.call("get_provider", provider_id=service["provider_id"],
                                     state_code=service["service_state"], plan_id=plan["plan_id"]).model_dump(mode="json")
    # Coverage rules are keyed by the plan's state (where the policy is regulated). An unknown
    # procedure code is a 404 that ends the run as INVALID_REQUEST; a known procedure with no
    # state rule returns an empty rule list, which the validation agent reports.
    out["policy_rules"] = tools.call("get_policy_rules", state_code=plan["state_code"],
                                     insurance_category=plan["insurance_category"], cpt_code=service["cpt_code"],
                                     on=dos).model_dump(mode="json")
    return out
