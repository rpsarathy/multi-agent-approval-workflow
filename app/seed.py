"""Seed a small, deterministic, fully SYNTHETIC prior-authorization dataset.

Every identifier is prefixed `SYN-` and every row has `is_synthetic=True`.
The records are hand-written for this demo. They reuse *concepts* from the
reference `healthcare_db/build_db.py` (plans, enrollments, licensure, network,
coverage rules, clinical criteria, documents) but none of them was copied from
the reference database.

Each member is built to exercise one path through the workflow; see SCENARIOS.
"""
from datetime import date, datetime

from sqlalchemy import select

from app import models as m
from app.db import create_all, session_scope
from app.security import hash_passcode

D = date.fromisoformat

# Demo-only reviewer passcodes (synthetic accounts). Change them for anything beyond a local demo.
DEMO_PASSCODES = {"SYN-R01": "nurse-demo", "SYN-R02": "director-demo", "SYN-R03": "inactive-demo"}

SCENARIOS = {
    "SYN-M0001": ("SYN-PA-0001", "Clean request: eligible, licensed, in network, criteria met, documentation complete"),
    "SYN-M0002": ("SYN-PA-0002", "HMO specialist request without a PCP referral"),
    "SYN-M0003": ("SYN-PA-0003", "Conflicting policy rules: two overlapping coverage rules for the same service"),
    "SYN-M0004": ("SYN-PA-0004", "Requesting provider's license expired before the service date"),
    "SYN-M0005": ("SYN-PA-0005", "Out-of-network provider on an HMO plan"),
    "SYN-M0006": ("SYN-PA-0006", "Coverage terminates before the service date"),
    "SYN-M0007": ("SYN-PA-0007", "Missing data: license expiry unknown, clinical notes incomplete, diagnosis outside criteria"),
    "SYN-M0008": ("SYN-PA-0008", "Conflicting customer data: two overlapping active enrollments"),
    "SYN-M0009": (None, "Coverage inquiry: physical therapy units above the annual limit (partial approval, COV-04)"),
    "SYN-M0010": ("SYN-PA-0010", "Member below the minimum age in the coverage rule"),
}


