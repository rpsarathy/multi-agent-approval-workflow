"""Durable workflow persistence: runs, checkpoints, recommendations, approvals,
action executions, token ledger and the hash-chained audit log.

Used by the orchestrator (infrastructure), never by agents.
"""
import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app import models as m
from app.db import session_scope
from app.models import utcnow

GENESIS = "0" * 64


class StaleRun(Exception):
    """Another writer advanced the run since it was loaded."""


class RunNotFound(Exception):
    pass


# ------------------------------------------------------------------- audit
def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _event_hash(prev_hash: str, run_id: str, seq: int, event_type: str, actor: str, payload: dict, at: datetime) -> str:
    body = _canonical({"run_id": run_id, "seq": seq, "type": event_type, "actor": actor, "payload": payload,
                       "at": at.isoformat()})
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def _append_audit(sess: Session, run_id: str, event_type: str, actor: str, payload: dict) -> m.AuditEvent:
    last = sess.scalar(select(m.AuditEvent).where(m.AuditEvent.run_id == run_id).order_by(m.AuditEvent.seq.desc()).limit(1))
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else GENESIS
    at = utcnow()
    payload = json.loads(_canonical(payload))      # JSON-normalise once so the stored value hashes identically
    ev = m.AuditEvent(run_id=run_id, seq=seq, event_type=event_type, actor=actor, payload=payload, prev_hash=prev,
                      hash=_event_hash(prev, run_id, seq, event_type, actor, payload, at), created_at=at)
    sess.add(ev)
    sess.flush()
    return ev


def append_audit(run_id: str, event_type: str, actor: str, payload: dict) -> None:
    with session_scope() as sess:
        _append_audit(sess, run_id, event_type, actor, payload)


def audit_trail(run_id: str) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.AuditEvent).where(m.AuditEvent.run_id == run_id).order_by(m.AuditEvent.seq))
        return [{"seq": e.seq, "event_type": e.event_type, "actor": e.actor, "payload": e.payload,
                 "created_at": e.created_at.isoformat(), "hash": e.hash, "prev_hash": e.prev_hash} for e in rows]


def verify_audit_chain(run_id: str) -> dict:
    with session_scope() as sess:
        prev = GENESIS
        n = 0
        for e in sess.scalars(select(m.AuditEvent).where(m.AuditEvent.run_id == run_id).order_by(m.AuditEvent.seq)):
            n += 1
            expected = _event_hash(prev, run_id, e.seq, e.event_type, e.actor, e.payload, e.created_at)
            if e.seq != n or e.prev_hash != prev or e.hash != expected:
                return {"valid": False, "events": n, "first_invalid_seq": e.seq}
            prev = e.hash
        return {"valid": True, "events": n, "first_invalid_seq": None}


# -------------------------------------------------------------------- runs
def _run_dict(r: m.WorkflowRun) -> dict:
    return {"run_id": r.run_id, "member_id": r.member_id, "request": r.request, "status": r.status,
            "current_step": r.current_step, "state": r.state, "llm_mode": r.llm_mode, "token_budget": r.token_budget,
            "error": r.error, "version": r.version, "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat()}


def create_run(run_id: str, member_id: str, request: dict, llm_mode: str, token_budget: int, actor: str,
               requested_budget: int | None = None) -> dict:
    with session_scope() as sess:
        r = m.WorkflowRun(run_id=run_id, member_id=member_id, request=request, status="RUNNING",
                          current_step="intake", state={}, llm_mode=llm_mode, token_budget=token_budget)
        sess.add(r)
        sess.flush()
        _append_audit(sess, run_id, "WORKFLOW_STARTED", actor,
                      {"member_id": member_id, "request": request, "llm_mode": llm_mode, "token_budget": token_budget,
                       "requested_token_budget": requested_budget})
        return _run_dict(r)


def get_run(run_id: str) -> dict:
    with session_scope() as sess:
        r = sess.get(m.WorkflowRun, run_id)
        if r is None:
            raise RunNotFound(run_id)
        return _run_dict(r)


def list_runs(limit: int = 50) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.WorkflowRun).order_by(m.WorkflowRun.created_at.desc()).limit(limit))
        return [{k: v for k, v in _run_dict(r).items() if k != "state"} for r in rows]


