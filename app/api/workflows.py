"""Public workflow API used by the approval UI and API clients.

Every workflow endpoint requires a reviewer bearer token from POST /api/v1/auth/login.
The decision endpoint takes the reviewer identity from the token, never from the body.
Demo reviewers and passcodes are synthetic (see app/seed.py); replace this login with a
real identity provider before any non-demo use.
"""
from fastapi import APIRouter, Depends, Header, HTTPException

from app import schemas as s
from app.agents import intake
from app.agents.intake import IntakeError
from app.orchestrator import engine
from app.security import issue_reviewer_token, verify_passcode, verify_reviewer_token
from app.seed import SCENARIOS
from app.services import domain
from app.services import workflow_store as store

router = APIRouter(prefix="/api/v1", tags=["workflows"])


def current_reviewer(authorization: str = Header(default="")) -> s.ReviewerOut:
    token = authorization.removeprefix("Bearer ").strip()
    reviewer_id = verify_reviewer_token(token) if token else None
    if reviewer_id is None:
        raise HTTPException(401, "missing or invalid reviewer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        reviewer = domain.get_reviewer(reviewer_id)
    except domain.NotFound:
        raise HTTPException(401, "reviewer no longer exists")
    # Checked on every request, so deactivation takes effect immediately, even for tokens issued earlier.
    if not reviewer.is_active:
        raise HTTPException(401, "reviewer account is inactive")
    return reviewer


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except store.RunNotFound:
        raise HTTPException(404, "workflow run not found")
    except engine.WorkflowError as e:
        raise HTTPException(e.status, e.message)
    except store.StaleRun as e:
        raise HTTPException(409, f"concurrent update: {e}")


@router.post("/auth/login")
def login(body: s.LoginIn):
    try:
        reviewer, (salt, digest) = domain.get_reviewer(body.reviewer_id), domain.get_reviewer_credentials(body.reviewer_id)
    except domain.NotFound:
        raise HTTPException(401, "invalid reviewer id or passcode")
    if not verify_passcode(body.passcode, salt, digest) or not reviewer.is_active:
        raise HTTPException(401, "invalid reviewer id or passcode")
    token, exp = issue_reviewer_token(reviewer.reviewer_id)
    return {"access_token": token, "token_type": "bearer", "expires_at": exp, "reviewer": reviewer.model_dump()}


@router.post("/workflows", status_code=201)
def start_workflow(body: s.StartWorkflowIn, reviewer: s.ReviewerOut = Depends(current_reviewer)):
    try:
        # Validate the business request up front so obviously bad input never creates a run.
        intake.run(body.member_id, body.request.model_dump(mode="json"))
    except IntakeError as e:
        raise HTTPException(422, str(e))
    run = _call(engine.start, body.member_id, body.request.model_dump(mode="json"), body.token_budget,
                actor=f"reviewer:{reviewer.reviewer_id}")
    return engine.snapshot(run["run_id"])


@router.get("/workflows")
def list_workflows(limit: int = 50, _: s.ReviewerOut = Depends(current_reviewer)):
    return store.list_runs(min(max(limit, 1), 200))


@router.get("/workflows/{run_id}")
def get_workflow(run_id: str, _: s.ReviewerOut = Depends(current_reviewer)):
    return _call(engine.snapshot, run_id)


@router.post("/workflows/{run_id}/decisions")
def decide(run_id: str, body: s.DecisionIn, reviewer: s.ReviewerOut = Depends(current_reviewer)):
    _call(engine.decide, run_id, body.decision.value, reviewer.reviewer_id, body.recommendation_version, body.comment)
    return engine.snapshot(run_id)


@router.post("/workflows/{run_id}/resume")
def resume(run_id: str, reviewer: s.ReviewerOut = Depends(current_reviewer)):
    _call(engine.resume, run_id, actor=f"reviewer:{reviewer.reviewer_id}")
    return engine.snapshot(run_id)


@router.get("/workflows/{run_id}/audit")
def audit(run_id: str, _: s.ReviewerOut = Depends(current_reviewer)):
    _call(store.get_run, run_id)
    return {"events": store.audit_trail(run_id), "integrity": store.verify_audit_chain(run_id)}


@router.get("/workflows/{run_id}/token-usage")
def token_usage(run_id: str, _: s.ReviewerOut = Depends(current_reviewer)):
    return _call(engine.token_usage, run_id)


@router.get("/demo/scenarios")
def scenarios():
    """Synthetic demo scenarios (labelled as such) to populate the UI."""
    return [{"member_id": mid, "auth_request_number": req, "description": desc, "synthetic": True,
             "request": ({"request_type": "PRIOR_AUTH_REVIEW", "auth_request_number": req} if req else
                         {"request_type": "COVERAGE_INQUIRY", "cpt_code": "97110", "icd10_code": "M62.81",
                          "units": 30, "service_date": "2026-11-20"})}
            for mid, (req, desc) in SCENARIOS.items()]


@router.get("/demo/reviewers")
def reviewers():
    """Synthetic demo reviewer accounts (no secrets)."""
    out = []
    for rid in ("SYN-R01", "SYN-R02", "SYN-R03"):
        try:
            out.append(domain.get_reviewer(rid).model_dump())
        except domain.NotFound:
            pass
    return out
