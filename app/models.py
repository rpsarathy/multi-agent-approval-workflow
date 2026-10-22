"""SQLAlchemy ORM models.

Two groups of tables:

* Domain (synthetic prior-authorization data): members, plans, enrollments,
  providers, coverage rules, ... Every row carries `is_synthetic=True`.
* Workflow (orchestration state): runs, checkpoints, recommendations,
  approvals, action executions, the token ledger and the hash-chained audit log.
"""
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SyntheticMixin:
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# --------------------------------------------------------------------- domain
class State(SyntheticMixin, Base):
    __tablename__ = "states"
    state_code: Mapped[str] = mapped_column(String(2), primary_key=True)
    state_name: Mapped[str] = mapped_column(String(64))
    pa_decision_deadline_days: Mapped[int] = mapped_column(Integer)


class InsuranceProduct(SyntheticMixin, Base):
    """Product type (reference: `insurance_types`)."""
    __tablename__ = "insurance_products"
    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_name: Mapped[str] = mapped_column(String(64), unique=True)
    category: Mapped[str] = mapped_column(String(16))          # COMMERCIAL | MEDICARE | MEDICAID
    network_model: Mapped[str] = mapped_column(String(8))      # HMO | PPO | EPO | POS
    requires_referral: Mapped[bool] = mapped_column(Boolean)
    oon_coverage: Mapped[bool] = mapped_column(Boolean)


class Plan(SyntheticMixin, Base):
    """The insurance policy a member is enrolled in (reference: `insurance_plans`)."""
    __tablename__ = "plans"
    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_code: Mapped[str] = mapped_column(String(32), unique=True)
    plan_name: Mapped[str] = mapped_column(String(128))
    payer_name: Mapped[str] = mapped_column(String(128))
    product_id: Mapped[int] = mapped_column(ForeignKey("insurance_products.product_id"))
    state_code: Mapped[str] = mapped_column(ForeignKey("states.state_code"))
    effective_date: Mapped[date] = mapped_column(Date)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean)


class Member(SyntheticMixin, Base):
    """The 'customer'. Holds (synthetic) direct identifiers like a real system of record does;
    they never leave the service layer (see app/privacy.py and tests/test_pii.py)."""
    __tablename__ = "members"
    member_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    date_of_birth: Mapped[date] = mapped_column(Date)
    sex: Mapped[str] = mapped_column(String(1))
    state_code: Mapped[str] = mapped_column(ForeignKey("states.state_code"))
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Synthetic only: 9xx area numbers are never issued by the SSA.
    ssn: Mapped[str | None] = mapped_column(String(11), nullable=True)


class Provider(SyntheticMixin, Base):
    __tablename__ = "providers"
    provider_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npi: Mapped[str] = mapped_column(String(10), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    credential: Mapped[str] = mapped_column(String(8))
    specialty: Mapped[str] = mapped_column(String(64))
    specialty_group: Mapped[str] = mapped_column(String(32))   # "Primary Care" | "Specialist" | ...
    oig_excluded: Mapped[bool] = mapped_column(Boolean, default=False)


class ProviderLicense(SyntheticMixin, Base):
    __tablename__ = "provider_licenses"
    __table_args__ = (UniqueConstraint("provider_id", "state_code"),)
    license_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.provider_id"))
    state_code: Mapped[str] = mapped_column(ForeignKey("states.state_code"))
    license_type: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))            # ACTIVE | EXPIRED | SUSPENDED | PROBATION | REVOKED
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class Enrollment(SyntheticMixin, Base):
    __tablename__ = "enrollments"
    enrollment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.member_id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.plan_id"))
    relationship: Mapped[str] = mapped_column(String(16))
    coverage_start: Mapped[date] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(24))            # ACTIVE | TERMINATED | PENDING_VERIFICATION
    pcp_provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.provider_id"), nullable=True)


class ProviderNetwork(SyntheticMixin, Base):
    __tablename__ = "provider_network"
    __table_args__ = (UniqueConstraint("provider_id", "plan_id"),)
    participation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.provider_id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.plan_id"))
    network_status: Mapped[str] = mapped_column(String(16))    # IN_NETWORK | TERMINATED
    effective_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class Procedure(SyntheticMixin, Base):
    __tablename__ = "procedures"
    cpt_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    description: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32))
    requires_prior_auth: Mapped[bool] = mapped_column(Boolean)
    default_required_doc: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Diagnosis(SyntheticMixin, Base):
    __tablename__ = "diagnoses"
    icd10_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    description: Mapped[str] = mapped_column(String(256))
    chapter: Mapped[str] = mapped_column(String(48))           # distinguishes MN-01 from MN-02, as in the reference


class DenialReason(SyntheticMixin, Base):
    """Denial reason codes, same scheme as the reference (internal code -> CARC code)."""
    __tablename__ = "denial_reason_codes"
    denial_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    carc_code: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(24))
    description: Mapped[str] = mapped_column(String(160))
    appealable: Mapped[bool] = mapped_column(Boolean)


