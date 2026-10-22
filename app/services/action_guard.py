"""Server-side enforcement of "act only after human approval".

The internal write endpoints call `authorize()` inside the same transaction as
the write. A valid agent credential is therefore not enough to change a record:
the write must belong to a run that is executing its actions, under that run's
latest APPROVE decision for its latest recommendation, and the action must be
both permitted for the outcome and one the approved recommendation proposed.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models as m
from app.services.access_policy import permitted_actions

NOTICE_ACTION = {"PROVIDER_DETERMINATION_NOTICE": "NOTIFY_PROVIDER", "DOCUMENT_REQUEST": "REQUEST_DOCUMENTS",
                 "MEDICAL_DIRECTOR_ESCALATION": "ESCALATE_TO_MEDICAL_DIRECTOR"}
DETERMINATION_FOR = {"APPROVE": "APPROVED", "DENY": "DENIED"}


class ActionForbidden(Exception):
    pass


def authorize(sess: Session, run_id: str, approval_id: int, action: str, member_id: str,
              request_number: str | None, determination_status: str | None = None) -> None:
    run = sess.get(m.WorkflowRun, run_id)
    if run is None:
        raise ActionForbidden("unknown workflow run")
    if run.status != "RUNNING" or run.current_step != "execute_actions":
        raise ActionForbidden(f"run is {run.status}/{run.current_step}, not executing approved actions")
    if run.state.get("approval_id") != approval_id:
        raise ActionForbidden("approval is not the run's current approval")
    approval = sess.get(m.Approval, approval_id)
    if approval is None or approval.run_id != run_id or approval.decision != "APPROVE":
        raise ActionForbidden("no APPROVE decision with this id for this run")
    latest = sess.scalar(select(func.max(m.Recommendation.version)).where(m.Recommendation.run_id == run_id))
    if approval.recommendation_version != latest:
        raise ActionForbidden("approval does not cover the latest recommendation")
    rec = sess.scalar(select(m.Recommendation).where(m.Recommendation.run_id == run_id,
                                                     m.Recommendation.version == latest)).payload
    request_type = run.state["intake"]["request_type"]
    if action not in permitted_actions(request_type, rec["outcome"]) or action not in rec["proposed_actions"]:
        raise ActionForbidden(f"{action} is not permitted/proposed for {request_type}/{rec['outcome']}")
    if member_id != run.member_id or request_number != run.state["intake"]["auth_request_number"]:
        raise ActionForbidden("member/request does not match the approved run")
    if determination_status is not None and DETERMINATION_FOR.get(rec["outcome"]) != determination_status:
        raise ActionForbidden(f"determination {determination_status} contradicts approved outcome {rec['outcome']}")
