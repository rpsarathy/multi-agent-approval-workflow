"""Domain service: business reads/writes over the repository, returning API DTOs."""
import hashlib
import json
from datetime import date

from app import models as m
from app import schemas as s
from app.db import session_scope
from app.models import utcnow
from app.repositories import DomainRepository
from app.services import action_guard


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Forbidden(Exception):
    pass


def _plan_out(plan: m.Plan, prod: m.InsuranceProduct) -> s.PlanOut:
    return s.PlanOut(plan_id=plan.plan_id, plan_code=plan.plan_code, plan_name=plan.plan_name, payer_name=plan.payer_name,
                     state_code=plan.state_code, is_active=plan.is_active, effective_date=plan.effective_date,
                     termination_date=plan.termination_date, product_name=prod.product_name,
                     insurance_category=prod.category, network_model=prod.network_model,
                     requires_referral=prod.requires_referral, oon_coverage=prod.oon_coverage)


def get_member_profile(member_id: str) -> s.MemberProfile:
    with session_scope() as sess:
        repo = DomainRepository(sess)
        mem = repo.member(member_id)
        if mem is None:
            raise NotFound(f"member {member_id} not found")
        enrollments = [
            s.EnrollmentOut(enrollment_id=e.enrollment_id, relationship=e.relationship, coverage_start=e.coverage_start,
                            coverage_end=e.coverage_end, status=e.status, pcp_provider_id=e.pcp_provider_id,
                            plan=_plan_out(p, prod))
            for e, p, prod in repo.enrollments_with_plans(member_id)
        ]
        return s.MemberProfile(member_id=mem.member_id, state_code=mem.state_code, has_email_on_file=bool(mem.email),
                               is_synthetic=mem.is_synthetic, enrollments=enrollments)


def get_member_age(member_id: str, on: date) -> s.MemberAgeOut:
    with session_scope() as sess:
        mem = DomainRepository(sess).member(member_id)
        if mem is None:
            raise NotFound(f"member {member_id} not found")
        dob = mem.date_of_birth
        age = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
        return s.MemberAgeOut(member_id=member_id, on=on, age_years=age)


def get_auth_request(member_id: str, request_number: str) -> s.AuthRequestOut:
    with session_scope() as sess:
        repo = DomainRepository(sess)
        req = repo.auth_request(request_number)
        # Scope the lookup to the member so one customer's request cannot be read through another's id.
        if req is None or req.member_id != member_id:
            raise NotFound(f"auth request {request_number} not found for member {member_id}")
        docs = [s.DocumentOut(document_id=d.document_id, document_type=d.document_type, received_at=d.received_at,
                              is_complete=d.is_complete) for d in repo.documents(request_number)]
        return s.AuthRequestOut.model_validate({**{c.name: getattr(req, c.name) for c in req.__table__.columns
                                                  if c.name in s.AuthRequestOut.model_fields}, "documents": docs})


def get_provider(provider_id: int, state_code: str, plan_id: int) -> s.ProviderOut:
    with session_scope() as sess:
        repo = DomainRepository(sess)
        p = repo.provider(provider_id)
        if p is None:
            raise NotFound(f"provider {provider_id} not found")
        lic = repo.license(provider_id, state_code)
        net = repo.network(provider_id, plan_id)
        return s.ProviderOut(provider_id=p.provider_id, npi=p.npi, display_name=p.display_name, credential=p.credential,
                             specialty=p.specialty, specialty_group=p.specialty_group, oig_excluded=p.oig_excluded,
                             license_in_state=s.LicenseOut.model_validate(lic) if lic else None,
                             network_for_plan=s.NetworkOut.model_validate(net) if net else None)