def save_step(run_id: str, expected_version: int, step: str, next_step: str, status: str, state: dict,
              audit: list[tuple[str, str, dict]] = (), error: str | None = None, recommendation: dict | None = None,
              action_records: list[dict] | None = None, lease_owner: str | None = None) -> dict:
    """Atomically: advance the run, write a checkpoint, append audit events, and persist the step's
    recommendation / action outcomes. Optimistic-locked on version."""
    try:
        with session_scope() as sess:
            r = sess.get(m.WorkflowRun, run_id)
            if r is None:
                raise RunNotFound(run_id)
            if r.version != expected_version:
                raise StaleRun(f"run {run_id} at version {r.version}, expected {expected_version}")
            if lease_owner is not None and r.lease_owner != lease_owner:
                raise StaleRun(f"run {run_id}: worker {lease_owner} no longer holds the lease")
            seq = (sess.scalar(select(func.max(m.WorkflowCheckpoint.seq)).where(m.WorkflowCheckpoint.run_id == run_id)) or 0) + 1
            r.state, r.current_step, r.status, r.error, r.updated_at = state, next_step, status, error, utcnow()
            sess.add(m.WorkflowCheckpoint(run_id=run_id, seq=seq, step=step, next_step=next_step, state=state))
            if recommendation:
                sess.add(m.Recommendation(run_id=run_id, version=recommendation["version"],
                                          payload=recommendation["payload"], generated_by=recommendation["generated_by"],
                                          guardrail_flags=recommendation["flags"]))
            for a in action_records or ():
                _upsert_action(sess, run_id, state["approval_id"], a)
            for event_type, actor, payload in audit:
                _append_audit(sess, run_id, event_type, actor, payload)
            sess.flush()
            return _run_dict(r)
    except StaleDataError as e:
        raise StaleRun(str(e)) from e


# ------------------------------------------------------------------ leases
class ResumeRefused(Exception):
    pass


def _lease_free(now):
    W = m.WorkflowRun
    return or_(W.lease_owner.is_(None), W.lease_expires_at < now)


def acquire_lease(run_id: str, owner: str, ttl: int) -> bool:
    """Atomic compare-and-set: take the lease if it is free, expired, or already ours."""
    now = utcnow()
    W = m.WorkflowRun
    with session_scope() as sess:
        res = sess.execute(update(W).where(W.run_id == run_id, or_(_lease_free(now), W.lease_owner == owner))
                           .values(lease_owner=owner, lease_expires_at=now + timedelta(seconds=ttl)))
        return res.rowcount == 1


def renew_lease(run_id: str, owner: str, ttl: int) -> bool:
    W = m.WorkflowRun
    with session_scope() as sess:
        res = sess.execute(update(W).where(W.run_id == run_id, W.lease_owner == owner)
                           .values(lease_expires_at=utcnow() + timedelta(seconds=ttl)))
        return res.rowcount == 1


def release_lease(run_id: str, owner: str) -> None:
    W = m.WorkflowRun
    with session_scope() as sess:
        sess.execute(update(W).where(W.run_id == run_id, W.lease_owner == owner)
                     .values(lease_owner=None, lease_expires_at=None))


def claim_for_resume(run_id: str, owner: str, ttl: int, actor: str) -> dict:
    """Atomically: a FAILED run, or a RUNNING run whose worker lease has expired, becomes
    RUNNING under `owner`'s lease. Anything else is refused."""
    now = utcnow()
    with session_scope() as sess:
        r = sess.get(m.WorkflowRun, run_id)
        if r is None:
            raise RunNotFound(run_id)
        lease_free = r.lease_owner is None or (r.lease_expires_at is not None and r.lease_expires_at < now)
        if not (r.status == "FAILED" and lease_free) and not (r.status == "RUNNING" and lease_free):
            held = "" if lease_free else f" (worker lease held until {r.lease_expires_at.isoformat()})"
            raise ResumeRefused(f"run is {r.status}{held}; only FAILED runs or RUNNING runs with an expired lease "
                                f"can be resumed")
        prev = r.status
        r.status, r.error, r.updated_at = "RUNNING", None, now
        r.lease_owner, r.lease_expires_at = owner, now + timedelta(seconds=ttl)
        _append_audit(sess, run_id, "WORKFLOW_RESUMED", actor, {"from_step": r.current_step, "from_status": prev})
        sess.flush()
        return _run_dict(r)


def fail_orphaned_runs() -> list[str]:
    """RUNNING runs whose worker lease has expired (or that never had one) were interrupted.
    Mark them FAILED so they can be resumed. Safe with several workers: live workers keep renewing."""
    now = utcnow()
    W = m.WorkflowRun
    out = []
    with session_scope() as sess:
        for r in sess.scalars(select(W).where(W.status == "RUNNING", _lease_free(now))):
            r.status, r.error, r.updated_at = "FAILED", f"{r.current_step}: worker lease expired (interrupted)", now
            r.lease_owner = r.lease_expires_at = None
            _append_audit(sess, r.run_id, "RECOVERED_AFTER_INTERRUPTION", "orchestrator", {"step": r.current_step})
            out.append(r.run_id)
    return out


