"""Orchestrator: an explicit, persisted state machine.

    intake -> retrieve_member -> retrieve_policy -> validate -> recommend
           -> [AWAITING_APPROVAL: human decision]
                 APPROVE          -> execute_actions -> COMPLETED
                 REJECT           -> REJECTED
                 REQUEST_REVISION -> recommend (with reviewer feedback) -> AWAITING_APPROVAL

After every step the full state, the next step and the audit events are written
in one transaction (workflow_store.save_step), so a crashed or failed run resumes
from its last checkpoint via `resume()`.

Concurrency: a run is advanced only by the worker holding its database lease
(`lease_owner`, `lease_expires_at`). A heartbeat thread renews the lease while the
worker is busy, including during long LLM calls, and every checkpoint write checks
the lease and the optimistic version. A RUNNING run whose lease has expired was
interrupted; recovery marks it FAILED and `resume()` claims it atomically. This
holds across processes and workers that share the database.

A LangGraph-style framework was not used: an explicit machine keeps the
persistence and approval semantics fully visible and avoids depending on a
framework API that could not be verified offline. `NODES` is the single place
to swap in a graph runtime later.
"""
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

from app.agents import actions, intake, recommendation, retrieval, validation
from app.agents.intake import IntakeError
from app.agents.tools import ToolCallRecord, ToolClient, ToolError
from app.config import get_settings
from app.security import agent_token
from app.llm.anthropic_provider import build_provider
from app.llm.budget import TokenBudget
from app.services import domain, privacy_service
from app.services import workflow_store as store
from app.services.access_policy import PERMITTED_ACTIONS, approval_permitted

MAX_REVISIONS = 3
MAX_STEPS_PER_ADVANCE = 20
ORCHESTRATOR = "orchestrator"


class WorkflowError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


_default_http: httpx.Client | None = None


def default_http_client() -> httpx.Client:
    """Real HTTP when INTERNAL_API_BASE_URL is set; otherwise the internal app in-process over ASGI."""
    global _default_http
    if _default_http is None:
        base = get_settings().internal_api_base_url
        if base:
            _default_http = httpx.Client(base_url=base.rstrip("/"), timeout=30)
        else:
            from fastapi.testclient import TestClient

            from app.internal_main import internal_app
            _default_http = TestClient(internal_app, base_url="http://internal")
    return _default_http


@dataclass
class Context:
    provider: object = None
    http: object = None
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}")

    def tools(self, run_id: str, agent_id: str) -> ToolClient:
        """The orchestrator mints each agent's credential; the agent package never sees the secret."""
        def audit(rec: ToolCallRecord):
            store.append_audit(run_id, "TOOL_CALL", f"agent:{rec.agent}",
                               {"tool": rec.tool, "args": rec.args, "http_status": rec.status, "ok": rec.ok,
                                "latency_ms": rec.latency_ms, "error": rec.error})
        return ToolClient(self.http or default_http_client(), agent_id, agent_token(agent_id), on_call=audit)

    def llm(self):
        if self.provider is None:
            self.provider = build_provider(get_settings())
        return self.provider


_ctx = Context()


def context() -> Context:
    return _ctx


def set_context(ctx: Context) -> None:
    global _ctx
    _ctx = ctx


# ---------------------------------------------------------------- nodes
# Each node: (run, ctx) -> (state_updates, [(event_type, actor, payload)], next_step, status)

def node_intake(run, ctx):
    norm = intake.run(run["member_id"], run["request"])
    return {"intake": norm}, [("INTAKE_COMPLETED", "agent:intake_agent", norm)], "retrieve_member", "RUNNING"


def node_retrieve_member(run, ctx):
    profile = retrieval.member_agent(ctx.tools(run["run_id"], "member_retrieval_agent"), run["member_id"])
    return ({"member_profile": profile},
            [("MEMBER_RETRIEVED", "agent:member_retrieval_agent",
              {"member_id": profile["member_id"], "enrollments": [e["enrollment_id"] for e in profile["enrollments"]]})],
            "retrieve_policy", "RUNNING")


