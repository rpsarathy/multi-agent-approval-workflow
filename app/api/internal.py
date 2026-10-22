"""Internal application API: the only path by which agents reach domain data.

Served by its own ASGI app (app/internal_main.py), never mounted on the public app.
Every call carries an agent identity and that agent's own credential (HMAC of the
identity, see app/security.py); each route declares its operation and the agent
must hold a grant for it. Writes also need an Idempotency-Key and are re-authorised
against the workflow's human approval by the service layer.
"""
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

from app import schemas as s
from app.security import verify_agent
from app.services import domain
from app.services.access_policy import agent_may_call

router = APIRouter(prefix="/internal/v1", tags=["internal"])

ID = r"^[A-Za-z0-9\-]{1,24}$"


def require(operation: str):
    def dep(x_agent_id: str = Header(..., max_length=64), x_agent_token: str = Header(..., max_length=128)) -> str:
        if not verify_agent(x_agent_id, x_agent_token):
            raise HTTPException(401, "invalid agent credential")
        if not agent_may_call(x_agent_id, operation):
            raise HTTPException(403, f"agent '{x_agent_id}' is not permitted to call '{operation}'")
        return x_agent_id
    return Depends(dep)


def _wrap(fn, *args):
    try:
        return fn(*args)
    except domain.NotFound as e:
        raise HTTPException(404, str(e))
    except domain.Conflict as e:
        raise HTTPException(409, str(e))
    except domain.Forbidden as e:
        raise HTTPException(403, f"action not authorised: {e}")


@router.get("/members/{member_id}", response_model=s.MemberProfile, dependencies=[require("get_member_profile")])
def member_profile(member_id: str = Path(pattern=ID)):
    return _wrap(domain.get_member_profile, member_id)


@router.get("/members/{member_id}/age", response_model=s.MemberAgeOut, dependencies=[require("get_member_age")])
def member_age(member_id: str = Path(pattern=ID), on: date = Query(...)):
    return _wrap(domain.get_member_age, member_id, on)


@router.get("/members/{member_id}/auth-requests/{request_number}", response_model=s.AuthRequestOut,
            dependencies=[require("get_auth_request")])
def auth_request(member_id: str = Path(pattern=ID), request_number: str = Path(pattern=ID)):
    return _wrap(domain.get_auth_request, member_id, request_number)


@router.get("/providers/{provider_id}", response_model=s.ProviderOut, dependencies=[require("get_provider")])
def provider(provider_id: int, state_code: str = Query(pattern=r"^[A-Z]{2}$"), plan_id: int = Query(ge=1)):
    return _wrap(domain.get_provider, provider_id, state_code, plan_id)


@router.get("/policy-rules", response_model=s.PolicyRulesOut, dependencies=[require("get_policy_rules")])
def policy_rules(state_code: str = Query(pattern=r"^[A-Z]{2}$"),
                 insurance_category: str = Query(pattern=r"^(COMMERCIAL|MEDICARE|MEDICAID)$"),
                 cpt_code: str = Query(pattern=r"^[A-Z0-9]{4,8}$"), on: date = Query(...)):
    return _wrap(domain.get_policy_rules, state_code, insurance_category, cpt_code, on)


@router.post("/auth-requests/{request_number}/determination", response_model=s.DeterminationOut,
             dependencies=[require("record_determination")])
def determination(body: s.DeterminationIn, request_number: str = Path(pattern=ID),
                  idempotency_key: str = Header(..., min_length=8, max_length=160)):
    return _wrap(domain.record_determination, request_number, body, idempotency_key)


@router.post("/notices", response_model=s.NoticeOut, dependencies=[require("create_notice")])
def notice(body: s.NoticeIn, idempotency_key: str = Header(..., min_length=8, max_length=160)):
    return _wrap(domain.create_notice, body, idempotency_key)