def get_policy_rules(state_code: str, insurance_category: str, cpt_code: str, on: date) -> s.PolicyRulesOut:
    with session_scope() as sess:
        repo = DomainRepository(sess)
        proc = repo.procedure(cpt_code)
        if proc is None:
            raise NotFound(f"procedure {cpt_code} not found")
        rules = [s.CoverageRuleOut.model_validate(r)
                 for r in repo.coverage_rules_effective(state_code, insurance_category, cpt_code, on)]
        crit = [s.CriterionOut(criteria_id=c.criteria_id, icd10_code=c.icd10_code, diagnosis_description=d.description,
                               criteria_source=c.criteria_source) for c, d in repo.clinical_criteria(cpt_code)]
        return s.PolicyRulesOut(procedure=s.ProcedureOut.model_validate(proc), coverage_rules=rules, clinical_criteria=crit)


def get_reviewer(reviewer_id: str) -> s.ReviewerOut:
    with session_scope() as sess:
        r = DomainRepository(sess).reviewer(reviewer_id)
        if r is None:
            raise NotFound(f"reviewer {reviewer_id} not found")
        return s.ReviewerOut.model_validate(r)


def get_reviewer_credentials(reviewer_id: str) -> tuple[str, str]:
    """(salt, passcode hash) for login verification. Never returned by any API."""
    with session_scope() as sess:
        r = DomainRepository(sess).reviewer(reviewer_id)
        if r is None:
            raise NotFound(f"reviewer {reviewer_id} not found")
        return r.passcode_salt, r.passcode_hash


def _replay(sess, key: str, operation: str, request_hash: str):
    rec = sess.get(m.IdempotencyRecord, key)
    if rec is None:
        return None
    if rec.operation != operation or rec.request_hash != request_hash:
        raise Conflict("idempotency key reused with a different request")
    return rec.response


def _remember(sess, key: str, operation: str, request_hash: str, response: dict) -> None:
    sess.add(m.IdempotencyRecord(idempotency_key=key, operation=operation, request_hash=request_hash, response=response))


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def record_determination(request_number: str, body: s.DeterminationIn, idempotency_key: str) -> s.DeterminationOut:
    with session_scope() as sess:
        h = _hash(request_number, body.model_dump(mode="json"))
        if (prior := _replay(sess, idempotency_key, "record_determination", h)) is not None:
            return s.DeterminationOut.model_validate(prior)
        repo = DomainRepository(sess)
        req = repo.auth_request(request_number)
        if req is None:
            raise NotFound(f"auth request {request_number} not found")
        try:
            action_guard.authorize(sess, body.workflow_run_id, body.approval_id, "RECORD_DETERMINATION",
                                   req.member_id, request_number, determination_status=body.status)
        except action_guard.ActionForbidden as e:
            raise Forbidden(str(e))
        if req.status != "PENDING":
            raise Conflict(f"auth request {request_number} already {req.status}")
        prev, now = req.status, utcnow()
        repo.set_determination(req, body.status, f"[workflow {body.workflow_run_id}] {body.note}", now)
        out = s.DeterminationOut(request_number=request_number, previous_status=prev, status=body.status, decision_at=now)
        _remember(sess, idempotency_key, "record_determination", h, out.model_dump(mode="json"))
        return out


def create_notice(body: s.NoticeIn, idempotency_key: str) -> s.NoticeOut:
    with session_scope() as sess:
        h = _hash(body.model_dump(mode="json"))
        if (prior := _replay(sess, idempotency_key, "create_notice", h)) is not None:
            return s.NoticeOut.model_validate(prior)
        repo = DomainRepository(sess)
        if repo.member(body.member_id) is None:
            raise NotFound(f"member {body.member_id} not found")
        try:
            action_guard.authorize(sess, body.workflow_run_id, body.approval_id,
                                   action_guard.NOTICE_ACTION[body.notice_type], body.member_id, body.request_number)
        except action_guard.ActionForbidden as e:
            raise Forbidden(str(e))
        n = repo.add_notice(m.Notice(member_id=body.member_id, request_number=body.request_number,
                                     notice_type=body.notice_type, body=body.body))
        out = s.NoticeOut(notice_id=n.notice_id, notice_type=n.notice_type, created_at=n.created_at)
        _remember(sess, idempotency_key, "create_notice", h, out.model_dump(mode="json"))
        return out