def node_retrieve_policy(run, ctx):
    st = run["state"]
    pol = retrieval.policy_agent(ctx.tools(run["run_id"], "policy_retrieval_agent"), st["intake"], st["member_profile"])
    summary = {"enrollment_id": (pol["enrollment"] or {}).get("enrollment_id"),
               "provider_id": (pol["provider"] or {}).get("provider_id"),
               "coverage_rule_ids": [r["coverage_rule_id"] for r in (pol["policy_rules"] or {}).get("coverage_rules", [])],
               "notes": pol["retrieval_notes"]}
    return {"policy": pol}, [("POLICY_RETRIEVED", "agent:policy_retrieval_agent", summary)], "validate", "RUNNING"


def node_validate(run, ctx):
    st = run["state"]
    res = validation.run(st["intake"], st["member_profile"], st["policy"])
    return ({"validation": res},
            [("VALIDATION_COMPLETED", "agent:validation_agent",
              {"summary": res["summary"], "failed": [c["check"] for c in res["checks"] if c["status"] == "FAIL"],
               "unknown": [c["check"] for c in res["checks"] if c["status"] == "UNKNOWN"],
               "data_issues": [f"{i['kind']}:{i['code']}" for i in res["data_issues"]]})],
            "recommend", "RUNNING")


def node_recommend(run, ctx):
    st = run["state"]
    permitted = PERMITTED_ACTIONS[st["intake"]["request_type"]]
    bundle = recommendation.build_bundle(st["intake"], st["member_profile"], st["policy"], st["validation"],
                                         {k: list(v) for k, v in permitted.items()}, st.get("reviewer_feedback", []))
    budget = TokenBudget(run["run_id"], run["token_budget"], "recommendation_agent")
    member = run["member_id"]
    out = recommendation.run(ctx.llm(), budget, bundle,
                             prompt_filter=lambda text: privacy_service.redact_for_member(member, text))
    _redact_model_output(member, out)
    # What a recorded determination would contain (status, reason code, units) comes from the checks, not the LLM.
    out["payload"]["determination"] = validation.determination_for(out["payload"]["outcome"], st["validation"],
                                                                   st["policy"]["service"]["units"])
    # Safe under the run lock; the recommendation row is written by save_step in the step's transaction.
    version = len(store.recommendations(run["run_id"])) + 1
    events = [("TOKEN_BUDGET_CHECKED", ORCHESTRATOR, out["budget"])]
    if any(f.startswith("TOKEN_BUDGET_EXCEEDED") for f in out["flags"]):
        events.append(("TOKEN_BUDGET_EXCEEDED", ORCHESTRATOR, {"reason": out["budget"]["reason"]}))
    events.append(("RECOMMENDATION_CREATED", "agent:recommendation_agent",
                   {"version": version, "outcome": out["payload"]["outcome"], "generated_by": out["generated_by"],
                    "guardrail_flags": out["flags"], "agrees_with_rules": out["payload"]["agrees_with_rules"],
                    "llm": out["llm"]}))
    events.append(("APPROVAL_REQUESTED", ORCHESTRATOR, {"recommendation_version": version}))
    rec = {"version": version, "payload": out["payload"], "generated_by": out["generated_by"], "flags": out["flags"]}
    return {"recommendation_version": version, "_recommendation": rec}, events, "await_approval", "AWAITING_APPROVAL"


def _redact_model_output(member_id: str, out: dict) -> None:
    """Scrub model-written prose against the member's identifiers on record before it is stored,
    shown to reviewers, or written into notices and determination notes."""
    p, kinds = out["payload"], {}

    def scrub(text):
        r = privacy_service.redact_for_member(member_id, text)
        for k, n in r.kinds.items():
            kinds[k] = kinds.get(k, 0) + n
        return r.text

    p["summary"] = scrub(p["summary"])
    p["missing_information"] = [scrub(x) for x in p["missing_information"]]
    for f in p["findings"]:
        f["statement"] = scrub(f["statement"])
    if kinds:
        out["flags"].append(f"PII_REDACTED_FROM_MODEL_OUTPUT: {kinds}")


