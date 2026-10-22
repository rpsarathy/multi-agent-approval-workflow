"""Pydantic contracts shared by the internal API (responses) and the agent tool layer (validation)."""
from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ----------------------------------------------------------- domain read DTOs
class PlanOut(Strict):
    plan_id: int
    plan_code: str
    plan_name: str
    payer_name: str
    state_code: str
    is_active: bool
    effective_date: date
    termination_date: date | None
    product_name: str
    insurance_category: str
    network_model: str
    requires_referral: bool
    oon_coverage: bool


class EnrollmentOut(Strict):
    enrollment_id: int
    relationship: str
    coverage_start: date
    coverage_end: date | None
    status: str
    pcp_provider_id: int | None
    plan: PlanOut


class MemberProfile(Strict):
    """Minimum necessary for policy work. No direct identifiers (see app/privacy.py): no name,
    date of birth, sex, SSN, address, phone or e-mail."""
    member_id: str                       # pseudonymous system key
    state_code: str
    has_email_on_file: bool
    is_synthetic: bool
    enrollments: list[EnrollmentOut]


class MemberAgeOut(Strict):
    """Age computed server-side, so the date of birth never leaves the service layer."""
    member_id: str
    on: date
    age_years: int


class DocumentOut(Strict):
    """Document metadata only; clinical free text (which names the patient) is not returned."""
    document_id: int
    document_type: str
    received_at: datetime
    is_complete: bool


class AuthRequestOut(Strict):
    request_number: str
    member_id: str
    enrollment_id: int
    requesting_provider_id: int
    service_state: str
    cpt_code: str
    primary_icd10: str
    units_requested: int
    urgency: str
    referral_on_file: bool
    submitted_at: datetime
    service_date: date
    status: str
    is_synthetic: bool
    documents: list[DocumentOut]


class LicenseOut(Strict):
    license_id: int
    state_code: str
    license_type: str
    status: str
    expiry_date: date | None


class NetworkOut(Strict):
    participation_id: int
    plan_id: int
    network_status: str
    effective_date: date
    end_date: date | None


class ProviderOut(Strict):
    provider_id: int
    npi: str
    display_name: str
    credential: str
    specialty: str
    specialty_group: str
    oig_excluded: bool
    license_in_state: LicenseOut | None
    network_for_plan: NetworkOut | None


class ProcedureOut(Strict):
    cpt_code: str
    description: str
    category: str
    requires_prior_auth: bool
    default_required_doc: str | None


class CoverageRuleOut(Strict):
    coverage_rule_id: int
    state_code: str
    insurance_category: str
    cpt_code: str
    is_covered: bool
    requires_prior_auth: bool
    required_document_type: str | None
    min_age: int | None
    max_age: int | None
    max_units_per_year: int | None
    effective_date: date
    end_date: date | None
    citation: str | None


class CriterionOut(Strict):
    criteria_id: int
    icd10_code: str
    diagnosis_description: str
    diagnosis_chapter: str
    criteria_source: str


class DiagnosisOut(Strict):
    icd10_code: str
    description: str
    chapter: str


class PolicyRulesOut(Strict):
    """Insurance-policy rules applicable to one service on one date."""
    procedure: ProcedureOut
    requested_diagnosis: DiagnosisOut
    coverage_rules: list[CoverageRuleOut]      # all rules effective on the date (more than one = conflict)
    clinical_criteria: list[CriterionOut]


# ---------------------------------------------------------- domain write DTOs
class ActionAuthorization(Strict):
    """Every internal write names the workflow run and the human approval that authorises it.
    The service layer re-verifies both before writing (see services/action_guard.py)."""
    workflow_run_id: str = Field(max_length=36)
    approval_id: int = Field(ge=1)


class DeterminationIn(ActionAuthorization):
    status: Literal["APPROVED", "DENIED"]
    denial_code: str | None = Field(default=None, pattern=r"^[A-Z]{2,3}-\d{2}$")   # denial, or partial approval
    units_approved: int = Field(ge=0)
    note: str = Field(max_length=2000)


class DeterminationOut(Strict):
    request_number: str
    previous_status: str
    status: str
    denial_code: str | None
    units_approved: int
    decision_at: datetime


class NoticeIn(ActionAuthorization):
    member_id: str
    request_number: str | None
    notice_type: Literal["PROVIDER_DETERMINATION_NOTICE", "DOCUMENT_REQUEST", "MEDICAL_DIRECTOR_ESCALATION"]
    body: str = Field(max_length=4000)


class NoticeOut(Strict):
    notice_id: int
    notice_type: str
    created_at: datetime


class ReviewerOut(Strict):
    reviewer_id: str
    display_name: str
    role: str
    is_active: bool


# -------------------------------------------------------------- workflow API
class RequestType(str, Enum):
    PRIOR_AUTH_REVIEW = "PRIOR_AUTH_REVIEW"   # review a submitted auth request
    COVERAGE_INQUIRY = "COVERAGE_INQUIRY"     # "would this service be covered?" (no submitted request)


class BusinessRequest(Strict):
    request_type: RequestType
    auth_request_number: str | None = Field(default=None, max_length=24)
    cpt_code: str | None = Field(default=None, max_length=8)
    icd10_code: str | None = Field(default=None, max_length=10)
    units: int | None = Field(default=None, ge=1, le=999)
    service_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class StartWorkflowIn(Strict):
    member_id: str = Field(min_length=1, max_length=16)
    request: BusinessRequest
    token_budget: int | None = Field(default=None, ge=0, le=1_000_000)


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_REVISION = "REQUEST_REVISION"


class DecisionIn(Strict):
    """The reviewer identity comes from the bearer token, never from the body."""
    decision: Decision
    recommendation_version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class LoginIn(Strict):
    reviewer_id: str = Field(max_length=16)
    passcode: str = Field(max_length=128)