def expire_stale_reservations(older_than_seconds: int) -> int:
    """A RESERVED ledger row this old belongs to a call whose process died. Its real usage is unknown,
    so it is settled as ABANDONED at the worst case it reserved (never under-counts)."""
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    E = m.TokenLedgerEntry
    with session_scope() as sess:
        rows = list(sess.scalars(select(E).where(E.outcome == "RESERVED", E.created_at < cutoff)))
        for e in rows:
            e.outcome, e.input_tokens, e.output_tokens = "ABANDONED", e.estimated_input_tokens, e.reserved_output_tokens
            e.detail = "reservation expired without settlement; charged at the reserved worst case"
        return len(rows)


def checkpoints(run_id: str) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.WorkflowCheckpoint).where(m.WorkflowCheckpoint.run_id == run_id)
                            .order_by(m.WorkflowCheckpoint.seq))
        return [{"seq": c.seq, "step": c.step, "next_step": c.next_step, "created_at": c.created_at.isoformat()} for c in rows]


# --------------------------------------------------------- recommendations
def recommendations(run_id: str) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.Recommendation).where(m.Recommendation.run_id == run_id).order_by(m.Recommendation.version))
        return [{"version": r.version, "payload": r.payload, "generated_by": r.generated_by,
                 "guardrail_flags": r.guardrail_flags, "created_at": r.created_at.isoformat()} for r in rows]


# --------------------------------------------------------------- approvals
def record_decision(run_id: str, expected_version: int, rec_version: int, decision: str, reviewer_id: str,
                    comment: str | None, next_step: str, status: str, state: dict, audit_payload: dict) -> tuple[int, dict]:
    """Persist the human decision and advance the run in one transaction."""
    try:
        with session_scope() as sess:
            r = sess.get(m.WorkflowRun, run_id)
            if r is None:
                raise RunNotFound(run_id)
            if r.version != expected_version:
                raise StaleRun(f"run {run_id} at version {r.version}, expected {expected_version}")
            a = m.Approval(run_id=run_id, recommendation_version=rec_version, decision=decision, reviewer_id=reviewer_id,
                           comment=comment)
            sess.add(a)
            sess.flush()
            seq = (sess.scalar(select(func.max(m.WorkflowCheckpoint.seq)).where(m.WorkflowCheckpoint.run_id == run_id)) or 0) + 1
            state = {**state, "approval_id": a.approval_id}
            r.state, r.current_step, r.status, r.updated_at = state, next_step, status, utcnow()
            sess.add(m.WorkflowCheckpoint(run_id=run_id, seq=seq, step="human_approval", next_step=next_step, state=state))
            _append_audit(sess, run_id, f"HUMAN_{decision}", f"reviewer:{reviewer_id}",
                          {**audit_payload, "approval_id": a.approval_id})
            sess.flush()
            return a.approval_id, _run_dict(r)
    except StaleDataError as e:
        raise StaleRun(str(e)) from e


def approvals(run_id: str) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.Approval).where(m.Approval.run_id == run_id).order_by(m.Approval.approval_id))
        return [{"approval_id": a.approval_id, "recommendation_version": a.recommendation_version, "decision": a.decision,
                 "reviewer_id": a.reviewer_id, "comment": a.comment, "decided_at": a.decided_at.isoformat()} for a in rows]


def get_approval(approval_id: int) -> dict | None:
    with session_scope() as sess:
        a = sess.get(m.Approval, approval_id)
        return None if a is None else {"approval_id": a.approval_id, "run_id": a.run_id, "decision": a.decision,
                                       "recommendation_version": a.recommendation_version, "reviewer_id": a.reviewer_id}


# ----------------------------------------------------------------- actions
def _upsert_action(sess: Session, run_id: str, approval_id: int, a: dict) -> None:
    existing = sess.scalar(select(m.ActionExecution).where(m.ActionExecution.idempotency_key == a["idempotency_key"]))
    if existing is None:
        sess.add(m.ActionExecution(run_id=run_id, approval_id=approval_id, action_type=a["action_type"],
                                   idempotency_key=a["idempotency_key"], status=a["status"], result=a["result"]))
    else:   # a retry overwrites the earlier outcome
        existing.status, existing.result, existing.executed_at = a["status"], a["result"], utcnow()