def node_execute_actions(run, ctx):
    approval = store.get_approval(run["state"]["approval_id"])
    latest = store.recommendations(run["run_id"])[-1]
    intake_ = run["state"]["intake"]
    results = actions.execute(ctx.tools(run["run_id"], "action_executor"), run_id=run["run_id"],
                              member_id=run["member_id"], request_type=intake_["request_type"],
                              request_number=intake_["auth_request_number"], approval=approval, recommendation=latest)
    events = [("ACTION_EXECUTED", "agent:action_executor", r) for r in results]
    if any(r["status"] == "FAILED" for r in results):
        # Leave the run FAILED at this step; on resume the server replays succeeded actions by idempotency key.
        raise ActionsFailed(results, events)
    events.append(("WORKFLOW_COMPLETED", ORCHESTRATOR, {"actions": [r["action_type"] for r in results]}))
    return {"action_results": results, "_action_records": results}, events, "end", "COMPLETED"


class ActionsFailed(Exception):
    def __init__(self, results, events):
        super().__init__("; ".join(f"{r['action_type']}: {r['result'].get('error')}" for r in results if r["status"] == "FAILED"))
        self.results, self.events = results, events


NODES = {
    "intake": "node_intake",
    "retrieve_member": "node_retrieve_member",
    "retrieve_policy": "node_retrieve_policy",
    "validate": "node_validate",
    "recommend": "node_recommend",
    "execute_actions": "node_execute_actions",
}


# --------------------------------------------------------------- driver
@contextmanager
def _leased(run_id: str, owner: str):
    """Hold the run's lease for the duration, renewing it from a heartbeat thread. Yields False if
    another live worker holds it."""
    ttl = get_settings().run_lease_seconds
    if not store.acquire_lease(run_id, owner, ttl):
        yield False
        return
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(max(ttl / 3, 0.05)):
            if not store.renew_lease(run_id, owner, ttl):
                return          # lease lost; the next checkpoint write will be refused

    beat = threading.Thread(target=heartbeat, daemon=True, name=f"lease-{run_id[:8]}")
    beat.start()
    try:
        yield True
    finally:
        stop.set()
        beat.join()
        store.release_lease(run_id, owner)


def _advance(run_id: str) -> dict:
    ctx = context()
    with _leased(run_id, ctx.worker_id) as held:
        run = store.get_run(run_id)
        if not held:
            return run          # another worker is advancing this run
        for _ in range(MAX_STEPS_PER_ADVANCE):
            if run["status"] != "RUNNING":
                return run
            step = run["current_step"]
            fn = globals()[NODES[step]]
            try:
                updates, events, next_step, status = fn(run, ctx)
            except Exception as e:  # persist the failure; resume() retries this step
                # Bad input (unknown member/request/procedure) can never succeed on retry: end as INVALID_REQUEST.
                terminal = isinstance(e, (ToolError, IntakeError)) and getattr(e, "status", 422) in (400, 404, 422)
                status = "INVALID_REQUEST" if terminal else "FAILED"
                events = list(getattr(e, "events", []))
                events.append(("STEP_FAILED", ORCHESTRATOR, {"step": step, "error": f"{type(e).__name__}: {e}",
                                                             "retryable": not terminal}))
                return store.save_step(run_id, run["version"], step, step, status, run["state"], events,
                                       error=f"{step}: {type(e).__name__}: {e}",
                                       action_records=getattr(e, "results", None), lease_owner=ctx.worker_id)
            rec = updates.pop("_recommendation", None)
            action_records = updates.pop("_action_records", None)
            run = store.save_step(run_id, run["version"], step, next_step, status, {**run["state"], **updates}, events,
                                  recommendation=rec, action_records=action_records, lease_owner=ctx.worker_id)
        return run


def start(member_id: str, request: dict, token_budget: int | None = None, actor: str = "api") -> dict:
    cfg = get_settings()
    run_id = str(uuid.uuid4())
    # Free text is redacted before it is persisted anywhere (run record, audit log, LLM prompt).
    request, redaction = dict(request), privacy_service.redact_for_member(member_id, request.get("notes"))
    if redaction.changed:
        request["notes"] = redaction.text
    # A caller may lower the per-run budget, never raise it above the configured cap.
    budget = cfg.token_budget_per_run if token_budget is None else min(token_budget, cfg.token_budget_per_run)
    store.create_run(run_id, member_id, request, cfg.effective_llm_mode, budget, actor,
                     requested_budget=token_budget)
    if redaction.changed:   # record what kind of PII was removed, never the values
        store.append_audit(run_id, "PII_REDACTED", ORCHESTRATOR, {"field": "request.notes", "kinds": redaction.kinds})
    return _advance(run_id)