class CoverageRule(SyntheticMixin, Base):
    """Policy rule: state x insurance category x procedure, effective-dated."""
    __tablename__ = "coverage_rules"
    __table_args__ = (UniqueConstraint("state_code", "insurance_category", "cpt_code", "effective_date"),)
    coverage_rule_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_code: Mapped[str] = mapped_column(ForeignKey("states.state_code"))
    insurance_category: Mapped[str] = mapped_column(String(16))
    cpt_code: Mapped[str] = mapped_column(ForeignKey("procedures.cpt_code"))
    is_covered: Mapped[bool] = mapped_column(Boolean)
    requires_prior_auth: Mapped[bool] = mapped_column(Boolean)
    required_document_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_units_per_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    citation: Mapped[str | None] = mapped_column(String(256), nullable=True)


class ClinicalCriterion(SyntheticMixin, Base):
    """Policy rule: diagnoses that satisfy medical-necessity criteria for a procedure."""
    __tablename__ = "clinical_criteria"
    criteria_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cpt_code: Mapped[str] = mapped_column(ForeignKey("procedures.cpt_code"))
    icd10_code: Mapped[str] = mapped_column(ForeignKey("diagnoses.icd10_code"))
    criteria_source: Mapped[str] = mapped_column(String(128))


class AuthRequest(SyntheticMixin, Base):
    """A submitted prior-authorization request (single service line, simplified)."""
    __tablename__ = "auth_requests"
    request_number: Mapped[str] = mapped_column(String(24), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.member_id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.enrollment_id"))
    requesting_provider_id: Mapped[int] = mapped_column(ForeignKey("providers.provider_id"))
    service_state: Mapped[str] = mapped_column(ForeignKey("states.state_code"))
    cpt_code: Mapped[str] = mapped_column(ForeignKey("procedures.cpt_code"))
    primary_icd10: Mapped[str] = mapped_column(ForeignKey("diagnoses.icd10_code"))
    units_requested: Mapped[int] = mapped_column(Integer)
    urgency: Mapped[str] = mapped_column(String(16))
    referral_on_file: Mapped[bool] = mapped_column(Boolean)
    submitted_at: Mapped[datetime] = mapped_column(DateTime)
    service_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))            # PENDING | APPROVED | DENIED
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set by RECORD_DETERMINATION: the reason code for a denial or a partial approval, and units approved.
    denial_code: Mapped[str | None] = mapped_column(ForeignKey("denial_reason_codes.denial_code"), nullable=True)
    units_approved: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ClinicalDocument(SyntheticMixin, Base):
    __tablename__ = "clinical_documents"
    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_number: Mapped[str] = mapped_column(ForeignKey("auth_requests.request_number"))
    document_type: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    is_complete: Mapped[bool] = mapped_column(Boolean)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class Reviewer(SyntheticMixin, Base):
    """Human approvers. `role` drives the approval authorization policy."""
    __tablename__ = "reviewers"
    reviewer_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32))              # NURSE_REVIEWER | MEDICAL_DIRECTOR
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    passcode_salt: Mapped[str] = mapped_column(String(32))
    passcode_hash: Mapped[str] = mapped_column(String(64))     # PBKDF2-SHA256; demo passcodes are documented


class Notice(SyntheticMixin, Base):
    """Outbox for follow-up actions (simulated delivery; nothing leaves the machine)."""
    __tablename__ = "notices"
    notice_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_number: Mapped[str | None] = mapped_column(String(24), nullable=True)
    member_id: Mapped[str] = mapped_column(String(16))
    notice_type: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyRecord(Base):
    """Server-side idempotency for internal write operations: a replayed key returns the stored response."""
    __tablename__ = "idempotency_records"
    idempotency_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    operation: Mapped[str] = mapped_column(String(48))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ------------------------------------------------------------------- workflow
class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(16), index=True)
    request: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_step: Mapped[str] = mapped_column(String(32))
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_mode: Mapped[str] = mapped_column(String(8))
    token_budget: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Worker lease: only the owner may advance the run; renewed by a heartbeat while it works.
    lease_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)    # optimistic lock
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __mapper_args__ = {"version_id_col": version}


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)
    checkpoint_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(32))
    next_step: Mapped[str] = mapped_column(String(32))
    state: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("run_id", "version"),)
    recommendation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    generated_by: Mapped[str] = mapped_column(String(64))       # e.g. "mock", "claude-opus-5-5", "rules_fallback"
    guardrail_flags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    approval_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), index=True)
    recommendation_version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(24))           # APPROVE | REJECT | REQUEST_REVISION
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("reviewers.reviewer_id"))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ActionExecution(Base):
    __tablename__ = "action_executions"
    action_execution_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), index=True)
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.approval_id"))
    action_type: Mapped[str] = mapped_column(String(48))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16))             # SUCCEEDED | FAILED
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TokenLedgerEntry(Base):
    __tablename__ = "token_ledger"
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), index=True)
    agent: Mapped[str] = mapped_column(String(48))
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))            # RESERVED | OK | ERROR | BLOCKED_BUDGET
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AuditEvent(Base):
    """Append-only, hash-chained per run: hash = sha256(prev_hash + canonical(event))."""
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"), CheckConstraint("length(hash) = 64"))
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