def actions(run_id: str) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.ActionExecution).where(m.ActionExecution.run_id == run_id)
                            .order_by(m.ActionExecution.action_execution_id))
        return [{"action_type": a.action_type, "status": a.status, "result": a.result,
                 "idempotency_key": a.idempotency_key, "executed_at": a.executed_at.isoformat()} for a in rows]


# ------------------------------------------------------------ token ledger
def _charged():
    """Tokens a ledger row counts against budgets: the worst case while RESERVED; afterwards every token the
    API reported processing (uncached input, output, and cache writes and reads, which are billed too)."""
    e = m.TokenLedgerEntry
    return case((e.outcome == "RESERVED", e.estimated_input_tokens + e.reserved_output_tokens),
                else_=e.input_tokens + e.output_tokens + e.cache_creation_input_tokens + e.cache_read_input_tokens)


def _sum_charged(sess: Session, *where) -> int:
    return int(sess.scalar(select(func.coalesce(func.sum(_charged()), 0)).where(*where)))


def reserve_tokens(*, run_id: str, agent: str, provider: str, model: str, estimated_input: int, max_input: int,
                   max_output: int, run_budget: int, daily_budget: int) -> dict:
    """Atomically check the budgets and reserve the worst case (input + max_tokens).

    The reservation row is inserted first, which takes SQLite's write lock, and the
    budgets are summed afterwards inside the same transaction - so concurrent callers
    are serialised and cannot jointly overshoot. (On Postgres use SERIALIZABLE or an
    advisory lock for the same guarantee.)
    """
    with session_scope() as sess:
        e = m.TokenLedgerEntry(run_id=run_id, agent=agent, provider=provider, model=model, outcome="RESERVED",
                               estimated_input_tokens=estimated_input, reserved_output_tokens=0)
        sess.add(e)
        sess.flush()
        others = m.TokenLedgerEntry.entry_id != e.entry_id
        run_used = _sum_charged(sess, m.TokenLedgerEntry.run_id == run_id, others)
        daily_used = _sum_charged(sess, m.TokenLedgerEntry.created_at >= utcnow() - timedelta(hours=24), others)
        # Output allowance: per-call cap, shrunk to fit what the run has left after this call's input.
        allowance = max(0, min(max_output, run_budget - run_used - estimated_input))
        need = estimated_input + allowance
        reason = None
        if estimated_input > max_input:
            reason = f"input {estimated_input} exceeds per-call cap {max_input}"
        elif allowance < 1024:
            reason = (f"run budget: {run_used} used + {estimated_input} input leaves {allowance} output tokens "
                      f"(< 1024) of {run_budget}")
        elif daily_used + need > daily_budget:
            reason = f"24h budget: {daily_used} used + {need} needed > {daily_budget}"
        if reason:
            e.outcome, e.detail = "BLOCKED_BUDGET", reason
        else:
            e.reserved_output_tokens = allowance
        return {"entry_id": e.entry_id, "allowed": reason is None, "reason": reason or "within budget",
                "estimated_input_tokens": estimated_input, "reserved_output_tokens": allowance if not reason else 0,
                "run_used": run_used, "run_budget": run_budget, "run_remaining": max(run_budget - run_used, 0),
                "daily_used": daily_used, "daily_budget": daily_budget}


def settle_tokens(entry_id: int, *, outcome: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
                  cache_creation: int = 0, detail: str | None = None) -> None:
    with session_scope() as sess:
        e = sess.get(m.TokenLedgerEntry, entry_id)
        e.outcome, e.input_tokens, e.output_tokens, e.detail = outcome, input_tokens, output_tokens, detail
        e.cache_read_input_tokens, e.cache_creation_input_tokens = cache_read, cache_creation


def tokens_used_for_run(run_id: str) -> int:
    with session_scope() as sess:
        return _sum_charged(sess, m.TokenLedgerEntry.run_id == run_id)


def ledger(run_id: str) -> list[dict]:
    with session_scope() as sess:
        rows = sess.scalars(select(m.TokenLedgerEntry).where(m.TokenLedgerEntry.run_id == run_id)
                            .order_by(m.TokenLedgerEntry.entry_id))
        return [{"agent": e.agent, "provider": e.provider, "model": e.model, "outcome": e.outcome,
                 "estimated_input_tokens": e.estimated_input_tokens, "reserved_output_tokens": e.reserved_output_tokens,
                 "input_tokens": e.input_tokens, "output_tokens": e.output_tokens,
                 "cache_read_input_tokens": e.cache_read_input_tokens,
                 "cache_creation_input_tokens": e.cache_creation_input_tokens,
                 "detail": e.detail, "created_at": e.created_at.isoformat()} for e in rows]
