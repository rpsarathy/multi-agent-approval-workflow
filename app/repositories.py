"""Repository layer: the only module that issues queries against domain tables."""
from datetime import date, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import models as m


class DomainRepository:
    def __init__(self, session: Session):
        self.s = session

    # members / policy -----------------------------------------------------
    def member(self, member_id: str) -> m.Member | None:
        return self.s.get(m.Member, member_id)

    def enrollments_with_plans(self, member_id: str):
        stmt = (select(m.Enrollment, m.Plan, m.InsuranceProduct)
                .join(m.Plan, m.Enrollment.plan_id == m.Plan.plan_id)
                .join(m.InsuranceProduct, m.Plan.product_id == m.InsuranceProduct.product_id)
                .where(m.Enrollment.member_id == member_id)
                .order_by(m.Enrollment.enrollment_id))
        return self.s.execute(stmt).all()

    # requests ---------------------------------------------------------------
    def auth_request(self, request_number: str) -> m.AuthRequest | None:
        return self.s.get(m.AuthRequest, request_number)

    def documents(self, request_number: str) -> list[m.ClinicalDocument]:
        return list(self.s.scalars(select(m.ClinicalDocument)
                                   .where(m.ClinicalDocument.request_number == request_number)
                                   .order_by(m.ClinicalDocument.document_id)))

    # providers --------------------------------------------------------------
    def provider(self, provider_id: int) -> m.Provider | None:
        return self.s.get(m.Provider, provider_id)

    def license(self, provider_id: int, state_code: str) -> m.ProviderLicense | None:
        return self.s.scalar(select(m.ProviderLicense).where(m.ProviderLicense.provider_id == provider_id,
                                                             m.ProviderLicense.state_code == state_code))

    def network(self, provider_id: int, plan_id: int) -> m.ProviderNetwork | None:
        return self.s.scalar(select(m.ProviderNetwork).where(m.ProviderNetwork.provider_id == provider_id,
                                                             m.ProviderNetwork.plan_id == plan_id))

    # policy rules -----------------------------------------------------------
    def procedure(self, cpt_code: str) -> m.Procedure | None:
        return self.s.get(m.Procedure, cpt_code)

    def coverage_rules_effective(self, state: str, category: str, cpt: str, on: date) -> list[m.CoverageRule]:
        stmt = (select(m.CoverageRule)
                .where(m.CoverageRule.state_code == state, m.CoverageRule.insurance_category == category,
                       m.CoverageRule.cpt_code == cpt, m.CoverageRule.effective_date <= on,
                       or_(m.CoverageRule.end_date.is_(None), m.CoverageRule.end_date >= on))
                .order_by(m.CoverageRule.effective_date, m.CoverageRule.coverage_rule_id))
        return list(self.s.scalars(stmt))

    def clinical_criteria(self, cpt: str):
        stmt = (select(m.ClinicalCriterion, m.Diagnosis)
                .join(m.Diagnosis, and_(m.ClinicalCriterion.icd10_code == m.Diagnosis.icd10_code))
                .where(m.ClinicalCriterion.cpt_code == cpt)
                .order_by(m.ClinicalCriterion.criteria_id))
        return self.s.execute(stmt).all()

    # reviewers --------------------------------------------------------------
    def reviewer(self, reviewer_id: str) -> m.Reviewer | None:
        return self.s.get(m.Reviewer, reviewer_id)

    # writes -----------------------------------------------------------------
    def set_determination(self, req: m.AuthRequest, status: str, note: str, at: datetime) -> None:
        req.status, req.decision_note, req.decision_at = status, note, at

    def add_notice(self, notice: m.Notice) -> m.Notice:
        self.s.add(notice)
        self.s.flush()
        return notice
