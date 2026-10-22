"""Action executor: runs follow-up actions after human approval.

Three layers enforce "only after approval":
  1. the orchestrator only reaches this step from an APPROVE decision;
  2. this agent re-checks the approval/recommendation it is handed (below);
  3. the internal API's service layer re-verifies the run, approval, outcome and
     action inside the write transaction (services/action_guard.py).
Every write carries the idempotency key `<run>:<approval>:<action>`; the server
stores it with the write and replays the original response, so re-running this
step after a crash never duplicates a side effect.
"""
from app.agents.tools import ToolClient, ToolError
from app.services.access_policy import permitted_actions

NOTICE_TYPE = {"NOTIFY_PROVIDER": "PROVIDER_DETERMINATION_NOTICE", "REQUEST_DOCUMENTS": "DOCUMENT_REQUEST",
               "ESCALATE_TO_MEDICAL_DIRECTOR": "MEDICAL_DIRECTOR_ESCALATION"}


class ActionNotPermitted(Exception):
    pass


def _notice_body(kind: str, rec: dict, request_number: str | None) -> str:
    ref = request_number or "coverage inquiry"
    if kind == "DOCUMENT_REQUEST":
        items = "; ".join(rec["missing_information"]) or "see reviewer notes"
        return f"[SYNTHETIC] Additional information requested for {ref}: {items}"[:4000]
    if kind == "MEDICAL_DIRECTOR_ESCALATION":
        return f"[SYNTHETIC] Escalated for physician review ({ref}): {rec['summary']}"[:4000]
    return f"[SYNTHETIC] Determination for {ref}: {rec['outcome']}. {rec['summary']}"[:4000]


def execute(tools: ToolClient, *, run_id: str, member_id: str, request_type: str, request_number: str | None,
            approval: dict, recommendation: dict) -> list[dict]:
    if approval["decision"] != "APPROVE" or approval["run_id"] != run_id:
        raise ActionNotPermitted("no matching APPROVE decision for this run")
    if approval["recommendation_version"] != recommendation["version"]:
        raise ActionNotPermitted("approval does not refer to the latest recommendation")
    rec = recommendation["payload"]
    allowed = permitted_actions(request_type, rec["outcome"])
    auth = {"workflow_run_id": run_id, "approval_id": approval["approval_id"]}

    results = []
    for action in rec["proposed_actions"]:
        if action not in allowed:
            raise ActionNotPermitted(f"{action} is not permitted for {request_type}/{rec['outcome']}")
        key = f"{run_id}:{approval['approval_id']}:{action}"
        try:
            if action == "RECORD_DETERMINATION":
                status = {"APPROVE": "APPROVED", "DENY": "DENIED"}[rec["outcome"]]
                out = tools.call("record_determination", idempotency_key=key, request_number=request_number,
                                 body={**auth, "status": status, "note": rec["summary"][:2000]})
            else:
                kind = NOTICE_TYPE[action]
                out = tools.call("create_notice", idempotency_key=key,
                                 body={**auth, "member_id": member_id, "request_number": request_number,
                                       "notice_type": kind, "body": _notice_body(kind, rec, request_number)})
            results.append({"action_type": action, "idempotency_key": key, "status": "SUCCEEDED",
                            "result": out.model_dump(mode="json")})
        except ToolError as e:
            results.append({"action_type": action, "idempotency_key": key, "status": "FAILED",
                            "result": {"error": e.message, "http_status": e.status}})
    return results