def _rows():
    states = [
        m.State(state_code="CA", state_name="California", pa_decision_deadline_days=5),
        m.State(state_code="TX", state_name="Texas", pa_decision_deadline_days=3),
        m.State(state_code="NY", state_name="New York", pa_decision_deadline_days=3),
        m.State(state_code="FL", state_name="Florida", pa_decision_deadline_days=7),
    ]
    products = [
        m.InsuranceProduct(product_id=1, product_name="Commercial PPO", category="COMMERCIAL", network_model="PPO",
                           requires_referral=False, oon_coverage=True),
        m.InsuranceProduct(product_id=2, product_name="Commercial HMO", category="COMMERCIAL", network_model="HMO",
                           requires_referral=True, oon_coverage=False),
        m.InsuranceProduct(product_id=3, product_name="Medicare Advantage PPO", category="MEDICARE", network_model="PPO",
                           requires_referral=False, oon_coverage=True),
        m.InsuranceProduct(product_id=4, product_name="Medicaid Managed Care HMO", category="MEDICAID",
                           network_model="HMO", requires_referral=True, oon_coverage=False),
    ]
    plans = [
        m.Plan(plan_id=1, plan_code="SYN-CA-PPO-01", plan_name="Golden Shield PPO (synthetic)", payer_name="Synthetic Mutual",
               product_id=1, state_code="CA", effective_date=D("2020-01-01"), termination_date=None, is_active=True),
        m.Plan(plan_id=2, plan_code="SYN-CA-HMO-01", plan_name="Golden Shield HMO (synthetic)", payer_name="Synthetic Mutual",
               product_id=2, state_code="CA", effective_date=D("2020-01-01"), termination_date=None, is_active=True),
        m.Plan(plan_id=3, plan_code="SYN-TX-MA-01", plan_name="Lone Star Advantage (synthetic)", payer_name="Synthetic Senior Health",
               product_id=3, state_code="TX", effective_date=D("2021-01-01"), termination_date=None, is_active=True),
        m.Plan(plan_id=4, plan_code="SYN-NY-MCD-01", plan_name="Empire Community Care (synthetic)", payer_name="Synthetic Community Plan",
               product_id=4, state_code="NY", effective_date=D("2019-01-01"), termination_date=None, is_active=True),
        m.Plan(plan_id=5, plan_code="SYN-FL-PPO-01", plan_name="Sunshine Choice PPO (synthetic)", payer_name="Synthetic Mutual",
               product_id=1, state_code="FL", effective_date=D("2020-01-01"), termination_date=D("2026-10-31"), is_active=False),
    ]
    providers = [
        m.Provider(provider_id=1, npi="9000000011", display_name="Dr. Lena Marsh (synthetic)", credential="MD",
                   specialty="Family Medicine", specialty_group="Primary Care"),
        m.Provider(provider_id=2, npi="9000000029", display_name="Dr. Omar Vance (synthetic)", credential="MD",
                   specialty="Orthopaedic Surgery", specialty_group="Specialist"),
        m.Provider(provider_id=3, npi="9000000037", display_name="Dr. Priya Nandakumar (synthetic)", credential="MD",
                   specialty="Neurology", specialty_group="Specialist"),
        m.Provider(provider_id=4, npi="9000000045", display_name="Jamie Ortiz, NP (synthetic)", credential="NP",
                   specialty="Nurse Practitioner - Family", specialty_group="Primary Care"),
        m.Provider(provider_id=5, npi="9000000052", display_name="Dr. Felix Brandt (synthetic)", credential="MD",
                   specialty="Sleep Medicine", specialty_group="Specialist"),
        m.Provider(provider_id=6, npi="9000000060", display_name="Dr. Rosa Delgado (synthetic)", credential="MD",
                   specialty="Orthopaedic Surgery", specialty_group="Specialist"),
        m.Provider(provider_id=7, npi="9000000078", display_name="Dr. Theo Grant (synthetic)", credential="MD",
                   specialty="Bariatric Surgery", specialty_group="Specialist"),
        m.Provider(provider_id=8, npi="9000000086", display_name="Casey Lin, PT (synthetic)", credential="PT",
                   specialty="Physical Therapy", specialty_group="Rehabilitation"),
    ]
    licenses = [
        m.ProviderLicense(license_id=1, provider_id=1, state_code="CA", license_type="MD", status="ACTIVE", expiry_date=D("2028-06-30")),
        m.ProviderLicense(license_id=2, provider_id=2, state_code="CA", license_type="MD", status="ACTIVE", expiry_date=D("2028-03-31")),
        m.ProviderLicense(license_id=3, provider_id=3, state_code="TX", license_type="MD", status="EXPIRED", expiry_date=D("2026-01-31")),
        m.ProviderLicense(license_id=4, provider_id=4, state_code="CA", license_type="NP", status="ACTIVE", expiry_date=None),
        m.ProviderLicense(license_id=5, provider_id=5, state_code="NY", license_type="MD", status="ACTIVE", expiry_date=D("2027-12-31")),
        m.ProviderLicense(license_id=6, provider_id=6, state_code="TX", license_type="MD", status="ACTIVE", expiry_date=D("2028-09-30")),
        m.ProviderLicense(license_id=7, provider_id=7, state_code="CA", license_type="MD", status="ACTIVE", expiry_date=D("2027-10-31")),
        m.ProviderLicense(license_id=8, provider_id=8, state_code="CA", license_type="PT", status="ACTIVE", expiry_date=D("2027-05-31")),
        m.ProviderLicense(license_id=9, provider_id=1, state_code="FL", license_type="MD", status="ACTIVE", expiry_date=D("2027-08-31")),
    ]
    network = [
        m.ProviderNetwork(participation_id=1, provider_id=1, plan_id=1, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=2, provider_id=1, plan_id=2, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=3, provider_id=2, plan_id=1, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=4, provider_id=2, plan_id=2, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=5, provider_id=3, plan_id=3, network_status="IN_NETWORK", effective_date=D("2021-01-01")),
        m.ProviderNetwork(participation_id=6, provider_id=4, plan_id=1, network_status="IN_NETWORK", effective_date=D("2022-01-01")),
        m.ProviderNetwork(participation_id=7, provider_id=5, plan_id=4, network_status="TERMINATED", effective_date=D("2019-01-01"),
                          end_date=D("2025-12-31")),
        m.ProviderNetwork(participation_id=8, provider_id=6, plan_id=3, network_status="IN_NETWORK", effective_date=D("2021-01-01")),
        m.ProviderNetwork(participation_id=9, provider_id=7, plan_id=1, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=10, provider_id=7, plan_id=2, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=11, provider_id=8, plan_id=1, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
        m.ProviderNetwork(participation_id=12, provider_id=1, plan_id=5, network_status="IN_NETWORK", effective_date=D("2020-01-01")),
    ]
    procedures = [
        m.Procedure(cpt_code="72148", description="MRI lumbar spine without contrast", category="IMAGING",
                    requires_prior_auth=True, default_required_doc="CLINICAL_NOTES"),
        m.Procedure(cpt_code="27447", description="Total knee arthroplasty", category="SURGERY",
                    requires_prior_auth=True, default_required_doc="IMAGING_REPORT"),
        m.Procedure(cpt_code="70551", description="MRI brain without contrast", category="IMAGING",
                    requires_prior_auth=True, default_required_doc="CLINICAL_NOTES"),
        m.Procedure(cpt_code="95810", description="Polysomnography, attended", category="DIAGNOSTIC",
                    requires_prior_auth=True, default_required_doc="CLINICAL_NOTES"),
        m.Procedure(cpt_code="43644", description="Laparoscopic gastric bypass", category="SURGERY",
                    requires_prior_auth=True, default_required_doc="LETTER_OF_MEDICAL_NECESSITY"),
        m.Procedure(cpt_code="97110", description="Therapeutic exercise, each 15 minutes", category="THERAPY",
                    requires_prior_auth=False, default_required_doc=None),
    ]
    diagnoses = [
        m.Diagnosis(icd10_code="M51.26", description="Other intervertebral disc displacement, lumbar region", chapter="Musculoskeletal"),
        m.Diagnosis(icd10_code="M54.50", description="Low back pain, unspecified", chapter="Musculoskeletal"),
        m.Diagnosis(icd10_code="M17.11", description="Unilateral primary osteoarthritis, right knee", chapter="Musculoskeletal"),
        m.Diagnosis(icd10_code="G43.909", description="Migraine, unspecified, not intractable", chapter="Nervous system"),
        m.Diagnosis(icd10_code="G47.33", description="Obstructive sleep apnea", chapter="Nervous system"),
        m.Diagnosis(icd10_code="E66.01", description="Morbid (severe) obesity due to excess calories", chapter="Endocrine"),
        m.Diagnosis(icd10_code="M62.81", description="Muscle weakness (generalized)", chapter="Musculoskeletal"),
    ]
    # Same code scheme as the reference (internal code, CARC code); only the codes this workflow can emit.
    denial_reasons = [m.DenialReason(denial_code=c, carc_code=carc, category=cat, description=desc, appealable=ap)
                      for c, carc, cat, desc, ap in [
        ("ADM-01", "29", "ADMINISTRATIVE", "Time limit for filing has expired", True),
        ("COV-01", "96", "COVERAGE", "Non-covered charge", True),
        ("COV-03", "6", "COVERAGE", "Procedure inconsistent with patient age", True),
        ("COV-04", "119", "COVERAGE", "Benefit maximum for this period has been reached", True),
        ("DOC-02", "252", "DOCUMENTATION", "Required attachment/documentation not received", True),
        ("DOC-03", "226", "DOCUMENTATION", "Requested information not provided or insufficient", True),
        ("ELG-01", "27", "ELIGIBILITY", "Expenses incurred after coverage terminated", True),
        ("ELG-02", "26", "ELIGIBILITY", "Expenses incurred prior to coverage", True),
        ("ELG-04", "22", "ELIGIBILITY", "Care may be covered by another payer (COB)", True),
        ("LIC-01", "B7", "LICENSURE", "Provider not licensed/eligible in service state on date of service", True),
        ("LIC-02", "52", "LICENSURE", "Rendering/ordering provider license suspended or revoked", False),
        ("LIC-04", "B7", "LICENSURE", "Provider excluded from federal health programs (OIG)", False),
        ("MN-01", "50", "MEDICAL_NECESSITY", "Not deemed a medical necessity by the payer", True),
        ("MN-02", "11", "MEDICAL_NECESSITY", "Diagnosis inconsistent with the procedure", True),
        ("NET-01", "242", "NETWORK", "Services not provided by network/primary care providers", True),
        ("REF-01", "288", "REFERRAL", "Referral absent", True),
    ]]
    rules = [
        m.CoverageRule(coverage_rule_id=1, state_code="CA", insurance_category="COMMERCIAL", cpt_code="72148", is_covered=True,
                       requires_prior_auth=True, required_document_type="CLINICAL_NOTES", max_units_per_year=2,
                       effective_date=D("2020-01-01"), citation="SYN-CA advanced imaging rule 4.2"),
        m.CoverageRule(coverage_rule_id=2, state_code="CA", insurance_category="COMMERCIAL", cpt_code="27447", is_covered=True,
                       requires_prior_auth=True, required_document_type="IMAGING_REPORT", min_age=18,
                       effective_date=D("2020-01-01"), citation="SYN-CA orthopaedic surgery rule 7.1"),
        m.CoverageRule(coverage_rule_id=3, state_code="CA", insurance_category="COMMERCIAL", cpt_code="43644", is_covered=True,
                       requires_prior_auth=True, required_document_type="LETTER_OF_MEDICAL_NECESSITY", min_age=18,
                       effective_date=D("2020-01-01"), citation="SYN-CA bariatric mandate (synthetic)"),
        # Like the reference's CA rule for 97110: prior auth required, PT progress notes, annual unit cap.
        m.CoverageRule(coverage_rule_id=4, state_code="CA", insurance_category="COMMERCIAL", cpt_code="97110", is_covered=True,
                       requires_prior_auth=True, required_document_type="PT_PROGRESS_NOTES", max_units_per_year=20,
                       effective_date=D("2020-01-01"), citation="SYN-CA rehabilitation visit limit"),
        m.CoverageRule(coverage_rule_id=5, state_code="TX", insurance_category="MEDICARE", cpt_code="70551", is_covered=True,
                       requires_prior_auth=True, required_document_type="CLINICAL_NOTES",
                       effective_date=D("2021-01-01"), citation="SYN-MA neuro-imaging LCD"),
        # Two overlapping, contradictory rules for the same key -> policy-rule conflict.
        m.CoverageRule(coverage_rule_id=6, state_code="TX", insurance_category="MEDICARE", cpt_code="27447", is_covered=True,
                       requires_prior_auth=True, required_document_type="IMAGING_REPORT",
                       effective_date=D("2021-01-01"), citation="SYN-MA joint replacement LCD v1"),
        m.CoverageRule(coverage_rule_id=7, state_code="TX", insurance_category="MEDICARE", cpt_code="27447", is_covered=True,
                       requires_prior_auth=True, required_document_type="PT_PROGRESS_NOTES",
                       effective_date=D("2026-01-01"), citation="SYN-MA joint replacement LCD v2 (v1 not end-dated)"),
        m.CoverageRule(coverage_rule_id=8, state_code="NY", insurance_category="MEDICAID", cpt_code="95810", is_covered=True,
                       requires_prior_auth=True, required_document_type="CLINICAL_NOTES",
                       effective_date=D("2019-01-01"), citation="SYN-NY Medicaid sleep study policy"),
        m.CoverageRule(coverage_rule_id=9, state_code="FL", insurance_category="COMMERCIAL", cpt_code="72148", is_covered=True,
                       requires_prior_auth=True, required_document_type="CLINICAL_NOTES",
                       effective_date=D("2020-01-01"), citation="SYN-FL advanced imaging rule"),
    ]
    criteria = [
        m.ClinicalCriterion(criteria_id=1, cpt_code="72148", icd10_code="M51.26", criteria_source="SYN imaging guideline LS-01"),
        m.ClinicalCriterion(criteria_id=2, cpt_code="27447", icd10_code="M17.11", criteria_source="SYN orthopaedic guideline TKA-03"),
        m.ClinicalCriterion(criteria_id=3, cpt_code="70551", icd10_code="G43.909", criteria_source="SYN neuro-imaging guideline NI-02"),
        m.ClinicalCriterion(criteria_id=4, cpt_code="95810", icd10_code="G47.33", criteria_source="SYN sleep guideline PSG-01"),
        m.ClinicalCriterion(criteria_id=5, cpt_code="43644", icd10_code="E66.01", criteria_source="SYN bariatric guideline BS-05"),
        m.ClinicalCriterion(criteria_id=6, cpt_code="97110", icd10_code="M62.81", criteria_source="SYN rehab guideline PT-01"),
    ]
    members = [
        m.Member(member_id="SYN-M0001", first_name="Ava", last_name="Thornbury", date_of_birth=D("1980-04-12"), sex="F", state_code="CA", email="ava.t@example.invalid", phone="(415) 555-0101", address_line1="4152 Granite Blvd", city="Riverbend", zip="94016", ssn="912-34-5601"),
        m.Member(member_id="SYN-M0002", first_name="Ben", last_name="Okafor", date_of_birth=D("1958-09-03"), sex="M", state_code="CA", email="ben.o@example.invalid", phone="(415) 555-0102", address_line1="88 Juniper Rd", city="Coastview", zip="94110", ssn="912-34-5602"),
        m.Member(member_id="SYN-M0003", first_name="Carmen", last_name="Diaz", date_of_birth=D("1952-01-22"), sex="F", state_code="TX", email=None, phone="(512) 555-0103", address_line1="301 Mesquite Ln", city="Cedar Hollow", zip="73301", ssn="912-34-5603"),
        m.Member(member_id="SYN-M0004", first_name="Dev", last_name="Patel", date_of_birth=D("1955-06-30"), sex="M", state_code="TX", email="dev.p@example.invalid", phone="(512) 555-0104", address_line1="19 Pecan Ct", city="Cedar Hollow", zip="73344", ssn="912-34-5604"),
        m.Member(member_id="SYN-M0005", first_name="Elena", last_name="Rossi", date_of_birth=D("1990-11-15"), sex="F", state_code="NY", email="elena.r@example.invalid", phone="(718) 555-0105", address_line1="5 Harbor Way", city="Bayridge", zip="11209", ssn="912-34-5605"),
        m.Member(member_id="SYN-M0006", first_name="Farid", last_name="Haddad", date_of_birth=D("1975-02-08"), sex="M", state_code="FL", email="farid.h@example.invalid", phone="(305) 555-0106", address_line1="742 Palmetto Ave", city="Sunport", zip="33101", ssn="912-34-5606"),
        m.Member(member_id="SYN-M0007", first_name="Grace", last_name="Lindqvist", date_of_birth=D("1987-07-19"), sex="F", state_code="CA", email="grace.l@example.invalid", phone="(415) 555-0107", address_line1="1200 Quarry Dr", city="Riverbend", zip="94017", ssn="912-34-5607"),
        m.Member(member_id="SYN-M0008", first_name="Hiro", last_name="Tanaka", date_of_birth=D("1979-12-01"), sex="M", state_code="CA", email="hiro.t@example.invalid", phone="(415) 555-0108", address_line1="66 Cypress St", city="Coastview", zip="94111", ssn="912-34-5608"),
        m.Member(member_id="SYN-M0009", first_name="Isla", last_name="Moreau", date_of_birth=D("1993-03-27"), sex="F", state_code="CA", email="isla.m@example.invalid", phone="(415) 555-0109", address_line1="9 Laurel Pl", city="Riverbend", zip="94018", ssn="912-34-5609"),
        m.Member(member_id="SYN-M0010", first_name="Jonah", last_name="Kepler", date_of_birth=D("2010-05-05"), sex="M", state_code="CA", email=None, phone="(415) 555-0110", address_line1="27 Alder Rd", city="Coastview", zip="94112", ssn="912-34-5610"),
    ]
    enrollments = [
        m.Enrollment(enrollment_id=1, member_id="SYN-M0001", plan_id=1, relationship="SUBSCRIBER", coverage_start=D("2021-01-01"), status="ACTIVE", pcp_provider_id=1),
        m.Enrollment(enrollment_id=2, member_id="SYN-M0002", plan_id=2, relationship="SUBSCRIBER", coverage_start=D("2022-01-01"), status="ACTIVE", pcp_provider_id=1),
        m.Enrollment(enrollment_id=3, member_id="SYN-M0003", plan_id=3, relationship="SUBSCRIBER", coverage_start=D("2021-01-01"), status="ACTIVE", pcp_provider_id=None),
        m.Enrollment(enrollment_id=4, member_id="SYN-M0004", plan_id=3, relationship="SUBSCRIBER", coverage_start=D("2021-06-01"), status="ACTIVE", pcp_provider_id=None),
        m.Enrollment(enrollment_id=5, member_id="SYN-M0005", plan_id=4, relationship="SUBSCRIBER", coverage_start=D("2023-01-01"), status="ACTIVE", pcp_provider_id=None),
        m.Enrollment(enrollment_id=6, member_id="SYN-M0006", plan_id=5, relationship="SUBSCRIBER", coverage_start=D("2020-01-01"), coverage_end=D("2026-10-31"), status="TERMINATED", pcp_provider_id=1),
        m.Enrollment(enrollment_id=7, member_id="SYN-M0007", plan_id=1, relationship="SPOUSE", coverage_start=D("2022-01-01"), status="ACTIVE", pcp_provider_id=4),
        m.Enrollment(enrollment_id=8, member_id="SYN-M0008", plan_id=1, relationship="SUBSCRIBER", coverage_start=D("2021-01-01"), status="ACTIVE", pcp_provider_id=1),
        m.Enrollment(enrollment_id=9, member_id="SYN-M0008", plan_id=2, relationship="DEPENDENT", coverage_start=D("2025-01-01"), status="ACTIVE", pcp_provider_id=1),
        m.Enrollment(enrollment_id=10, member_id="SYN-M0009", plan_id=1, relationship="SUBSCRIBER", coverage_start=D("2024-01-01"), status="ACTIVE", pcp_provider_id=1),
        m.Enrollment(enrollment_id=11, member_id="SYN-M0010", plan_id=1, relationship="DEPENDENT", coverage_start=D("2015-01-01"), status="ACTIVE", pcp_provider_id=1),
    ]

    def req(n, member, enr, prov, st, cpt, dx, units=1, svc="2026-11-16", referral=False):
        return m.AuthRequest(request_number=n, member_id=member, enrollment_id=enr, requesting_provider_id=prov,
                             service_state=st, cpt_code=cpt, primary_icd10=dx, units_requested=units, urgency="STANDARD",
                             referral_on_file=referral, submitted_at=datetime(2026, 10, 19, 9, 30), service_date=D(svc),
                             status="PENDING")

    requests = [
        req("SYN-PA-0001", "SYN-M0001", 1, 2, "CA", "72148", "M51.26"),
        req("SYN-PA-0002", "SYN-M0002", 2, 2, "CA", "27447", "M17.11"),
        req("SYN-PA-0003", "SYN-M0003", 3, 6, "TX", "27447", "M17.11"),
        req("SYN-PA-0004", "SYN-M0004", 4, 3, "TX", "70551", "G43.909"),
        req("SYN-PA-0005", "SYN-M0005", 5, 5, "NY", "95810", "G47.33"),
        req("SYN-PA-0006", "SYN-M0006", 6, 1, "FL", "72148", "M51.26"),
        req("SYN-PA-0007", "SYN-M0007", 7, 4, "CA", "72148", "M54.50"),
        req("SYN-PA-0008", "SYN-M0008", 8, 7, "CA", "43644", "E66.01"),
        req("SYN-PA-0010", "SYN-M0010", 11, 7, "CA", "43644", "E66.01"),
    ]

    def doc(i, n, kind, complete=True):
        return m.ClinicalDocument(document_id=i, request_number=n, document_type=kind, received_at=datetime(2026, 10, 19, 10, 0),
                                  is_complete=complete, summary=f"Synthetic {kind.replace('_', ' ').lower()} for {n}.")

    documents = [
        doc(1, "SYN-PA-0001", "CLINICAL_NOTES"),
        doc(2, "SYN-PA-0002", "IMAGING_REPORT"),
        doc(3, "SYN-PA-0003", "IMAGING_REPORT"),
        doc(4, "SYN-PA-0004", "CLINICAL_NOTES"),
        doc(5, "SYN-PA-0005", "CLINICAL_NOTES"),
        doc(6, "SYN-PA-0006", "CLINICAL_NOTES"),
        doc(7, "SYN-PA-0007", "CLINICAL_NOTES", complete=False),
        doc(8, "SYN-PA-0008", "LETTER_OF_MEDICAL_NECESSITY"),
        doc(9, "SYN-PA-0010", "LETTER_OF_MEDICAL_NECESSITY"),
    ]
    def reviewer(rid, name, role, active=True):
        salt = f"salt-{rid}"
        return m.Reviewer(reviewer_id=rid, display_name=name, role=role, is_active=active, passcode_salt=salt,
                          passcode_hash=hash_passcode(DEMO_PASSCODES[rid], salt))

    # Clinical document summaries are free text and, like real ones, mention the patient. They stay in the
    # system of record: the internal API does not return document text to agents.
    for d_ in documents:
        mem = next(x for x in members if x.member_id == next(r for r in requests if r.request_number == d_.request_number).member_id)
        d_.summary += f" Patient {mem.first_name} {mem.last_name}, DOB {mem.date_of_birth:%m/%d/%Y}, tel {mem.phone}."

    reviewers = [
        reviewer("SYN-R01", "Jordan Avery, RN (synthetic)", "NURSE_REVIEWER"),
        reviewer("SYN-R02", "Dr. Sam Whitaker, MD (synthetic)", "MEDICAL_DIRECTOR"),
        reviewer("SYN-R03", "Alex Former, RN (synthetic, inactive)", "NURSE_REVIEWER", active=False),
    ]
    # Insert in FK-dependency order.
    return [states, products, plans, providers, licenses, network, procedures, diagnoses, denial_reasons, rules, criteria,
            members, enrollments, requests, documents, reviewers]


def seed(reset: bool = False) -> bool:
    """Create tables and insert the synthetic dataset if it is not present. Returns True if inserted."""
    from app.db import Base, get_engine
    if reset:
        Base.metadata.drop_all(get_engine())
    create_all()
    with session_scope() as s:
        if s.scalar(select(m.Member).limit(1)) is not None:
            return False
        for group in _rows():
            s.add_all(group)
            s.flush()
    return True


if __name__ == "__main__":
    import sys
    inserted = seed(reset="--reset" in sys.argv)
    print("Seeded synthetic dataset." if inserted else "Dataset already present (use --reset to recreate).")