def resume(run_id: str, actor: str = "api") -> dict:
    ctx = context()
    try:
        store.claim_for_resume(run_id, ctx.worker_id, get_settings().run_lease_seconds, actor)
    except store.ResumeRefused as e:
        raise WorkflowError(409, str(e))
    return _advance(run_id)


def recover_interrupted_runs() -> list[str]:
    """Startup / periodic recovery: fail RUNNING runs whose worker lease expired (so they can be resumed)
    and settle token reservations left by dead calls. Live workers keep their leases renewed, so this is
    safe to run while other workers are busy."""
    cfg = get_settings()
    store.expire_stale_reservations(cfg.token_reservation_expiry_seconds)
    return store.fail_orphaned_runs()


def decide(run_id: str, decision: str, reviewer_id: str, recommendation_version: int, comment: str | None) -> dict:
    # Concurrent decisions are serialised by the run's optimistic version (StaleRun -> 409).
    run = store.get_run(run_id)
    comment_redaction = privacy_service.redact_for_member(run["member_id"], comment)
    comment = comment_redaction.text if comment else comment
    if run["status"] != "AWAITING_APPROVAL":
        raise WorkflowError(409, f"run is {run['status']}, not AWAITING_APPROVAL")
    latest = store.recommendations(run_id)[-1]
    if recommendation_version != latest["version"]:
        raise WorkflowError(409, f"recommendation version {recommendation_version} is stale; latest is {latest['version']}")
    try:
        reviewer = domain.get_reviewer(reviewer_id)
    except domain.NotFound:
        raise WorkflowError(403, f"unknown reviewer {reviewer_id}")
    outcome = latest["payload"]["outcome"]
    if not reviewer.is_active or not approval_permitted(reviewer.role, decision, outcome):
        store.append_audit(run_id, "APPROVAL_DENIED", f"reviewer:{reviewer_id}",
                           {"decision": decision, "role": reviewer.role, "outcome": outcome,
                            "active": reviewer.is_active})
        raise WorkflowError(403, f"reviewer {reviewer_id} ({reviewer.role}, active={reviewer.is_active}) may not "
                                 f"{decision} a {outcome} recommendation")
    state = dict(run["state"])
    if decision == "APPROVE":
        next_step, status = "execute_actions", "RUNNING"
    elif decision == "REJECT":
        next_step, status = "end", "REJECTED"
    else:
        if not comment:
            raise WorkflowError(422, "REQUEST_REVISION requires a comment")
        feedback = list(state.get("reviewer_feedback", []))
        if len(feedback) >= MAX_REVISIONS:
            raise WorkflowError(409, f"revision limit ({MAX_REVISIONS}) reached; approve or reject")
        feedback.append({"recommendation_version": recommendation_version, "reviewer_id": reviewer_id,
                         "comment": comment})
        state["reviewer_feedback"] = feedback
        next_step, status = "recommend", "RUNNING"
    store.record_decision(run_id, run["version"], recommendation_version, decision, reviewer_id, comment, next_step,
                          status, state, {"recommendation_version": recommendation_version, "outcome": outcome,
                                          "role": reviewer.role, "comment": comment,
                                          "pii_redacted": comment_redaction.kinds or None})
    return _advance(run_id)


def snapshot(run_id: str) -> dict:
    run = store.get_run(run_id)
    return {**run, "recommendations": store.recommendations(run_id), "approvals": store.approvals(run_id),
            "actions": store.actions(run_id), "checkpoints": store.checkpoints(run_id),
            "token_usage": token_usage(run_id)}


def token_usage(run_id: str) -> dict:
    run = store.get_run(run_id)
    used = store.tokens_used_for_run(run_id)
    return {"run_budget": run["token_budget"], "used": used, "remaining": max(run["token_budget"] - used, 0),
            "llm_mode": run["llm_mode"], "entries": store.ledger(run_id)}
