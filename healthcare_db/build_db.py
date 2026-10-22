#!/usr/bin/env python3
"""Build a fully synthetic prior-authorization / claims-review SQLite database.

All people, identifiers and organizations are fictional. SSNs use the 900-999
area range, which the SSA never issues. Request outcomes are derived from the
data by a rule engine (eligibility -> licensure -> scope -> network -> referral
-> coverage -> documentation -> medical necessity), then a small number of
reviewer errors and ambiguous edge cases are layered on top.
"""
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

SEED = 20250930
AS_OF = date(2025, 9, 30)  # "today" inside the dataset
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "healthcare.db")

rng = random.Random(SEED)

SCHEMA = """
CREATE TABLE states (
    state_code                  TEXT PRIMARY KEY,
    state_name                  TEXT NOT NULL,
    region                      TEXT NOT NULL,
    telehealth_parity           INTEGER NOT NULL CHECK (telehealth_parity IN (0,1)),
    imlc_member                 INTEGER NOT NULL CHECK (imlc_member IN (0,1)),
    np_full_practice_authority  INTEGER NOT NULL CHECK (np_full_practice_authority IN (0,1)),
    pa_decision_deadline_days   INTEGER NOT NULL
);

CREATE TABLE insurance_types (
    insurance_type_id      INTEGER PRIMARY KEY,
    type_name              TEXT NOT NULL UNIQUE,
    category               TEXT NOT NULL CHECK (category IN ('COMMERCIAL','MEDICARE','MEDICAID')),
    network_model          TEXT NOT NULL CHECK (network_model IN ('HMO','PPO','EPO','POS','FFS','PDP')),
    market_segment         TEXT NOT NULL,
    requires_referral      INTEGER NOT NULL CHECK (requires_referral IN (0,1)),
    oon_coverage           INTEGER NOT NULL CHECK (oon_coverage IN (0,1)),
    std_turnaround_days    INTEGER NOT NULL,
    exp_turnaround_hours   INTEGER NOT NULL
);

CREATE TABLE insurance_plans (
    plan_id            INTEGER PRIMARY KEY,
    plan_code          TEXT NOT NULL UNIQUE,
    plan_name          TEXT NOT NULL,
    payer_name         TEXT NOT NULL,
    insurance_type_id  INTEGER NOT NULL REFERENCES insurance_types(insurance_type_id),
    state_code         TEXT NOT NULL REFERENCES states(state_code),
    effective_date     TEXT NOT NULL,
    termination_date   TEXT,
    annual_deductible  REAL NOT NULL,
    oop_max            REAL NOT NULL,
    is_active          INTEGER NOT NULL CHECK (is_active IN (0,1))
);

CREATE TABLE specialties (
    specialty_id        INTEGER PRIMARY KEY,
    specialty_name      TEXT NOT NULL UNIQUE,
    taxonomy_code       TEXT NOT NULL UNIQUE,
    specialty_group     TEXT NOT NULL,
    requires_board_cert INTEGER NOT NULL CHECK (requires_board_cert IN (0,1))
);

CREATE TABLE facilities (
    facility_id     INTEGER PRIMARY KEY,
    facility_name   TEXT NOT NULL,
    facility_type   TEXT NOT NULL CHECK (facility_type IN
                      ('HOSPITAL','ASC','CLINIC','IMAGING_CENTER','SNF','INFUSION_CENTER','SLEEP_LAB','REHAB')),
    facility_npi    TEXT NOT NULL UNIQUE,
    address_line1   TEXT NOT NULL,
    city            TEXT NOT NULL,
    state_code      TEXT NOT NULL REFERENCES states(state_code),
    zip             TEXT NOT NULL,
    accreditation   TEXT,
    cms_certified   INTEGER NOT NULL CHECK (cms_certified IN (0,1))
);

CREATE TABLE providers (
    provider_id            INTEGER PRIMARY KEY,
    npi                    TEXT NOT NULL UNIQUE,
    first_name             TEXT NOT NULL,
    last_name              TEXT NOT NULL,
    credential             TEXT NOT NULL CHECK (credential IN ('MD','DO','NP','PA','DPM','DC','PT','PhD','LCSW')),
    specialty_id           INTEGER NOT NULL REFERENCES specialties(specialty_id),
    primary_facility_id    INTEGER REFERENCES facilities(facility_id),
    oig_excluded           INTEGER NOT NULL DEFAULT 0 CHECK (oig_excluded IN (0,1)),
    accepting_new_patients INTEGER NOT NULL CHECK (accepting_new_patients IN (0,1))
);

CREATE TABLE provider_licenses (
    license_id      INTEGER PRIMARY KEY,
    provider_id     INTEGER NOT NULL REFERENCES providers(provider_id),
    state_code      TEXT NOT NULL REFERENCES states(state_code),
    license_number  TEXT NOT NULL,
    license_type    TEXT NOT NULL,
    issue_date      TEXT NOT NULL,
    expiry_date     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('ACTIVE','EXPIRED','SUSPENDED','PROBATION','REVOKED')),
    via_compact     INTEGER NOT NULL DEFAULT 0 CHECK (via_compact IN (0,1)),
    dea_registered  INTEGER NOT NULL CHECK (dea_registered IN (0,1)),
    UNIQUE (provider_id, state_code)
);

CREATE TABLE procedure_codes (
    cpt_code             TEXT PRIMARY KEY,
    description          TEXT NOT NULL,
    category             TEXT NOT NULL,
    requires_prior_auth  INTEGER NOT NULL CHECK (requires_prior_auth IN (0,1)),
    default_required_doc TEXT,
    allowed_license_types TEXT NOT NULL,  -- default scope; state rules in license_scope_rules override
    typical_allowed_amt  REAL NOT NULL
);

CREATE TABLE license_scope_rules (
    scope_rule_id         INTEGER PRIMARY KEY,
    state_code            TEXT NOT NULL REFERENCES states(state_code),
    license_type          TEXT NOT NULL,
    procedure_category    TEXT NOT NULL,
    permitted             INTEGER NOT NULL CHECK (permitted IN (0,1)),
    supervision_required  INTEGER NOT NULL CHECK (supervision_required IN (0,1)),
    statute_reference     TEXT,
    UNIQUE (state_code, license_type, procedure_category)
);

CREATE TABLE diagnosis_codes (
    icd10_code   TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    chapter      TEXT NOT NULL,
    is_billable  INTEGER NOT NULL CHECK (is_billable IN (0,1))
);

CREATE TABLE drugs (
    drug_id                INTEGER PRIMARY KEY,
    ndc                    TEXT NOT NULL UNIQUE,
    brand_name             TEXT NOT NULL,
    generic_name           TEXT NOT NULL,
    drug_class             TEXT NOT NULL,
    formulary_tier         INTEGER CHECK (formulary_tier BETWEEN 1 AND 5),  -- NULL = non-formulary
    requires_prior_auth    INTEGER NOT NULL CHECK (requires_prior_auth IN (0,1)),
    step_therapy_required  INTEGER NOT NULL CHECK (step_therapy_required IN (0,1)),
    quantity_limit_30d     INTEGER,
    is_specialty           INTEGER NOT NULL CHECK (is_specialty IN (0,1)),
    dea_schedule           TEXT
);

CREATE TABLE clinical_criteria (
    criteria_id      INTEGER PRIMARY KEY,
    cpt_code         TEXT REFERENCES procedure_codes(cpt_code),
    drug_id          INTEGER REFERENCES drugs(drug_id),
    icd10_code       TEXT NOT NULL REFERENCES diagnosis_codes(icd10_code),
    criteria_source  TEXT NOT NULL,
    guideline_ref    TEXT,
    CHECK ((cpt_code IS NULL) <> (drug_id IS NULL))
);

CREATE TABLE coverage_rules (
    coverage_rule_id       INTEGER PRIMARY KEY,
    state_code             TEXT NOT NULL REFERENCES states(state_code),
    insurance_category     TEXT NOT NULL CHECK (insurance_category IN ('COMMERCIAL','MEDICARE','MEDICAID')),
    cpt_code               TEXT NOT NULL REFERENCES procedure_codes(cpt_code),
    is_covered             INTEGER NOT NULL CHECK (is_covered IN (0,1)),
    requires_prior_auth    INTEGER NOT NULL CHECK (requires_prior_auth IN (0,1)),
    required_document_type TEXT,
    min_age                INTEGER,
    max_age                INTEGER,
    max_units_per_year     INTEGER,
    effective_date         TEXT NOT NULL,
    end_date               TEXT,
    mandate_citation       TEXT,
    UNIQUE (state_code, insurance_category, cpt_code, effective_date)
);

CREATE TABLE denial_reason_codes (
    denial_code   TEXT PRIMARY KEY,
    carc_code     TEXT NOT NULL,
    category      TEXT NOT NULL,
    description   TEXT NOT NULL,
    appealable    INTEGER NOT NULL CHECK (appealable IN (0,1))
);

CREATE TABLE members (
    member_id      TEXT PRIMARY KEY,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    date_of_birth  TEXT NOT NULL,
    sex            TEXT NOT NULL CHECK (sex IN ('F','M','X')),
    ssn            TEXT NOT NULL UNIQUE CHECK (CAST(substr(ssn,1,3) AS INTEGER) BETWEEN 900 AND 999),
    address_line1  TEXT NOT NULL,
    address_line2  TEXT,
    city           TEXT NOT NULL,
    state_code     TEXT NOT NULL REFERENCES states(state_code),
    zip            TEXT NOT NULL,
    phone          TEXT,
    email          TEXT
);

CREATE TABLE member_enrollments (
    enrollment_id         INTEGER PRIMARY KEY,
    member_id             TEXT NOT NULL REFERENCES members(member_id),
    plan_id               INTEGER NOT NULL REFERENCES insurance_plans(plan_id),
    subscriber_member_id  TEXT REFERENCES members(member_id),
    relationship          TEXT NOT NULL CHECK (relationship IN ('SUBSCRIBER','SPOUSE','DEPENDENT')),
    coverage_start        TEXT NOT NULL,
    coverage_end          TEXT,
    status                TEXT NOT NULL CHECK (status IN ('ACTIVE','TERMINATED','PENDING_VERIFICATION')),
    pcp_provider_id       INTEGER REFERENCES providers(provider_id)
);

CREATE TABLE provider_network (
    participation_id  INTEGER PRIMARY KEY,
    provider_id       INTEGER NOT NULL REFERENCES providers(provider_id),
    plan_id           INTEGER NOT NULL REFERENCES insurance_plans(plan_id),
    network_status    TEXT NOT NULL CHECK (network_status IN ('IN_NETWORK','TERMINATED')),
    effective_date    TEXT NOT NULL,
    end_date          TEXT,
    UNIQUE (provider_id, plan_id)
);

CREATE TABLE reviewers (
    reviewer_id    INTEGER PRIMARY KEY,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    credential     TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN
                     ('INTAKE','NURSE_REVIEWER','PHARMACIST','MEDICAL_DIRECTOR','APPEALS_SPECIALIST')),
    license_state  TEXT REFERENCES states(state_code),
    hire_date      TEXT NOT NULL,
    is_active      INTEGER NOT NULL CHECK (is_active IN (0,1))
);

CREATE TABLE auth_requests (
    request_id              INTEGER PRIMARY KEY,
    request_number          TEXT NOT NULL UNIQUE,
    member_id               TEXT NOT NULL REFERENCES members(member_id),
    enrollment_id           INTEGER NOT NULL REFERENCES member_enrollments(enrollment_id),
    requesting_provider_id  INTEGER NOT NULL REFERENCES providers(provider_id),
    servicing_facility_id   INTEGER REFERENCES facilities(facility_id),
    service_state           TEXT NOT NULL REFERENCES states(state_code),
    request_type            TEXT NOT NULL CHECK (request_type IN ('PRIOR_AUTH','RETRO_AUTH','CONCURRENT','PHARMACY')),
    urgency                 TEXT NOT NULL CHECK (urgency IN ('STANDARD','EXPEDITED')),
    is_telehealth           INTEGER NOT NULL CHECK (is_telehealth IN (0,1)),
    referral_on_file        INTEGER NOT NULL CHECK (referral_on_file IN (0,1)),
    primary_icd10           TEXT NOT NULL REFERENCES diagnosis_codes(icd10_code),
    secondary_icd10         TEXT REFERENCES diagnosis_codes(icd10_code),
    submitted_at            TEXT NOT NULL,
    service_date            TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('APPROVED','DENIED','PENDING')),
    decision_at             TEXT,
    denial_code             TEXT REFERENCES denial_reason_codes(denial_code),
    assigned_reviewer_id    INTEGER REFERENCES reviewers(reviewer_id),
    CHECK (status <> 'PENDING' OR decision_at IS NULL)
);

CREATE TABLE request_line_items (
    line_id         INTEGER PRIMARY KEY,
    request_id      INTEGER NOT NULL REFERENCES auth_requests(request_id),
    line_number     INTEGER NOT NULL,
    cpt_code        TEXT REFERENCES procedure_codes(cpt_code),
    drug_id         INTEGER REFERENCES drugs(drug_id),
    units_requested INTEGER NOT NULL,
    units_approved  INTEGER,
    billed_amount   REAL NOT NULL,
    line_status     TEXT NOT NULL CHECK (line_status IN ('APPROVED','DENIED','PENDING')),
    denial_code     TEXT REFERENCES denial_reason_codes(denial_code),
    UNIQUE (request_id, line_number),
    CHECK ((cpt_code IS NULL) <> (drug_id IS NULL))
);

CREATE TABLE clinical_documents (
    document_id         INTEGER PRIMARY KEY,
    request_id          INTEGER NOT NULL REFERENCES auth_requests(request_id),
    document_type       TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    author_provider_id  INTEGER REFERENCES providers(provider_id),
    page_count          INTEGER,
    is_complete         INTEGER NOT NULL CHECK (is_complete IN (0,1)),
    summary             TEXT
);

CREATE TABLE review_history (
    event_id     INTEGER PRIMARY KEY,
    request_id   INTEGER NOT NULL REFERENCES auth_requests(request_id),
    reviewer_id  INTEGER REFERENCES reviewers(reviewer_id),   -- NULL = automated rules engine
    event_at     TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    notes        TEXT
);

CREATE TABLE appeals (
    appeal_id                 INTEGER PRIMARY KEY,
    request_id                INTEGER NOT NULL REFERENCES auth_requests(request_id),
    appeal_level              INTEGER NOT NULL CHECK (appeal_level IN (1,2,3)),  -- 3 = external IRO
    filed_by                  TEXT NOT NULL CHECK (filed_by IN ('MEMBER','PROVIDER','AUTHORIZED_REP')),
    filed_at                  TEXT NOT NULL,
    decided_at                TEXT,
    outcome                   TEXT NOT NULL CHECK (outcome IN ('PENDING','UPHELD','OVERTURNED','WITHDRAWN')),
    reviewer_id               INTEGER REFERENCES reviewers(reviewer_id),
    new_documentation         INTEGER NOT NULL CHECK (new_documentation IN (0,1)),
    rationale                 TEXT,
    UNIQUE (request_id, appeal_level)
);

CREATE TABLE payments (
    payment_id             INTEGER PRIMARY KEY,
    request_id             INTEGER NOT NULL REFERENCES auth_requests(request_id),
    line_id                INTEGER NOT NULL REFERENCES request_line_items(line_id),
    payee_provider_id      INTEGER NOT NULL REFERENCES providers(provider_id),
    payment_type           TEXT NOT NULL CHECK (payment_type IN ('ORIGINAL','ADJUSTMENT','INTEREST','RECOUPMENT')),
    paid_date              TEXT NOT NULL,
    allowed_amount         REAL NOT NULL,
    paid_amount            REAL NOT NULL,
    member_responsibility  REAL NOT NULL,
    payment_method         TEXT NOT NULL CHECK (payment_method IN ('EFT','CHECK','VIRTUAL_CARD')),
    trace_number           TEXT NOT NULL UNIQUE
);
"""


# ----------------------------------------------------------------- helpers
def d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s


def iso(x):
    return x.isoformat() if x is not None else None


def ts(day, hour=None, minute=None):
    hour = rng.randint(7, 18) if hour is None else hour
    minute = rng.randint(0, 59) if minute is None else minute
    return datetime(day.year, day.month, day.day, hour, minute).isoformat(sep=" ")


def rand_date(start, end):
    start, end = d(start), d(end)
    return start + timedelta(days=rng.randint(0, (end - start).days))


def age_on(dob, on):
    dob, on = d(dob), d(on)
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def luhn_npi(base9):
    """Return a 10-digit NPI with a valid Luhn check digit (80840 prefix)."""
    digits = [int(c) for c in "80840" + base9]
    total = 0
    for i, n in enumerate(reversed(digits)):
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return base9 + str((10 - total % 10) % 10)


# ============================================================ reference data
CORE_STATES = ["CA", "TX", "FL", "NY", "PA", "OH", "GA", "NC", "IL", "AZ"]

# code, name, region, telehealth_parity, imlc_member, np_full_practice, PA deadline days
STATES = [
    ("AL", "Alabama", "South", 0, 1, 0, 15), ("AK", "Alaska", "West", 1, 0, 1, 15),
    ("AZ", "Arizona", "West", 1, 1, 1, 14), ("AR", "Arkansas", "South", 1, 0, 0, 15),
    ("CA", "California", "West", 1, 0, 0, 5), ("CO", "Colorado", "West", 1, 1, 1, 7),
    ("CT", "Connecticut", "Northeast", 1, 1, 1, 15), ("DE", "Delaware", "South", 1, 1, 1, 15),
    ("FL", "Florida", "South", 0, 0, 0, 7), ("GA", "Georgia", "South", 1, 1, 0, 15),
    ("HI", "Hawaii", "West", 1, 0, 1, 15), ("ID", "Idaho", "West", 0, 1, 1, 15),
    ("IL", "Illinois", "Midwest", 1, 1, 1, 5), ("IN", "Indiana", "Midwest", 0, 1, 0, 7),
    ("IA", "Iowa", "Midwest", 1, 1, 1, 15), ("KS", "Kansas", "Midwest", 1, 1, 1, 15),
    ("KY", "Kentucky", "South", 1, 1, 0, 5), ("LA", "Louisiana", "South", 1, 1, 0, 15),
    ("ME", "Maine", "Northeast", 1, 1, 1, 15), ("MD", "Maryland", "South", 1, 1, 1, 15),
    ("MA", "Massachusetts", "Northeast", 1, 0, 1, 14), ("MI", "Michigan", "Midwest", 1, 1, 0, 15),
    ("MN", "Minnesota", "Midwest", 1, 1, 1, 10), ("MS", "Mississippi", "South", 1, 1, 0, 15),
    ("MO", "Missouri", "Midwest", 0, 0, 0, 15), ("MT", "Montana", "West", 1, 1, 1, 15),
    ("NE", "Nebraska", "Midwest", 1, 1, 1, 15), ("NV", "Nevada", "West", 1, 1, 1, 15),
    ("NH", "New Hampshire", "Northeast", 1, 1, 1, 14), ("NJ", "New Jersey", "Northeast", 1, 1, 0, 15),
    ("NM", "New Mexico", "West", 1, 0, 1, 7), ("NY", "New York", "Northeast", 1, 0, 1, 3),
    ("NC", "North Carolina", "South", 0, 0, 0, 15), ("ND", "North Dakota", "Midwest", 1, 1, 1, 15),
    ("OH", "Ohio", "Midwest", 1, 1, 0, 10), ("OK", "Oklahoma", "South", 1, 1, 0, 15),
    ("OR", "Oregon", "West", 1, 0, 1, 15), ("PA", "Pennsylvania", "Northeast", 1, 1, 0, 15),
    ("RI", "Rhode Island", "Northeast", 1, 0, 1, 15), ("SC", "South Carolina", "South", 0, 0, 0, 15),
    ("SD", "South Dakota", "Midwest", 1, 1, 1, 15), ("TN", "Tennessee", "South", 1, 1, 0, 7),
    ("TX", "Texas", "South", 1, 1, 0, 3), ("UT", "Utah", "West", 1, 1, 1, 15),
    ("VT", "Vermont", "Northeast", 1, 1, 1, 14), ("VA", "Virginia", "South", 1, 0, 0, 14),
    ("WA", "Washington", "West", 1, 1, 1, 5), ("WV", "West Virginia", "South", 1, 1, 1, 15),
    ("WI", "Wisconsin", "Midwest", 1, 1, 0, 15), ("WY", "Wyoming", "West", 1, 1, 1, 15),
]


def build_insurance_types():
    rows, i = [], 1
    #                 model  referral oon
    commercial = [("HMO", 1, 0), ("PPO", 0, 1), ("EPO", 0, 0), ("POS", 1, 1), ("PPO", 0, 1)]
    labels = ["HMO", "PPO", "EPO", "POS", "HDHP-PPO"]
    for (model, ref, oon), label in zip(commercial, labels):
        for seg in ["Individual", "Small Group", "Large Group", "Self-Funded ASO"]:
            rows.append((i, f"Commercial {label} - {seg}", "COMMERCIAL", model, seg, ref, oon, 15, 72))
            i += 1
    for name, model, ref, oon in [("Medicare Advantage HMO", "HMO", 1, 0),
                                  ("Medicare Advantage PPO", "PPO", 0, 1),
                                  ("Medicare Advantage D-SNP", "HMO", 1, 0),
                                  ("Medicare Advantage C-SNP", "HMO", 1, 0),
                                  ("Medicare Part D PDP", "PDP", 0, 1)]:
        rows.append((i, name, "MEDICARE", model, "Medicare", ref, oon, 14, 72))
        i += 1
    for name, model, ref in [("Medicaid Managed Care HMO", "HMO", 1),
                             ("Medicaid Expansion MCO", "HMO", 1),
                             ("CHIP Managed Care", "HMO", 1),
                             ("Medicaid LTSS", "HMO", 0),
                             ("Medicaid Fee-for-Service", "FFS", 0)]:
        rows.append((i, name, "MEDICAID", model, "Medicaid", ref, 0, 14, 72))
        i += 1
    return rows


# cpt, description, category, requires_pa, default_doc, allowed_license_types, allowed_amt
PROCEDURES = [
    ("70551", "MRI brain without contrast", "IMAGING", 1, "CLINICAL_NOTES", "MD,DO,NP,PA", 620),
    ("70553", "MRI brain without and with contrast", "IMAGING", 1, "CLINICAL_NOTES", "MD,DO,NP,PA", 890),
    ("72148", "MRI lumbar spine without contrast", "IMAGING", 1, "CLINICAL_NOTES", "MD,DO,NP,PA", 640),
    ("73721", "MRI lower extremity joint without contrast", "IMAGING", 1, "CLINICAL_NOTES", "MD,DO,NP,PA,DPM", 600),
    ("74177", "CT abdomen and pelvis with contrast", "IMAGING", 1, "CLINICAL_NOTES", "MD,DO,NP,PA", 480),
    ("78815", "PET/CT imaging, skull base to mid-thigh", "IMAGING", 1, "IMAGING_REPORT", "MD,DO", 1650),
    ("27447", "Total knee arthroplasty", "ORTHO_SURGERY", 1, "IMAGING_REPORT", "MD,DO", 14500),
    ("27130", "Total hip arthroplasty", "ORTHO_SURGERY", 1, "IMAGING_REPORT", "MD,DO", 15200),
    ("29881", "Knee arthroscopy with meniscectomy", "ORTHO_SURGERY", 1, "IMAGING_REPORT", "MD,DO", 4200),
    ("63030", "Lumbar laminotomy, single interspace", "SPINE_SURGERY", 1, "IMAGING_REPORT", "MD,DO", 9800),
    ("22612", "Lumbar arthrodesis, posterior technique", "SPINE_SURGERY", 1, "PT_PROGRESS_NOTES", "MD,DO", 28500),
    ("64483", "Transforaminal epidural steroid injection, lumbar", "PAIN_MGMT", 1, "CLINICAL_NOTES", "MD,DO", 780),
    ("20610", "Arthrocentesis/injection, major joint", "ORTHO_PROCEDURE", 0, None, "MD,DO,NP,PA,DPM", 120),
    ("43644", "Laparoscopic gastric bypass (Roux-en-Y)", "BARIATRIC", 1, "LETTER_OF_MEDICAL_NECESSITY", "MD,DO", 22000),
    ("47562", "Laparoscopic cholecystectomy", "GENERAL_SURGERY", 0, None, "MD,DO", 6800),
    ("19318", "Breast reduction mammaplasty", "PLASTIC_SURGERY", 1, "CLINICAL_PHOTOS", "MD,DO", 8900),
    ("15823", "Blepharoplasty, upper eyelid, with excessive skin", "PLASTIC_SURGERY", 1, "VISUAL_FIELD_TEST", "MD,DO", 3200),
    ("93306", "Transthoracic echocardiogram, complete", "CARDIOLOGY_DX", 0, None, "MD,DO,NP,PA", 520),
    ("93458", "Left heart catheterization with coronary angiography", "CARDIOLOGY_INTERVENTION", 1, "CLINICAL_NOTES", "MD,DO", 4100),
    ("33208", "Permanent pacemaker insertion, dual chamber", "CARDIOLOGY_INTERVENTION", 1, "CLINICAL_NOTES", "MD,DO", 11200),
    ("95810", "Polysomnography, attended, in-lab", "SLEEP", 1, "CLINICAL_NOTES", "MD,DO,NP,PA", 1150),
    ("95806", "Home sleep apnea test", "SLEEP", 0, None, "MD,DO,NP,PA", 240),
    ("E0601", "CPAP device", "DME", 1, "SLEEP_STUDY", "MD,DO,NP,PA", 850),
    ("K0861", "Power wheelchair, group 3, single power option", "DME", 1, "LETTER_OF_MEDICAL_NECESSITY", "MD,DO,NP,PA", 18500),
    ("E0784", "External ambulatory insulin infusion pump", "DME", 1, "LAB_RESULTS", "MD,DO,NP,PA", 6200),
    ("97110", "Therapeutic exercise, each 15 minutes", "PHYSICAL_THERAPY", 1, "PT_PROGRESS_NOTES", "PT,MD,DO", 38),
    ("97140", "Manual therapy techniques, each 15 minutes", "PHYSICAL_THERAPY", 1, "PT_PROGRESS_NOTES", "PT,MD,DO,DC", 35),
    ("97161", "Physical therapy evaluation, low complexity", "PHYSICAL_THERAPY", 0, None, "PT", 95),
    ("90837", "Psychotherapy, 60 minutes", "BEHAVIORAL", 0, None, "MD,DO,NP,PhD,LCSW", 160),
    ("96130", "Psychological testing evaluation, first hour", "PSYCH_TESTING", 1, "CLINICAL_NOTES", "PhD,MD", 210),
    ("97153", "Adaptive behavior treatment by protocol (ABA), 15 min", "ABA", 1, "TREATMENT_PLAN", "PhD,MD", 32),
    ("96413", "Chemotherapy IV infusion, first hour", "INFUSION", 1, "TREATMENT_PLAN", "MD,DO", 340),
    ("77385", "Intensity-modulated radiation therapy delivery", "RADIATION_ONCOLOGY", 1, "TREATMENT_PLAN", "MD,DO", 520),
    ("45378", "Colonoscopy, diagnostic", "GI_ENDOSCOPY", 0, None, "MD,DO", 1100),
    ("43239", "Upper GI endoscopy with biopsy", "GI_ENDOSCOPY", 0, None, "MD,DO", 950),
    ("81162", "BRCA1/BRCA2 full sequence and deletion analysis", "GENETIC_TESTING", 1, "GENETIC_COUNSELING_NOTE", "MD,DO,NP", 2400),
    ("99214", "Office visit, established patient, moderate", "EVALUATION_MGMT", 0, None, "MD,DO,NP,PA,DPM,DC", 130),
    ("99204", "Office visit, new patient, moderate", "EVALUATION_MGMT", 0, None, "MD,DO,NP,PA,DPM", 190),
    ("28296", "Hallux valgus correction with metatarsal osteotomy", "PODIATRY_SURGERY", 1, "IMAGING_REPORT", "DPM,MD,DO", 3600),
    ("98941", "Chiropractic manipulative treatment, 3-4 regions", "CHIROPRACTIC", 1, "CLINICAL_NOTES", "DC", 55),
]

# icd10, description, chapter
DIAGNOSES = [
    ("G43.909", "Migraine, unspecified, not intractable", "Nervous system"),
    ("G43.719", "Chronic migraine without aura, intractable", "Nervous system"),
    ("G35", "Multiple sclerosis", "Nervous system"),
    ("R51.9", "Headache, unspecified", "Symptoms"),
    ("C71.9", "Malignant neoplasm of brain, unspecified", "Neoplasms"),
    ("M54.16", "Radiculopathy, lumbar region", "Musculoskeletal"),
    ("M54.50", "Low back pain, unspecified", "Musculoskeletal"),
    ("M51.26", "Other intervertebral disc displacement, lumbar", "Musculoskeletal"),
    ("M48.061", "Spinal stenosis, lumbar, without neurogenic claudication", "Musculoskeletal"),
    ("M17.11", "Unilateral primary osteoarthritis, right knee", "Musculoskeletal"),
    ("M16.11", "Unilateral primary osteoarthritis, right hip", "Musculoskeletal"),
    ("S83.241A", "Other tear of medial meniscus, right knee, initial", "Injury"),
    ("E66.01", "Morbid (severe) obesity due to excess calories", "Endocrine"),
    ("Z68.41", "BMI 40.0-44.9, adult", "Factors influencing health"),
    ("E11.65", "Type 2 diabetes mellitus with hyperglycemia", "Endocrine"),
    ("E10.9", "Type 1 diabetes mellitus without complications", "Endocrine"),
    ("K80.20", "Calculus of gallbladder without obstruction", "Digestive"),
    ("N62", "Hypertrophy of breast", "Genitourinary"),
    ("H02.831", "Dermatochalasis of right upper eyelid", "Eye"),
    ("I25.10", "Atherosclerotic heart disease of native coronary artery", "Circulatory"),
    ("I20.9", "Angina pectoris, unspecified", "Circulatory"),
    ("I44.2", "Atrioventricular block, complete", "Circulatory"),
    ("I49.5", "Sick sinus syndrome", "Circulatory"),
    ("G47.33", "Obstructive sleep apnea", "Nervous system"),
    ("R06.83", "Snoring", "Symptoms"),
    ("G12.21", "Amyotrophic lateral sclerosis", "Nervous system"),
    ("F33.1", "Major depressive disorder, recurrent, moderate", "Mental/behavioral"),
    ("F84.0", "Autistic disorder", "Mental/behavioral"),
    ("F90.2", "ADHD, combined type", "Mental/behavioral"),
    ("C50.911", "Malignant neoplasm of right female breast, unspecified site", "Neoplasms"),
    ("C34.90", "Malignant neoplasm of unspecified bronchus or lung", "Neoplasms"),
    ("Z80.3", "Family history of malignant neoplasm of breast", "Factors influencing health"),
    ("K21.9", "Gastro-esophageal reflux disease without esophagitis", "Digestive"),
    ("Z12.11", "Encounter for screening for malignant neoplasm of colon", "Factors influencing health"),
    ("M21.611", "Bunion of right foot", "Musculoskeletal"),
    ("M54.2", "Cervicalgia", "Musculoskeletal"),
    ("L40.0", "Psoriasis vulgaris", "Skin"),
    ("M05.79", "Rheumatoid arthritis with rheumatoid factor, multiple sites", "Musculoskeletal"),
    ("K50.90", "Crohn's disease, unspecified, without complications", "Digestive"),
    ("E78.01", "Familial hypercholesterolemia", "Endocrine"),
    ("M54", "Dorsalgia (category header, not billable)", "Musculoskeletal"),
]

# brand, generic, class, tier(None=non-formulary), pa, step, qty_limit, specialty, schedule
DRUGS = [
    ("Humira", "adalimumab", "TNF inhibitor", 5, 1, 1, 2, 1, None),
    ("Hyrimoz", "adalimumab-adaz", "TNF inhibitor (biosimilar)", 4, 1, 0, 2, 1, None),
    ("Enbrel", "etanercept", "TNF inhibitor", 5, 1, 1, 4, 1, None),
    ("Stelara", "ustekinumab", "IL-12/23 inhibitor", 5, 1, 1, 1, 1, None),
    ("Dupixent", "dupilumab", "IL-4R antagonist", 5, 1, 0, 2, 1, None),
    ("Ozempic", "semaglutide", "GLP-1 receptor agonist", 3, 1, 1, 1, 0, None),
    ("Wegovy", "semaglutide", "GLP-1 receptor agonist (weight)", None, 1, 0, 4, 0, None),
    ("Mounjaro", "tirzepatide", "GIP/GLP-1 receptor agonist", 3, 1, 1, 4, 0, None),
    ("Zepbound", "tirzepatide", "GIP/GLP-1 receptor agonist (weight)", None, 1, 0, 4, 0, None),
    ("Glumetza", "metformin ER", "Biguanide", 1, 0, 0, None, 0, None),
    ("Lantus", "insulin glargine", "Long-acting insulin", 2, 0, 0, None, 0, None),
    ("Jardiance", "empagliflozin", "SGLT2 inhibitor", 3, 0, 1, 30, 0, None),
    ("Lipitor", "atorvastatin", "Statin", 1, 0, 0, None, 0, None),
    ("Crestor", "rosuvastatin", "Statin", 1, 0, 0, None, 0, None),
    ("Repatha", "evolocumab", "PCSK9 inhibitor", 4, 1, 1, 2, 1, None),
    ("Eliquis", "apixaban", "Factor Xa inhibitor", 3, 0, 0, 60, 0, None),
    ("Keytruda", "pembrolizumab", "PD-1 inhibitor", 5, 1, 0, None, 1, None),
    ("Herceptin", "trastuzumab", "HER2 inhibitor", 5, 1, 0, None, 1, None),
    ("Ocrevus", "ocrelizumab", "CD20 monoclonal antibody", 5, 1, 0, None, 1, None),
    ("Imitrex", "sumatriptan", "Triptan", 1, 0, 0, 9, 0, None),
    ("Aimovig", "erenumab", "CGRP antagonist", 4, 1, 1, 1, 1, None),
    ("Nurtec ODT", "rimegepant", "CGRP antagonist", 4, 1, 1, 16, 0, None),
    ("Zoloft", "sertraline", "SSRI", 1, 0, 0, None, 0, None),
    ("Abilify", "aripiprazole", "Atypical antipsychotic", 2, 0, 0, 30, 0, None),
    ("Spravato", "esketamine", "NMDA antagonist (REMS)", 5, 1, 1, 8, 1, "CIII"),
    ("Concerta", "methylphenidate ER", "CNS stimulant", 2, 0, 0, 30, 0, "CII"),
    ("Vyvanse", "lisdexamfetamine", "CNS stimulant", 3, 1, 1, 30, 0, "CII"),
    ("OxyContin", "oxycodone ER", "Opioid agonist", 3, 1, 0, 60, 0, "CII"),
    ("Suboxone", "buprenorphine/naloxone", "Opioid partial agonist", 2, 0, 0, 90, 0, "CIII"),
    ("ProAir HFA", "albuterol", "Short-acting beta agonist", 1, 0, 0, 2, 0, None),
    ("Advair Diskus", "fluticasone/salmeterol", "ICS/LABA", 2, 0, 0, 1, 0, None),
    ("Xolair", "omalizumab", "Anti-IgE monoclonal antibody", 5, 1, 1, 2, 1, None),
    ("Otezla", "apremilast", "PDE4 inhibitor", 4, 1, 1, 60, 1, None),
    ("Xeljanz", "tofacitinib", "JAK inhibitor", 5, 1, 1, 60, 1, None),
    ("Rilutek", "riluzole", "Glutamate inhibitor", 2, 0, 0, 60, 0, None),
]

# (cpt or None, drug brand or None, icd10, source)
CRITERIA = [
    ("70551", None, "G43.719", "MCG"), ("70551", None, "G35", "MCG"), ("70553", None, "C71.9", "MCG"),
    ("70553", None, "G35", "MCG"), ("72148", None, "M54.16", "InterQual"), ("72148", None, "M51.26", "InterQual"),
    ("73721", None, "S83.241A", "InterQual"), ("74177", None, "K80.20", "MCG"), ("78815", None, "C34.90", "NCCN"),
    ("78815", None, "C50.911", "NCCN"), ("27447", None, "M17.11", "InterQual"), ("27130", None, "M16.11", "InterQual"),
    ("29881", None, "S83.241A", "InterQual"), ("63030", None, "M51.26", "InterQual"), ("63030", None, "M48.061", "InterQual"),
    ("22612", None, "M48.061", "InterQual"), ("64483", None, "M54.16", "MCG"), ("43644", None, "E66.01", "ASMBS"),
    ("19318", None, "N62", "Internal"), ("15823", None, "H02.831", "Internal"), ("93458", None, "I20.9", "ACC/AHA"),
    ("93458", None, "I25.10", "ACC/AHA"), ("33208", None, "I44.2", "ACC/AHA"), ("33208", None, "I49.5", "ACC/AHA"),
    ("95810", None, "G47.33", "AASM"), ("E0601", None, "G47.33", "CMS LCD"), ("K0861", None, "G12.21", "CMS LCD"),
    ("E0784", None, "E10.9", "CMS LCD"), ("97110", None, "M54.16", "Internal"), ("97140", None, "M54.2", "Internal"),
    ("96130", None, "F90.2", "Internal"), ("97153", None, "F84.0", "BACB"), ("96413", None, "C50.911", "NCCN"),
    ("77385", None, "C34.90", "NCCN"), ("81162", None, "Z80.3", "NCCN"), ("81162", None, "C50.911", "NCCN"),
    ("28296", None, "M21.611", "Internal"), ("98941", None, "M54.2", "Internal"),
    (None, "Humira", "M05.79", "Internal P&T"), (None, "Humira", "K50.90", "Internal P&T"),
    (None, "Stelara", "K50.90", "Internal P&T"), (None, "Enbrel", "L40.0", "Internal P&T"),
    (None, "Otezla", "L40.0", "Internal P&T"), (None, "Ozempic", "E11.65", "Internal P&T"),
    (None, "Mounjaro", "E11.65", "Internal P&T"), (None, "Wegovy", "E66.01", "Internal P&T"),
    (None, "Repatha", "E78.01", "Internal P&T"), (None, "Aimovig", "G43.719", "Internal P&T"),
    (None, "Ocrevus", "G35", "Internal P&T"), (None, "Vyvanse", "F90.2", "Internal P&T"),
]

# code, CARC/NCPDP, category, description, appealable
DENIAL_CODES = [
    ("ELG-01", "27", "ELIGIBILITY", "Expenses incurred after coverage terminated", 1),
    ("ELG-02", "26", "ELIGIBILITY", "Expenses incurred prior to coverage", 1),
    ("ELG-03", "31", "ELIGIBILITY", "Patient cannot be identified as our insured", 1),
    ("ELG-04", "22", "ELIGIBILITY", "Care may be covered by another payer (COB)", 1),
    ("NET-01", "242", "NETWORK", "Services not provided by network/primary care providers", 1),
    ("NET-02", "38", "NETWORK", "Services not provided or authorized by designated providers", 1),
    ("REF-01", "288", "REFERRAL", "Referral absent", 1),
    ("LIC-01", "B7", "LICENSURE", "Provider not licensed/eligible in service state on date of service", 1),
    ("LIC-02", "52", "LICENSURE", "Rendering/ordering provider license suspended or revoked", 0),
    ("LIC-03", "185", "LICENSURE", "Service outside the provider's licensed scope of practice in this state", 1),
    ("LIC-04", "B7", "LICENSURE", "Provider excluded from federal health programs (OIG)", 0),
    ("COV-01", "96", "COVERAGE", "Non-covered charge", 1),
    ("COV-02", "204", "COVERAGE", "Service not covered under the member's current benefit plan", 1),
    ("COV-03", "6", "COVERAGE", "Procedure inconsistent with patient age", 1),
    ("COV-04", "119", "COVERAGE", "Benefit maximum for this period has been reached", 1),
    ("COV-05", "167", "COVERAGE", "Diagnosis is not covered", 1),
    ("MN-01", "50", "MEDICAL_NECESSITY", "Not deemed a medical necessity by the payer", 1),
    ("MN-02", "11", "MEDICAL_NECESSITY", "Diagnosis inconsistent with the procedure", 1),
    ("MN-03", "55", "MEDICAL_NECESSITY", "Experimental/investigational", 1),
    ("MN-04", "56", "MEDICAL_NECESSITY", "Treatment not proven effective for this indication", 1),
    ("DOC-01", "16", "DOCUMENTATION", "Claim/request lacks information needed for adjudication", 1),
    ("DOC-02", "252", "DOCUMENTATION", "Required attachment/documentation not received", 1),
    ("DOC-03", "226", "DOCUMENTATION", "Requested information not provided or insufficient", 1),
    ("PA-01", "197", "AUTHORIZATION", "Precertification/authorization absent", 1),
    ("PA-02", "15", "AUTHORIZATION", "Authorization number missing or invalid", 1),
    ("PA-03", "39", "AUTHORIZATION", "Services denied at time authorization was requested", 1),
    ("ADM-01", "29", "ADMINISTRATIVE", "Time limit for filing has expired", 1),
    ("ADM-02", "18", "ADMINISTRATIVE", "Exact duplicate request", 0),
    ("RX-01", "NCPDP-70", "PHARMACY", "Product not on formulary", 1),
    ("RX-02", "NCPDP-608", "PHARMACY", "Step therapy requirements not met", 1),
    ("RX-03", "NCPDP-76", "PHARMACY", "Plan quantity limit exceeded", 1),
    ("RX-04", "NCPDP-88", "PHARMACY", "Drug utilization review conflict", 1),
]

SPECIALTIES = [
    ("Family Medicine", "207Q00000X", "Primary Care", 1), ("Internal Medicine", "207R00000X", "Primary Care", 1),
    ("Pediatrics", "208000000X", "Primary Care", 1), ("Orthopaedic Surgery", "207X00000X", "Surgical", 1),
    ("Neurological Surgery", "207T00000X", "Surgical", 1), ("General Surgery", "208600000X", "Surgical", 1),
    ("Plastic Surgery", "208200000X", "Surgical", 1), ("Bariatric Surgery", "2086S0102X", "Surgical", 1),
    ("Cardiology", "207RC0000X", "Medical", 1), ("Interventional Cardiology", "207RI0011X", "Medical", 1),
    ("Neurology", "2084N0400X", "Medical", 1), ("Gastroenterology", "207RG0100X", "Medical", 1),
    ("Medical Oncology", "207RX0202X", "Medical", 1), ("Radiation Oncology", "2085R0001X", "Medical", 1),
    ("Rheumatology", "207RR0500X", "Medical", 1), ("Endocrinology", "207RE0101X", "Medical", 1),
    ("Dermatology", "207N00000X", "Medical", 1), ("Sleep Medicine", "207RS0012X", "Medical", 1),
    ("Pain Medicine", "208VP0014X", "Medical", 1), ("Psychiatry", "2084P0800X", "Behavioral", 1),
    ("Clinical Psychology", "103TC0700X", "Behavioral", 0), ("Clinical Social Work", "1041C0700X", "Behavioral", 0),
    ("Behavior Analysis", "103K00000X", "Behavioral", 0), ("Physical Therapy", "225100000X", "Rehab", 0),
    ("Chiropractic", "111N00000X", "Rehab", 0), ("Podiatry", "213E00000X", "Surgical", 1),
    ("Ophthalmology", "207W00000X", "Surgical", 1), ("Nurse Practitioner - Family", "363LF0000X", "Primary Care", 0),
    ("Physician Assistant - Medical", "363AM0700X", "Primary Care", 0), ("Medical Genetics", "207SG0201X", "Medical", 1),
]


def build_scope_rules():
    fpa = {s[0]: s[5] for s in STATES}
    dc_imaging = {"TX", "FL", "GA", "AZ", "OH"}
    pa_pain = {"TX", "FL", "AZ", "GA", "NC"}
    rows, i = [], 1
    for st in CORE_STATES:
        for lic, cat, permitted, sup in [
            ("NP", "IMAGING", 1, 1 - fpa[st]),
            ("DC", "IMAGING", int(st in dc_imaging), 0),
            ("NP", "GENETIC_TESTING", fpa[st], 0),
            ("PA", "PAIN_MGMT", int(st in pa_pain), 1),
        ]:
            ref = f"{st} Health Occ. Code §{rng.randint(100, 899)}.{rng.randint(1, 40)} (synthetic)"
            rows.append((i, st, lic, cat, permitted, sup, ref))
            i += 1
    return rows


# state, category, cpt, covered, pa, doc, min_age, max_age, max_units, effective, end, citation
COVERAGE_RULES = [
    ("CA", "COMMERCIAL", "43644", 1, 1, "LETTER_OF_MEDICAL_NECESSITY", 18, None, None, "2020-01-01", None, "CA Ins. Code bariatric mandate (synthetic)"),
    ("TX", "COMMERCIAL", "43644", 0, 0, None, None, None, None, "2020-01-01", None, "No state mandate; plan exclusion"),
    ("FL", "COMMERCIAL", "43644", 0, 0, None, None, None, None, "2020-01-01", None, "No state mandate; plan exclusion"),
    ("GA", "COMMERCIAL", "43644", 0, 0, None, None, None, None, "2023-01-01", None, "Employer group carve-out of bariatric benefit"),
    ("GA", "MEDICAID", "43644", 1, 1, "LETTER_OF_MEDICAL_NECESSITY", 21, None, None, "2021-07-01", None, "GA Medicaid policy manual (synthetic)"),
    ("NY", "COMMERCIAL", "43644", 1, 1, "LETTER_OF_MEDICAL_NECESSITY", 18, None, None, "2020-01-01", None, "NY essential benefits (synthetic)"),
    ("TX", "MEDICAID", "98941", 1, 1, "CLINICAL_NOTES", None, 20, None, "2020-01-01", None, "EPSDT only"),
    ("FL", "MEDICAID", "98941", 1, 1, "CLINICAL_NOTES", None, 20, None, "2020-01-01", None, "EPSDT only"),
    ("GA", "MEDICAID", "98941", 0, 0, None, None, None, None, "2020-01-01", None, "Not a covered Medicaid service"),
    ("CA", "COMMERCIAL", "97153", 1, 1, "TREATMENT_PLAN", None, None, None, "2020-01-01", None, "CA autism mandate (synthetic)"),
    ("TX", "COMMERCIAL", "97153", 1, 1, "TREATMENT_PLAN", None, None, None, "2020-01-01", None, "TX autism mandate (synthetic)"),
    ("NC", "COMMERCIAL", "97153", 1, 1, "TREATMENT_PLAN", None, 18, None, "2020-01-01", None, "NC autism mandate, age-limited (synthetic)"),
    ("FL", "MEDICAID", "97153", 1, 1, "TREATMENT_PLAN", None, 20, None, "2020-01-01", None, "EPSDT"),
    ("OH", "COMMERCIAL", "97153", 1, 1, "TREATMENT_PLAN", None, 13, None, "2020-01-01", None, "OH autism mandate, age-limited (synthetic)"),
    ("NY", "MEDICARE", "15823", 1, 1, "VISUAL_FIELD_TEST", None, None, None, "2020-01-01", None, "LCD blepharoplasty (synthetic)"),
    ("PA", "COMMERCIAL", "19318", 1, 1, "CLINICAL_PHOTOS", 18, None, None, "2020-01-01", None, "Plan medical policy"),
    ("IL", "COMMERCIAL", "19318", 1, 1, "CLINICAL_PHOTOS", 16, None, None, "2020-01-01", None, "Plan medical policy"),
    ("CA", "MEDICARE", "E0601", 1, 1, "SLEEP_STUDY", None, None, None, "2020-01-01", None, "LCD PAP devices (synthetic)"),
    ("TX", "MEDICARE", "E0601", 1, 1, "SLEEP_STUDY", None, None, None, "2020-01-01", None, "LCD PAP devices (synthetic)"),
    ("FL", "MEDICARE", "K0861", 1, 1, "LETTER_OF_MEDICAL_NECESSITY", None, None, None, "2020-01-01", None, "LCD power mobility (synthetic)"),
    ("OH", "MEDICARE", "K0861", 1, 1, "CLINICAL_NOTES", None, None, None, "2020-01-01", None, "Face-to-face encounter required"),
    ("NY", "COMMERCIAL", "22612", 1, 1, "PT_PROGRESS_NOTES", None, None, None, "2020-01-01", None, "6 wks conservative therapy"),
    ("PA", "COMMERCIAL", "22612", 1, 1, "IMAGING_REPORT", None, None, None, "2020-01-01", None, "Plan medical policy"),
    ("CA", "COMMERCIAL", "97110", 1, 1, "PT_PROGRESS_NOTES", None, None, 80, "2020-01-01", None, "Visit limit"),
    ("TX", "COMMERCIAL", "97110", 1, 1, "PT_PROGRESS_NOTES", None, None, 60, "2020-01-01", None, "Unit limit per benefit year"),
    ("GA", "COMMERCIAL", "97110", 1, 1, "PT_PROGRESS_NOTES", None, None, 48, "2020-01-01", None, "Visit limit"),
    ("IL", "MEDICAID", "97110", 1, 1, "PT_PROGRESS_NOTES", None, None, 40, "2020-01-01", None, "Medicaid therapy cap"),
    ("AZ", "COMMERCIAL", "81162", 1, 1, "GENETIC_COUNSELING_NOTE", 18, None, None, "2020-01-01", None, "Plan genetic testing policy"),
    ("AZ", "MEDICAID", "81162", 1, 1, "GENETIC_COUNSELING_NOTE", 18, None, None, "2020-01-01", None, "Adult-onset hereditary cancer testing not covered for minors"),
    ("NC", "MEDICARE", "81162", 1, 1, "GENETIC_COUNSELING_NOTE", None, None, None, "2020-01-01", None, "NCD 90.2: personal cancer history required (synthetic)"),
    ("OH", "MEDICAID", "81162", 0, 0, None, None, None, None, "2020-01-01", None, "Not covered"),
    ("CA", "MEDICARE", "70551", 1, 0, None, None, None, None, "2020-01-01", None, "No PA for advanced imaging"),
    ("NY", "MEDICARE", "72148", 1, 1, "CLINICAL_NOTES", None, None, None, "2020-01-01", None, "RBM program"),
    ("IL", "COMMERCIAL", "70551", 1, 0, None, None, None, None, "2024-01-01", None, "Gold-carding program"),
    ("TX", "COMMERCIAL", "95810", 1, 1, "SLEEP_STUDY", None, None, None, "2020-01-01", None, "HST required first"),
    ("FL", "COMMERCIAL", "95810", 1, 1, "SLEEP_STUDY", None, None, None, "2020-01-01", None, "HST required first"),
    ("AZ", "MEDICAID", "95810", 1, 1, "CLINICAL_NOTES", None, None, None, "2020-01-01", None, "AHCCCS policy (synthetic)"),
    ("NC", "MEDICAID", "96130", 1, 1, "CLINICAL_NOTES", None, 20, None, "2020-01-01", None, "EPSDT"),
    ("PA", "MEDICAID", "96130", 1, 1, "CLINICAL_NOTES", None, None, None, "2020-01-01", None, "Plan policy"),
    ("GA", "COMMERCIAL", "15823", 0, 0, None, None, None, None, "2020-01-01", None, "Cosmetic exclusion"),
    ("OH", "COMMERCIAL", "27447", 1, 1, "PT_PROGRESS_NOTES", None, None, None, "2020-01-01", None, "3 months conservative therapy"),
    ("AZ", "MEDICARE", "27447", 1, 1, "IMAGING_REPORT", None, None, None, "2020-01-01", None, "Plan medical policy"),
    ("FL", "COMMERCIAL", "63030", 1, 1, "IMAGING_REPORT", None, None, None, "2020-01-01", None, "Plan medical policy"),
    ("IL", "MEDICARE", "93458", 1, 1, "CLINICAL_NOTES", None, None, None, "2020-01-01", None, "Stress test results required"),
    ("NY", "COMMERCIAL", "96413", 1, 0, None, None, None, None, "2020-01-01", None, "State limit on oncology PA (synthetic)"),
    ("TX", "MEDICAID", "77385", 1, 1, "TREATMENT_PLAN", None, None, None, "2020-01-01", None, "Plan policy"),
    ("CA", "COMMERCIAL", "72148", 1, 1, "CLINICAL_NOTES", None, None, None, "2020-01-01", "2025-06-30", "Superseded 2025-07-01"),
    ("CA", "COMMERCIAL", "72148", 1, 1, "PT_PROGRESS_NOTES", None, None, None, "2025-07-01", None, "6 weeks documented conservative therapy now required"),
]

PAYERS = ["Bluestone Health", "Meridian Crest Health", "Harborview Mutual", "Cedar Valley Health", "Sunpeak Health Partners"]
CITIES = {
    "CA": [("Riverbend", "95"), ("Alta Loma Vista", "92"), ("Coastview", "93")],
    "TX": [("Pecan Hollow", "75"), ("Mesquite Falls", "78"), ("Bluebonnet", "77")],
    "FL": [("Palm Harbor Key", "33"), ("Cypress Bend", "32"), ("Mangrove", "34")],
    "NY": [("Hudson Ridge", "12"), ("Lakemont", "14"), ("Elm Harbor", "11")],
    "PA": [("Keystone Mills", "17"), ("Allegheny Run", "15"), ("Brandywine", "19")],
    "OH": [("Maple Fork", "43"), ("Cuyahoga Flats", "44"), ("Miami Bend", "45")],
    "GA": [("Peachtree Hollow", "30"), ("Oconee Springs", "31"), ("Savannah Pines", "31")],
    "NC": [("Piedmont Crossing", "27"), ("Blue Ridge Falls", "28"), ("Outer Banks", "27")],
    "IL": [("Prairie Junction", "61"), ("Lake Shore", "60"), ("Sangamon", "62")],
    "AZ": [("Saguaro Flats", "85"), ("Red Mesa", "86"), ("Copper Canyon", "85")],
}
STREETS = ["Oak", "Cedar", "Willow", "Juniper", "Harbor", "Sycamore", "Ridge", "Lantern", "Meadow", "Quarry",
           "Birch", "Summit", "Orchard", "Falcon", "Granite", "Linden", "Magnolia", "Prairie", "Aspen", "Copper"]
SUFFIXES = ["St", "Ave", "Rd", "Ln", "Dr", "Ct", "Way", "Blvd"]
FIRST_F = ["Avery", "Brielle", "Camila", "Delphine", "Esme", "Farrah", "Greta", "Halona", "Ines", "Juniper",
           "Kalani", "Leocadia", "Marisol", "Noor", "Odette", "Priya", "Quinn", "Rosalind", "Saoirse", "Thea",
           "Ursula", "Valentina", "Wren", "Ximena", "Yara", "Zuri"]
FIRST_M = ["Anders", "Bastian", "Caius", "Dashiell", "Emeka", "Florian", "Gideon", "Hollis", "Idris", "Jasper",
           "Kenji", "Lucian", "Matteo", "Nikolai", "Oren", "Pascal", "Rafferty", "Soren", "Tobias", "Umar",
           "Viggo", "Wendell", "Xavi", "Yusuf", "Zane"]
LAST = ["Abernathy", "Blackwood", "Castellanos", "Dunleavy", "Ekwueme", "Fairbanks", "Galloway", "Hargrove",
        "Ishikawa", "Jankowski", "Kowalczyk", "Lindqvist", "Marchetti", "Nakamura", "Okonkwo", "Pemberton",
        "Quintanilla", "Rasmussen", "Szymanski", "Thibodeaux", "Underhill", "Valdivia", "Whitlock", "Yamamoto",
        "Zielinski", "Ashworth", "Bellamy", "Crowder", "Delacroix", "Esposito", "Fenwick", "Grimaldi", "Holloway",
        "Ingram", "Kingsley", "Larkspur", "Montague", "Northcott", "Oyelaran", "Prendergast"]


def addr(state):
    city, zip3 = rng.choice(CITIES[state])
    return (f"{rng.randint(100, 9899)} {rng.choice(STREETS)} {rng.choice(SUFFIXES)}", city, f"{zip3}{rng.randint(100, 999)}")


def build_plans(itypes):
    by_cat = {}
    for t in itypes:
        by_cat.setdefault((t[2], t[3]), []).append(t)
    rows, pid = [], 1
    for st in CORE_STATES:
        picks = [rng.choice(by_cat[("COMMERCIAL", "HMO")] + by_cat[("COMMERCIAL", "EPO")]),
                 rng.choice(by_cat[("COMMERCIAL", "PPO")] + by_cat[("COMMERCIAL", "POS")]),
                 rng.choice([t for t in itypes if t[2] == "MEDICARE" and t[3] != "PDP"]),
                 rng.choice([t for t in itypes if t[2] == "MEDICAID" and t[3] == "HMO"])]
        for t in picks:
            payer = rng.choice(PAYERS)
            ded = {"COMMERCIAL": rng.choice([500, 1500, 3000, 6000]), "MEDICARE": rng.choice([0, 250]), "MEDICAID": 0}[t[2]]
            oop = {"COMMERCIAL": rng.choice([6500, 8000, 9200]), "MEDICARE": rng.choice([3900, 5500, 8850]), "MEDICAID": 0}[t[2]]
            name = f"{payer.split()[0]} {t[1].replace('Commercial ', '')} {st}"
            rows.append([pid, f"{st}-{t[3]}-{pid:03d}", name, payer, t[0], st, "2019-01-01", None, ded, oop, 1])
            pid += 1
    # one plan withdrew from the market mid-2025
    rows[5][7], rows[5][10] = "2025-03-31", 0
    return rows


def build_facilities():
    kinds = ["HOSPITAL", "ASC", "CLINIC", "IMAGING_CENTER", "SNF", "INFUSION_CENTER", "SLEEP_LAB", "REHAB"]
    nouns = {"HOSPITAL": "Regional Medical Center", "ASC": "Surgery Center", "CLINIC": "Family Clinic",
             "IMAGING_CENTER": "Diagnostic Imaging", "SNF": "Skilled Nursing & Rehab", "INFUSION_CENTER": "Infusion Center",
             "SLEEP_LAB": "Sleep Disorders Center", "REHAB": "Physical Rehabilitation"}
    rows = []
    for i in range(1, 36):
        st = CORE_STATES[(i - 1) % 10]
        kind = ["HOSPITAL", "ASC", "CLINIC", "IMAGING_CENTER"][(i - 1) // 10] if i <= 30 else kinds[4 + (i - 31) % 4]
        a1, city, z = addr(st)
        accred = {"HOSPITAL": "Joint Commission", "ASC": "AAAHC", "IMAGING_CENTER": "ACR"}.get(kind, rng.choice(["CARF", None]))
        rows.append((i, f"{city} {nouns[kind]}", kind, luhn_npi(str(1300000000 + i * 7919)[1:]), a1, city, st, z,
                     accred, 0 if i in (17, 33) else 1))
    return rows


# credential -> list of specialty names valid for it
CRED_SPECIALTIES = {
    "MD": ["Family Medicine", "Internal Medicine", "Pediatrics", "Orthopaedic Surgery", "Neurological Surgery",
           "General Surgery", "Plastic Surgery", "Bariatric Surgery", "Cardiology", "Interventional Cardiology",
           "Neurology", "Gastroenterology", "Medical Oncology", "Radiation Oncology", "Rheumatology", "Endocrinology",
           "Dermatology", "Sleep Medicine", "Pain Medicine", "Psychiatry", "Ophthalmology", "Medical Genetics"],
    "DO": ["Family Medicine", "Internal Medicine", "Orthopaedic Surgery", "Pain Medicine", "Sleep Medicine"],
    "NP": ["Nurse Practitioner - Family"], "PA": ["Physician Assistant - Medical"], "DPM": ["Podiatry"],
    "DC": ["Chiropractic"], "PT": ["Physical Therapy"], "PhD": ["Clinical Psychology", "Behavior Analysis"],
    "LCSW": ["Clinical Social Work"],
}
CREDENTIAL_MIX = ["MD"] * 21 + ["DO"] * 4 + ["NP"] * 7 + ["PA"] * 4 + ["DPM", "DPM", "DC", "DC", "PT", "PT", "PhD", "PhD", "LCSW"]


def build_providers(specialties, facilities):
    spec_id = {s[1]: s[0] for s in specialties}
    fac_by_state = {}
    for f in facilities:
        fac_by_state.setdefault(f[6], []).append(f[0])
    rows, home = [], {}
    md_specs = CRED_SPECIALTIES["MD"][:]
    rng.shuffle(md_specs)
    for i, cred in enumerate(CREDENTIAL_MIX, start=1):
        st = CORE_STATES[(i - 1) % 10]
        spec = md_specs.pop() if cred == "MD" and md_specs else rng.choice(CRED_SPECIALTIES[cred])
        fname = rng.choice(FIRST_F + FIRST_M)
        fac = None if i % 9 == 0 else rng.choice(fac_by_state[st])
        rows.append([i, luhn_npi(str(1100000000 + i * 104729)[1:]), fname, rng.choice(LAST), cred, spec_id[spec],
                     fac, 0, rng.choice([0, 1, 1])])
        home[i] = st
    rows[30][7] = 1  # one provider on the OIG exclusion list
    return rows, home


def build_licenses(providers, home):
    rows, lid = [], 1
    for p in providers:
        pid, cred = p[0], p[4]
        issued = rand_date("2008-01-01", "2022-12-31")
        rows.append([lid, pid, home[pid], f"{home[pid]}-{cred}-{rng.randint(10000, 99999)}", cred, iso(issued),
                     iso(date(2026 + rng.randint(0, 2), issued.month, min(issued.day, 28))), "ACTIVE", 0,
                     int(cred in ("MD", "DO", "NP", "PA", "DPM"))])
        lid += 1
    # second-state licenses (border practice / telehealth), some via the interstate compact
    for pid, st, compact in [(1, "NV", 1), (4, "NJ", 0), (12, "CO", 1), (15, "FL", 0), (23, "WA", 1)]:
        cred = providers[pid - 1][4]
        rows.append([lid, pid, st, f"{st}-{cred}-{rng.randint(10000, 99999)}", cred, "2021-03-15", "2027-03-31",
                     "ACTIVE", compact, 1])
        lid += 1
    # messiness: lapsed, disciplined and badly-kept licenses (indexes are provider_id - 1)
    rows[2][6], rows[2][7] = "2025-03-31", "EXPIRED"            # expired before most 2025 services
    rows[8][6], rows[8][7] = "2025-07-31", "EXPIRED"            # expired mid-year
    rows[13][6], rows[13][7] = "2025-05-31", "ACTIVE"           # stale status: date has passed, still says ACTIVE
    rows[19][7] = "SUSPENDED"
    rows[25][7] = "PROBATION"
    rows[34][7] = "PROBATION"
    rows[28][6] = None                                          # expiry never keyed in
    rows[40][7], rows[40][6] = "REVOKED", "2024-11-30"
    return rows


def build_members(plans, itypes, providers):
    cat_of = {t[0]: t[2] for t in itypes}
    plan_cat = {p[0]: cat_of[p[4]] for p in plans}
    rows, enroll, ssns = [], [], set()
    active_plans = [p for p in plans if p[10] == 1]
    for i in range(1, 46):
        plan = active_plans[(i - 1) % len(active_plans)] if i <= 39 else rng.choice(active_plans)
        st, cat = plan[5], plan_cat[plan[0]]
        if cat == "MEDICARE":
            dob = rand_date("1935-01-01", "1959-12-31")
        elif cat == "MEDICAID":
            dob = rand_date("2008-01-01", "2021-12-31") if i % 3 == 0 else rand_date("1960-01-01", "2004-12-31")
        else:
            dob = rand_date("1962-01-01", "2002-12-31")
        sex = rng.choice(["F", "M"]) if i != 22 else "X"
        first = rng.choice(FIRST_F if sex == "F" else FIRST_M if sex == "M" else FIRST_F + FIRST_M)
        while True:
            ssn = f"{rng.randint(900, 999)}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"
            if ssn not in ssns:
                ssns.add(ssn)
                break
        a1, city, z = addr(st)
        a2 = f"Apt {rng.randint(1, 40)}{rng.choice('ABCD')}" if i % 6 == 0 else None
        phone = None if i % 11 == 0 else f"({rng.randint(201, 989)}) 555-{rng.randint(100, 199):04d}"
        last = rng.choice(LAST)
        email = None if i % 4 == 0 else f"{first.lower()}.{last.lower()}{rng.randint(1, 99)}@example.com"
        mid = f"M{100000 + i * 37:06d}"
        rows.append((mid, first, last, iso(dob), sex, ssn, a1, a2, city, st, z, phone, email))
        enroll.append([mid, plan[0], st, cat, dob])
    return rows, enroll, plan_cat


def build_enrollments(member_seed, plans, itypes, providers, specialties, home):
    model_of = {t[0]: t[3] for t in itypes}
    plan_model = {p[0]: model_of[p[4]] for p in plans}
    spec_name = {s[0]: s[1] for s in specialties}
    pcps = {}
    for p in providers:
        if spec_name[p[5]] in ("Family Medicine", "Internal Medicine", "Pediatrics", "Nurse Practitioner - Family"):
            pcps.setdefault(home[p[0]], []).append(p[0])
    rows, eid = [], 1
    terminated_plan = next(p for p in plans if p[10] == 0)
    for idx, (mid, plan_id, st, cat, dob) in enumerate(member_seed):
        start = max(rand_date("2018-01-01", "2024-06-30"), dob + timedelta(days=1))
        pcp = rng.choice(pcps[st]) if plan_model[plan_id] == "HMO" and st in pcps and idx % 7 != 3 else None
        rel = "SUBSCRIBER"
        sub = None
        # a handful of members had a prior plan that terminated
        if idx in (2, 9, 16, 30):
            prev = terminated_plan if idx == 2 else next(p for p in plans if p[5] == st and p[0] != plan_id)
            p_end = date(2025, 3, 31) if idx == 2 else date(2024, 12, 31)
            rows.append([eid, mid, prev[0], None, "SUBSCRIBER", iso(start), iso(p_end), "TERMINATED", None])
            eid += 1
            start = p_end + timedelta(days=1)
        status, end = "ACTIVE", None
        if idx in (5, 21):           # lost coverage, no replacement
            status, end = "TERMINATED", ("2025-05-31" if idx == 5 else "2025-07-31")
        elif idx == 12:              # newly enrolled, eligibility file not yet confirmed
            status, start = "PENDING_VERIFICATION", date(2025, 9, 1)
        elif idx == 26:              # future-dated coverage
            start = date(2025, 10, 1)
        if cat == "COMMERCIAL" and idx >= 40:
            subs = [r for r in rows if r[2] == plan_id and r[4] == "SUBSCRIBER" and r[7] == "ACTIVE"]
            if subs:
                rel, sub = ("DEPENDENT" if dob.year > 2000 else "SPOUSE"), subs[0][1]
        rows.append([eid, mid, plan_id, sub, rel, iso(start), end, status, pcp])
        eid += 1
    # one member with a retroactively-terminated enrollment that was re-instated
    rows.append([eid, member_seed[40][0], member_seed[40][1], None, "SUBSCRIBER", "2017-01-01", "2017-12-31",
                 "TERMINATED", None])
    return rows, plan_model


def build_network(providers, plans, home, plan_model):
    rows, seen, nid = [], set(), 1
    by_state = {}
    for p in plans:
        by_state.setdefault(p[5], []).append(p)
    for p in providers:
        st = home[p[0]]
        # HMOs contract narrowly; each provider joins 1 plan in-state, alternating so HMOs get coverage
        cand = sorted(by_state[st], key=lambda pl: (plan_model[pl[0]] != "HMO", pl[0]))
        pick = cand[(p[0] // 10) % len(cand)]
        rows.append([nid, p[0], pick[0], "IN_NETWORK", "2020-01-01", None])
        seen.add((p[0], pick[0]))
        nid += 1
    extras = 0
    for p in providers:
        if extras >= 5:
            break
        st = home[p[0]]
        for pl in by_state[st]:
            if (p[0], pl[0]) not in seen and plan_model[pl[0]] == "HMO":
                rows.append([nid, p[0], pl[0], "IN_NETWORK", "2022-01-01", None])
                seen.add((p[0], pl[0]))
                nid += 1
                extras += 1
                break
    rows[7][3], rows[7][5] = "TERMINATED", "2025-05-31"
    return rows


def build_reviewers():
    roles = (["INTAKE"] * 5 + ["NURSE_REVIEWER"] * 12 + ["PHARMACIST"] * 4 + ["MEDICAL_DIRECTOR"] * 6
             + ["APPEALS_SPECIALIST"] * 3)
    cred = {"INTAKE": "CPC", "NURSE_REVIEWER": "RN", "PHARMACIST": "PharmD", "MEDICAL_DIRECTOR": "MD",
            "APPEALS_SPECIALIST": "RN"}
    rows = []
    for i, role in enumerate(roles, start=1):
        rows.append((i, rng.choice(FIRST_F + FIRST_M), rng.choice(LAST), cred[role], role,
                     None if role == "INTAKE" else rng.choice(CORE_STATES), iso(rand_date("2012-01-01", "2024-12-31")),
                     0 if i in (8, 19) else 1))
    return rows


# ================================================================ rule engine
SPEC_SERVICES = {
    "Family Medicine": ["72148", "70551", "99214", "E0784", "Ozempic", "Wegovy", "Zepbound"],
    "Internal Medicine": ["74177", "70551", "95810", "E0601", "Repatha", "Ozempic"],
    "Pediatrics": ["96130", "99214", "Vyvanse"],
    "Orthopaedic Surgery": ["27447", "27130", "29881", "73721", "20610"],
    "Neurological Surgery": ["63030", "22612", "72148"],
    "General Surgery": ["47562", "43644"],
    "Plastic Surgery": ["19318", "15823"],
    "Bariatric Surgery": ["43644"],
    "Cardiology": ["93306", "93458", "Repatha"],
    "Interventional Cardiology": ["93458", "33208"],
    "Neurology": ["70553", "70551", "Aimovig", "Ocrevus", "Nurtec ODT"],
    "Gastroenterology": ["45378", "43239", "Stelara", "Humira"],
    "Medical Oncology": ["96413", "78815", "81162", "Keytruda"],
    "Radiation Oncology": ["77385", "78815"],
    "Rheumatology": ["Humira", "Enbrel", "Xeljanz", "Hyrimoz"],
    "Endocrinology": ["E0784", "Mounjaro", "Wegovy"],
    "Dermatology": ["Otezla", "Enbrel"],
    "Sleep Medicine": ["95810", "E0601", "95806"],
    "Pain Medicine": ["64483", "72148", "20610"],
    "Psychiatry": ["90837", "Spravato", "Vyvanse"],
    "Ophthalmology": ["15823"],
    "Medical Genetics": ["81162"],
    "Nurse Practitioner - Family": ["70551", "72148", "81162", "95810", "Ozempic", "Vyvanse"],
    "Physician Assistant - Medical": ["64483", "72148", "99214", "20610"],
    "Podiatry": ["28296", "73721"],
    "Chiropractic": ["98941", "72148", "97140"],
    "Physical Therapy": ["97110", "97140", "97161"],
    "Clinical Psychology": ["96130", "90837"],
    "Behavior Analysis": ["97153"],
    "Clinical Social Work": ["90837", "96130"],
}
# plausible diagnosis for services that have no payer criteria rows
FALLBACK_DX = {
    "99214": "E11.65", "47562": "K80.20", "45378": "Z12.11", "43239": "K21.9", "93306": "I25.10",
    "95806": "R06.83", "20610": "M17.11", "97161": "M54.50", "90837": "F33.1", "Zepbound": "E66.01",
    "Hyrimoz": "M05.79", "Enbrel": "M05.79", "Xeljanz": "M05.79", "Nurtec ODT": "G43.909",
    "Keytruda": "C34.90", "Spravato": "F33.1",
}
NETWORK_RESTRICTED = ("HMO", "EPO")


class World:
    """In-memory view of the generated reference/entity data for rule evaluation."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.proc = {p[0]: p for p in PROCEDURES}
        self.drug = {r[2]: r for r in kw["drug_rows"]}            # brand -> row
        self.drug_by_id = {r[0]: r for r in kw["drug_rows"]}
        self.itype = {t[0]: t for t in kw["itypes"]}
        self.plan = {p[0]: p for p in kw["plans"]}
        self.member = {m[0]: m for m in kw["members"]}
        self.enr = {e[0]: e for e in kw["enrollments"]}
        self.prov = {p[0]: p for p in kw["providers"]}
        self.spec = {s[0]: s for s in kw["specialties"]}
        self.lic = {(l[1], l[2]): l for l in kw["licenses"]}
        self.scope = {(r[1], r[2], r[3]): r for r in kw["scope_rules"]}
        self.crit = {}
        for c in kw["criteria"]:
            self.crit.setdefault(c[1] or ("rx", c[2]), set()).add(c[3])
        self.dx = {x[0]: x for x in DIAGNOSES}

    def category(self, plan_id):
        return self.itype[self.plan[plan_id][4]][2]

    def model(self, plan_id):
        return self.itype[self.plan[plan_id][4]][3]

    def in_network(self, prov_id, plan_id, on):
        for n in self.network:
            if n[1] == prov_id and n[2] == plan_id and d(n[4]) <= on and (n[5] is None or on <= d(n[5])):
                return True
        return False

    def coverage(self, state, cat, cpt, on):
        for r in self.coverage_rows:
            if r[1] == state and r[2] == cat and r[3] == cpt and d(r[10]) <= on and (r[11] is None or on <= d(r[11])):
                return r
        return None

    def crit_key(self, svc):
        return svc if svc in self.proc else ("rx", self.drug[svc][0])

    def scope_ok(self, state, lic_type, cpt):
        cat = self.proc[cpt][2]
        rule = self.scope.get((state, lic_type, cat))
        if rule:
            return bool(rule[4])
        return lic_type in self.proc[cpt][5].split(",")

    def requirements(self, state, cat, svc, on):
        """-> (requires_pa, required_doc, covered, min_age, max_age, max_units)"""
        if svc in self.proc:
            p = self.proc[svc]
            r = self.coverage(state, cat, svc, on)
            if r:
                return r[5], r[6], r[4], r[7], r[8], r[9]
            return p[3], p[4], 1, None, None, None
        dr = self.drug[svc]
        doc = "STEP_THERAPY_HISTORY" if dr[7] else None
        return dr[6], doc, 1, None, None, dr[8]


def evaluate(w, req, lines, docs, cutoff):
    """Adjudicate a request as of `cutoff` (datetime). Returns (status, code, [(line_status, code, units_ok)], note)."""
    dos = d(req["service_date"])
    sub = datetime.fromisoformat(req["submitted_at"])
    enr = w.enr[req["enrollment_id"]]
    plan_id = enr[2]
    cat, model = w.category(plan_id), w.model(plan_id)
    st = req["service_state"]
    prov = w.prov[req["requesting_provider_id"]]
    due = req["due_at"]
    have = [x for x in docs if datetime.fromisoformat(x["received_at"]) <= cutoff]

    def deny_all(code, note):
        return "DENIED", code, [("DENIED", code, 0) for _ in lines], note

    # 1. eligibility
    if enr[7] == "PENDING_VERIFICATION":
        return "PENDING", None, [("PENDING", None, None) for _ in lines], "Eligibility pending verification with employer group"
    if d(enr[5]) > dos:
        return deny_all("ELG-02", "Service date precedes coverage effective date")
    if enr[6] and dos > d(enr[6]):
        return deny_all("ELG-01", f"Coverage terminated {enr[6]}")
    # timely filing for retro requests
    if req["request_type"] == "RETRO_AUTH" and (sub.date() - dos).days > 90:
        return deny_all("ADM-01", "Retro request filed more than 90 days after service")
    # 2. provider eligibility & licensure in the service state
    if prov[7]:
        return deny_all("LIC-04", "Requesting provider on OIG exclusion list")
    lic = w.lic.get((prov[0], st))
    if lic is None:
        return deny_all("LIC-01", f"No {st} license on file for requesting provider")
    if lic[7] in ("SUSPENDED", "REVOKED"):
        return deny_all("LIC-02", f"{st} license status {lic[7]}")
    if lic[6] is None:
        return "PENDING", None, [("PENDING", None, None) for _ in lines], "License expiry not on file; credentialing verification requested"
    if d(lic[5]) > dos or dos > d(lic[6]):
        return deny_all("LIC-01", f"{st} license not valid on DOS (expires {lic[6]})")
    # 3. network
    if model in NETWORK_RESTRICTED and not w.in_network(prov[0], plan_id, dos):
        return deny_all("NET-01", f"Provider out of network for {model} plan")
    # 4. referral
    spec_group = w.spec[prov[5]][3]
    has_ref_doc = any(x["document_type"] == "REFERRAL" for x in have)
    if w.itype[w.plan[plan_id][4]][5] and spec_group != "Primary Care" and not (req["referral_on_file"] or has_ref_doc):
        return deny_all("REF-01", "PCP referral required for specialist services")

    # 5. line-level adjudication
    age = age_on(w.member[req["member_id"]][3], dos)
    dxs = {req["primary_icd10"], req["secondary_icd10"]} - {None}
    results, notes = [], []
    for ln in lines:
        svc = ln["svc"]
        pa, req_doc, covered, min_age, max_age, max_units = w.requirements(st, cat, svc, dos)
        if svc in w.proc and not w.scope_ok(st, lic[4], svc):
            results.append(("DENIED", "LIC-03", 0)); notes.append(f"{svc} outside {lic[4]} scope in {st}")
            continue
        if not covered:
            results.append(("DENIED", "COV-01", 0)); notes.append(f"{svc} excluded for {cat} in {st}")
            continue
        if (min_age is not None and age < min_age) or (max_age is not None and age > max_age):
            results.append(("DENIED", "COV-03", 0)); notes.append(f"Member age {age} outside covered range")
            continue
        if svc in w.drug and w.drug[svc][5] is None:
            if any(x["document_type"] == "LETTER_OF_MEDICAL_NECESSITY" and x["is_complete"] for x in have):
                notes.append("Formulary exception granted")
            else:
                results.append(("DENIED", "RX-01", 0)); notes.append(f"{svc} non-formulary")
                continue
        if not pa:
            results.append(("APPROVED", None, ln["units"])); notes.append(f"{svc}: no authorization required")
            continue
        if req_doc:
            match = [x for x in have if x["document_type"] == req_doc]
            if not match or not any(x["is_complete"] for x in match):
                code = "RX-02" if req_doc == "STEP_THERAPY_HISTORY" else ("DOC-03" if match else "DOC-02")
                if cutoff < due:
                    results.append(("PENDING", None, None)); notes.append(f"Awaiting {req_doc}")
                else:
                    results.append(("DENIED", code, 0)); notes.append(f"{req_doc} {'incomplete' if match else 'not received'}")
                continue
        allowed_dx = w.crit.get(w.crit_key(svc))
        if allowed_dx and not (allowed_dx & dxs):
            same_ch = any(w.dx[a][2] == w.dx[req["primary_icd10"]][2] for a in allowed_dx)
            results.append(("DENIED", "MN-01" if same_ch else "MN-02", 0)); notes.append(f"{svc}: criteria not met for dx")
            continue
        if max_units is not None and ln["units"] > max_units:
            code = "RX-03" if svc in w.drug else "COV-04"
            results.append(("APPROVED", code, max_units)); notes.append(f"{svc}: reduced to {max_units} units")
            continue
        results.append(("APPROVED", None, ln["units"]))
    stats = [r[0] for r in results]
    if "PENDING" in stats:
        return "PENDING", None, results, "; ".join(notes)
    if all(s == "DENIED" for s in stats):
        return "DENIED", results[0][1], results, "; ".join(notes)
    return "APPROVED", None, results, "; ".join(notes) or "Criteria met"


# ------------------------------------------------------- scenario candidates
def features(w, enr, prov, svc, on):
    member = w.member[enr[1]]
    st = member[9]
    plan = w.plan[enr[2]]
    cat, model = w.category(plan[0]), w.model(plan[0])
    lic = w.lic.get((prov[0], st))
    pa, req_doc, covered, min_age, max_age, max_units = w.requirements(st, cat, svc, on)
    age = age_on(member[3], on)
    is_drug = svc in w.drug
    return dict(
        st=st, cat=cat, model=model, lic=lic,
        lic_ok=bool(lic and lic[7] in ("ACTIVE", "PROBATION") and lic[6] and d(lic[6]) > date(2025, 12, 31)),
        in_net=w.in_network(prov[0], plan[0], on),
        net_ok=model not in NETWORK_RESTRICTED or w.in_network(prov[0], plan[0], on),
        ref_needed=bool(w.itype[plan[4]][5]) and w.spec[prov[5]][3] != "Primary Care",
        scope_ok=is_drug or bool(lic and w.scope_ok(st, lic[4], svc)),
        pa=pa, req_doc=req_doc, covered=covered, max_units=max_units, is_drug=is_drug,
        age_ok=(min_age is None or age >= min_age) and (max_age is None or age <= max_age),
        enr_clean=enr[7] == "ACTIVE" and enr[6] is None and d(enr[5]) <= date(2025, 1, 1) and plan[10] == 1,
        oig=prov[7] == 1, tier=w.drug[svc][5] if is_drug else 0,
        step=bool(w.drug[svc][7]) if is_drug else False,
        crit=w.crit.get(w.crit_key(svc)), svc=svc,
    )


def clean(f):
    return (f["enr_clean"] and f["lic_ok"] and not f["oig"] and f["net_ok"] and f["scope_ok"] and f["covered"]
            and f["age_ok"] and f["tier"] is not None)


SCENARIOS = {
    "clean":            lambda f: clean(f) and f["pa"] and f["lic"][7] == "ACTIVE",
    "clean_nopa":       lambda f: clean(f) and not f["pa"] and not f["is_drug"],
    "partial_units":    lambda f: clean(f) and f["pa"] and f["max_units"] is not None and not f["is_drug"],
    "lic_probation":    lambda f: clean(f) and f["pa"] and f["lic"][7] == "PROBATION",
    "elig_term":        lambda f: f["lic_ok"] and f["net_ok"] and f["scope_ok"] and f["covered"],
    "elig_future":      lambda f: f["lic_ok"] and f["scope_ok"] and f["pa"],
    "elig_pending":     lambda f: f["lic_ok"] and f["scope_ok"] and f["pa"],
    "lic_none_tele":    lambda f: f["enr_clean"] and f["lic"] is None and not f["oig"],
    "lic_expired":      lambda f: f["enr_clean"] and f["lic"] and f["lic"][7] == "EXPIRED" and f["lic"][6] > "2025-06-01",
    "err_missed_stale": lambda f: f["enr_clean"] and f["lic"] and f["lic"][7] == "ACTIVE" and f["lic"][6] and f["lic"][6] < "2025-09-01" and f["net_ok"] and f["pa"] and f["covered"],
    "lic_suspended":    lambda f: f["enr_clean"] and f["lic"] and f["lic"][7] in ("SUSPENDED", "REVOKED"),
    "lic_oig":          lambda f: f["enr_clean"] and f["oig"] and f["lic"] is not None,
    "lic_unknown":      lambda f: f["enr_clean"] and f["lic"] and f["lic"][6] is None and not f["oig"],
    "scope":            lambda f: f["enr_clean"] and f["lic_ok"] and f["net_ok"] and not f["scope_ok"] and not f["oig"],
    "oon":              lambda f: f["enr_clean"] and f["lic_ok"] and not f["oig"] and not f["net_ok"] and f["scope_ok"] and f["pa"],
    "no_referral":      lambda f: clean(f) and f["pa"] and f["ref_needed"],
    "not_covered":      lambda f: f["enr_clean"] and f["lic_ok"] and not f["oig"] and f["net_ok"] and f["scope_ok"] and not f["covered"],
    "age":              lambda f: f["enr_clean"] and f["lic_ok"] and not f["oig"] and f["net_ok"] and f["scope_ok"] and f["covered"] and not f["age_ok"],
    "doc_missing_old":  lambda f: clean(f) and f["pa"] and f["req_doc"] and not f["is_drug"],
    "doc_missing_new":  lambda f: clean(f) and f["pa"] and f["req_doc"] and not f["is_drug"],
    "doc_incomplete_new": lambda f: clean(f) and f["pa"] and f["req_doc"] and not f["is_drug"],
    "med_nec":          lambda f: clean(f) and f["pa"] and f["crit"],
    "err_secondary_dx": lambda f: clean(f) and f["pa"] and f["crit"],
    "err_doc_overlooked": lambda f: clean(f) and f["pa"] and f["req_doc"] and not f["is_drug"],
    "rx_nonformulary":  lambda f: f["enr_clean"] and f["lic_ok"] and not f["oig"] and f["net_ok"] and f["is_drug"] and f["tier"] is None,
    "rx_step_new":      lambda f: clean(f) and f["is_drug"] and f["step"],
    "rule_change":      lambda f: clean(f) and f["st"] == "CA" and f["cat"] == "COMMERCIAL" and f["svc"] == "72148",
    "retro_late":       lambda f: clean(f) and f["pa"] and not f["is_drug"],
}
# scenario -> count (40 requests total)
SCENARIO_PLAN = [
    ("clean", 10), ("clean_nopa", 2), ("partial_units", 1), ("lic_probation", 1),
    ("elig_term", 2), ("elig_future", 1), ("elig_pending", 1),
    ("lic_none_tele", 1), ("lic_expired", 1), ("err_missed_stale", 1), ("lic_suspended", 1), ("lic_oig", 1),
    ("lic_unknown", 1), ("scope", 1), ("oon", 1), ("no_referral", 1), ("not_covered", 1), ("age", 1),
    ("doc_missing_old", 1), ("doc_missing_new", 1), ("doc_incomplete_new", 1), ("med_nec", 2),
    ("err_secondary_dx", 1), ("err_doc_overlooked", 1), ("rx_nonformulary", 1), ("rx_step_new", 1),
    ("rule_change", 1), ("retro_late", 0),
]

# scenario -> appeal outcomes by level (filed only while dates stay <= AS_OF)
APPEAL_PATHS = {
    "err_doc_overlooked": ["OVERTURNED"], "err_secondary_dx": ["UPHELD", "OVERTURNED"],
    "doc_missing_old": ["OVERTURNED"], "med_nec": [["UPHELD", "UPHELD", "OVERTURNED"], ["UPHELD", "UPHELD", "UPHELD"]],
    "elig_term": ["UPHELD", "UPHELD"], "elig_future": ["WITHDRAWN"], "lic_none_tele": ["UPHELD", "UPHELD"],
    "lic_expired": ["UPHELD", "PENDING"], "scope": ["UPHELD", "UPHELD"], "oon": ["UPHELD", "OVERTURNED"],
    "no_referral": ["UPHELD", "UPHELD"], "not_covered": ["UPHELD", "UPHELD", "UPHELD"], "age": ["UPHELD", "PENDING"],
    "rx_nonformulary": ["OVERTURNED"], "partial_units": ["UPHELD"], "rule_change": ["UPHELD", "OVERTURNED"],
}
RECENT = {"doc_missing_new", "doc_incomplete_new", "rx_step_new", "elig_pending"}
NO_DOCS = {"doc_missing_old", "doc_missing_new", "rx_step_new"}
DOC_SUMMARIES = {
    "CLINICAL_NOTES": "Office notes documenting history, exam and failed conservative management.",
    "IMAGING_REPORT": "Radiology report of prior imaging supporting the requested service.",
    "PT_PROGRESS_NOTES": "Physical therapy progress notes with functional scores.",
    "LETTER_OF_MEDICAL_NECESSITY": "Letter of medical necessity signed by the treating provider.",
    "SLEEP_STUDY": "Sleep study report with AHI and oxygen desaturation index.",
    "LAB_RESULTS": "Recent laboratory results (HbA1c, C-peptide).",
    "TREATMENT_PLAN": "Individualized treatment plan with goals and frequency.",
    "CLINICAL_PHOTOS": "Pre-operative clinical photographs.",
    "VISUAL_FIELD_TEST": "Automated visual field test, taped and untaped.",
    "GENETIC_COUNSELING_NOTE": "Pre-test genetic counseling note with pedigree.",
    "STEP_THERAPY_HISTORY": "Pharmacy claims history showing prior trials and outcomes.",
    "REFERRAL": "PCP referral form.",
}


def dt(x):
    return datetime.fromisoformat(x) if isinstance(x, str) else x


def pick_candidates(w, scen, usage_m, usage_p):
    on = date(2025, 6, 15)
    enr_filter = {
        "elig_term": lambda e: e[7] == "TERMINATED" and e[6] and e[6] >= "2025-01-01",
        "elig_future": lambda e: d(e[5]) > AS_OF,
        "elig_pending": lambda e: e[7] == "PENDING_VERIFICATION",
    }.get(scen, lambda e: True)
    out = []
    for e in w.enrollments:
        if not enr_filter(e):
            continue
        for p in w.providers:
            for svc in SPEC_SERVICES[w.spec[p[5]][1]]:
                f = features(w, e, p, svc, on)
                if SCENARIOS[scen](f):
                    out.append((usage_m.get(e[1], 0) + usage_p.get(p[0], 0), rng.random(), e, p, svc))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def schedule(scen, enr, prov, w, st):
    """Return (submitted datetime, service date, request_type, telehealth)."""
    rtype, tele = "PRIOR_AUTH", 0
    if scen in RECENT:
        sub = datetime.combine(AS_OF - timedelta(days=rng.randint(1, 2)), datetime.min.time()).replace(hour=rng.randint(8, 15))
    elif scen == "elig_term":
        end = d(enr[6])
        sub = datetime.combine(end - timedelta(days=rng.randint(6, 12)), datetime.min.time()).replace(hour=10)
        return sub, end + timedelta(days=rng.randint(10, 20)), rtype, tele
    elif scen == "elig_future":
        sub = datetime(2025, 9, 8, 11, 20)
        return sub, date(2025, 9, 22), rtype, tele
    elif scen == "lic_expired":
        sub = datetime(2025, 7, 21, 9, 45)
        return sub, date(2025, 8, 12), rtype, tele
    elif scen == "err_missed_stale":
        sub = datetime(2025, 6, 23, 14, 5)
    elif scen == "rule_change":
        sub = datetime(2025, 6, 24, 10, 30)
        return sub, date(2025, 7, 9), rtype, tele
    elif scen == "lic_unknown":
        sub = datetime(2025, 8, 26, 13, 0)
    elif scen in APPEAL_PATHS and len(APPEAL_PATHS[scen]) >= 2:
        sub = datetime.combine(rand_date("2025-01-06", "2025-03-31"), datetime.min.time()).replace(hour=rng.randint(8, 17))
    else:
        sub = datetime.combine(rand_date("2025-01-06", "2025-08-29"), datetime.min.time()).replace(hour=rng.randint(8, 17), minute=rng.randint(0, 59))
    if scen == "lic_none_tele":
        tele = 1
    svc_lag = rng.randint(0, 3) if scen.startswith("rx_") else rng.randint(7, 40)
    return sub, sub.date() + timedelta(days=svc_lag), rtype, tele


def build_requests(w):
    usage_m, usage_p = {}, {}
    reqs, lines, docs, second_lines = [], [], [], 0
    plan_list = [s for s, n in SCENARIO_PLAN for _ in range(n)]
    rng.shuffle(plan_list)
    state_deadline = {s[0]: s[6] for s in STATES}
    for rid, scen in enumerate(plan_list, start=1):
        cands = pick_candidates(w, scen, usage_m, usage_p)
        if not cands:
            scen = "clean"
            cands = pick_candidates(w, scen, usage_m, usage_p)
        _, _, enr, prov, svc = cands[0]
        member = w.member[enr[1]]
        st = member[9]
        usage_m[member[0]] = usage_m.get(member[0], 0) + 1
        usage_p[prov[0]] = usage_p.get(prov[0], 0) + 1
        sub, dos, rtype, tele = schedule(scen, enr, prov, w, st)
        is_drug = svc in w.drug
        if is_drug:
            rtype = "PHARMACY"
        elif rid % 13 == 0 and scen == "clean":
            rtype = "CONCURRENT"
        urgency = "EXPEDITED" if (rid % 9 == 0 and scen not in RECENT) or svc in ("96413", "77385") else "STANDARD"
        plan_days = w.itype[w.plan[enr[2]][4]][7]
        due = sub + (timedelta(hours=72) if urgency == "EXPEDITED"
                     else timedelta(days=min(state_deadline[st], plan_days)))

        # diagnoses
        crit = w.crit.get(w.crit_key(svc))
        support = sorted(crit) if crit else [FALLBACK_DX.get(svc, "R51.9")]
        prim, sec = rng.choice(support), None
        if scen in ("med_nec", "err_secondary_dx"):
            ch = w.dx[support[0]][2]
            pool = [x[0] for x in DIAGNOSES if x[2] == ch and x[0] not in support and x[0] != "M54"]
            pool = pool or [x[0] for x in DIAGNOSES if x[0] not in support and x[0] != "M54"]
            prim = rng.choice(pool)
            sec = rng.choice(support) if scen == "err_secondary_dx" else None
        elif rng.random() < 0.35:
            sec = rng.choice([x[0] for x in DIAGNOSES if x[0] not in (prim, "M54")])

        req = dict(request_id=rid, request_number=f"PA-2025-{rid * 7 + 104000:06d}", member_id=member[0],
                   enrollment_id=enr[0], requesting_provider_id=prov[0],
                   servicing_facility_id=None if (is_drug or rid % 8 == 0) else prov[6],
                   service_state=st, request_type=rtype, urgency=urgency, is_telehealth=tele,
                   referral_on_file=0 if scen == "no_referral" else int(features(w, enr, prov, svc, dos)["ref_needed"] or rng.random() < 0.3),
                   primary_icd10=prim, secondary_icd10=sec, submitted_at=sub.isoformat(sep=" "),
                   service_date=iso(dos), due_at=due, scenario=scen)

        # line items
        def units_for(s, f):
            if s in w.drug:
                return w.drug[s][8] or 30
            if s in ("97110", "97140"):
                return (f["max_units"] + 12) if scen == "partial_units" else min(12, f["max_units"] or 12)
            return 240 if s == "97153" else 1
        f0 = features(w, enr, prov, svc, dos)
        req_lines = [dict(svc=svc, units=units_for(svc, f0))]
        dxset = {prim, sec} - {None}
        extra = [s for s in SPEC_SERVICES[w.spec[prov[5]][1]] if s != svc and s in w.proc
                 and (not w.crit.get(s) or w.crit[s] & dxset)]
        if not is_drug and extra and second_lines < 10 and scen in ("clean", "clean_nopa", "lic_probation", "partial_units",
                                              "oon", "err_doc_overlooked"):
            s2 = rng.choice(extra)
            req_lines.append(dict(svc=s2, units=units_for(s2, features(w, enr, prov, s2, dos))))
            second_lines += 1
        for i, ln in enumerate(req_lines, start=1):
            ln.update(line_number=i, request_id=rid)

        # documents
        t = sub
        req_docs = []
        for ln in req_lines:
            pa, rdoc, *_ = w.requirements(st, w.category(enr[2]), ln["svc"], dos)
            if not rdoc or not pa:
                continue
            if scen == "rule_change":
                rdoc = "CLINICAL_NOTES"  # provider followed the policy in force when they submitted
            if scen in NO_DOCS:
                continue
            t = t + timedelta(hours=rng.randint(1, 30))
            if t > datetime.combine(AS_OF, datetime.min.time()).replace(hour=17):
                t = datetime.combine(AS_OF, datetime.min.time()).replace(hour=9)
            complete = 0 if (scen == "doc_incomplete_new" and ln["line_number"] == 1) else int(rng.random() > 0.04)
            req_docs.append(dict(request_id=rid, document_type=rdoc, received_at=t.isoformat(sep=" "),
                                 author_provider_id=prov[0] if rng.random() > 0.1 else None,
                                 page_count=rng.randint(1, 24) if rng.random() > 0.1 else None, is_complete=complete,
                                 summary=DOC_SUMMARIES.get(rdoc) if complete else "Document truncated; pages 3+ missing from fax."))
        if rid % 6 == 1 and scen not in RECENT and scen not in NO_DOCS:
            t = t + timedelta(hours=rng.randint(1, 20))
            req_docs.append(dict(request_id=rid, document_type="CLINICAL_NOTES", received_at=t.isoformat(sep=" "),
                                 author_provider_id=prov[0], page_count=rng.randint(2, 12), is_complete=1,
                                 summary="Supplemental progress note."))
        reqs.append(req)
        lines.extend(req_lines)
        docs.extend(req_docs)
    return reqs, lines, docs


def adjudicate(w, reqs, lines, docs, reviewers):
    """Set initial decisions from the rule engine, then apply reviewer behavior."""
    by_role = {}
    for r in reviewers:
        by_role.setdefault(r[4], []).append(r[0])
    active = {r[0] for r in reviewers if r[7]}
    history = []
    pend_cutoff = datetime.combine(AS_OF, datetime.min.time()).replace(hour=17)

    def pick(role, exclude=()):
        pool = [x for x in by_role[role] if x in active and x not in exclude]
        return rng.choice(pool)

    for req in reqs:
        rid, scen = req["request_id"], req["scenario"]
        rl = [l for l in lines if l["request_id"] == rid]
        rd = [x for x in docs if x["request_id"] == rid]
        sub = dt(req["submitted_at"])
        last_doc = max([dt(x["received_at"]) for x in rd], default=sub)
        T = max(last_doc + timedelta(hours=2), sub + timedelta(hours=rng.randint(4, 96)))
        if scen in RECENT:
            T = pend_cutoff
        status, code, res, note = evaluate(w, req, rl, rd, min(T, pend_cutoff))
        if status == "PENDING" and T < req["due_at"] and scen not in RECENT and "Awaiting" in note:
            T = req["due_at"]  # payer waits for documentation until the regulatory deadline
            status, code, res, note = evaluate(w, req, rl, rd, min(T, pend_cutoff))
        if T > pend_cutoff:
            status, code = "PENDING", None
            res = [("PENDING", None, None) for _ in rl]
        # reviewer errors (the engine result is what the data supports; the reviewer did otherwise)
        if scen == "err_missed_stale":
            status, code, note = "APPROVED", None, "License verified (primary source check skipped - cached status ACTIVE)"
            res = [("APPROVED", None, l["units"]) for l in rl]
        elif scen == "err_doc_overlooked":
            status, code, note = "DENIED", "DOC-02", "Required documentation not located in case file"
            res = [("DENIED", "DOC-02", 0)] + list(res[1:])
            rd[0]["received_at"] = (T - timedelta(hours=2)).isoformat(sep=" ")
        elif scen == "err_secondary_dx":
            status, code, note = "DENIED", "MN-02", "Primary diagnosis does not meet criteria"
            res = [("DENIED", "MN-02", 0) for _ in rl]

        req["status"], req["denial_code"] = status, code
        req["initial_status"] = status
        req["decision_at"] = None if status == "PENDING" else T.isoformat(sep=" ")
        for l, (ls, lc, u) in zip(rl, res):
            l["line_status"], l["denial_code"], l["units_approved"] = ls, lc, u

        # reviewer assignment + history
        nopa = status == "APPROVED" and note and all("no authorization required" in n for n in note.split("; ") if n)
        if status == "PENDING":
            req["assigned_reviewer_id"] = None if rid % 3 == 0 else pick("NURSE_REVIEWER")
            history.append((rid, None, (sub + timedelta(minutes=rng.randint(2, 50))).isoformat(sep=" "),
                            "PENDED", None, "PENDING", note or "Queued for clinical review"))
            if scen == "doc_incomplete_new":
                history.append((rid, req["assigned_reviewer_id"] or pick("NURSE_REVIEWER"),
                                (sub + timedelta(hours=5)).isoformat(sep=" "), "INFO_REQUESTED", "PENDING", "PENDING",
                                "Faxed records incomplete; requested full documentation from provider office"))
            continue
        if nopa:
            req["assigned_reviewer_id"] = None
            history.append((rid, None, req["decision_at"], "AUTO_APPROVED", "PENDING", "APPROVED",
                            "Service does not require prior authorization for this plan/state"))
            continue
        if code and code.startswith("MN"):
            rv = pick("MEDICAL_DIRECTOR")
        elif req["request_type"] == "PHARMACY":
            rv = pick("PHARMACIST")
        elif scen == "lic_probation":
            nurse = pick("NURSE_REVIEWER")
            history.append((rid, nurse, (T - timedelta(hours=20)).isoformat(sep=" "), "ESCALATED", "PENDING", "PENDING",
                            "Provider license on probation; escalated to medical director"))
            rv = pick("MEDICAL_DIRECTOR")
        else:
            rv = pick("NURSE_REVIEWER")
        req["assigned_reviewer_id"] = rv
        history.append((rid, rv, req["decision_at"], status, "PENDING", status, note))
        if code and code.startswith("MN") and scen == "med_nec" and not any(h[3] == "PEER_TO_PEER" for h in history):
            history.append((rid, rv, (T + timedelta(days=3)).isoformat(sep=" "), "PEER_TO_PEER", status, status,
                            "Peer-to-peer held with ordering provider; denial maintained"))
        if scen == "err_missed_stale":
            history.append((rid, pick("MEDICAL_DIRECTOR"), (T + timedelta(days=41)).isoformat(sep=" "), "QA_FLAG",
                            status, status, "Retrospective audit: provider license expired before DOS; refer for recoupment"))
    return history


def build_appeals(w, reqs, lines, docs, reviewers, history):
    by_role = {}
    for r in reviewers:
        if r[7]:
            by_role.setdefault(r[4], []).append(r[0])
    appeals, limit, seen = [], datetime.combine(AS_OF, datetime.min.time()).replace(hour=17), {}
    for req in reqs:
        path = APPEAL_PATHS.get(req["scenario"])
        if path and isinstance(path[0], list):
            seen[req["scenario"]] = seen.get(req["scenario"], -1) + 1
            path = path[seen[req["scenario"]] % len(path)]
        if not path or req["decision_at"] is None:
            continue
        if req["initial_status"] != "DENIED" and req["scenario"] != "partial_units":
            continue
        if req["denial_code"] and not next(c for c in DENIAL_CODES if c[0] == req["denial_code"])[4]:
            continue
        rl = [l for l in lines if l["request_id"] == req["request_id"]]
        prev, used = dt(req["decision_at"]), {req["assigned_reviewer_id"]}
        is_mn = (req["denial_code"] or "").startswith("MN")
        for level, outcome in enumerate(path, start=1):
            filed = prev + timedelta(days=rng.randint(6, 25), hours=rng.randint(0, 8))
            if filed > limit:
                break
            decided = filed + timedelta(days=rng.randint(12, 28))
            if outcome == "PENDING" or decided > limit:
                outcome, decided = "PENDING", None
            if level == 3:
                rv = None
            else:
                role = "MEDICAL_DIRECTOR" if (is_mn or level == 2) else "APPEALS_SPECIALIST"
                rv = rng.choice([x for x in by_role[role] if x not in used])
                used.add(rv)
            new_doc = 0
            if outcome == "OVERTURNED" and req["scenario"] in ("doc_missing_old", "no_referral", "rx_nonformulary"):
                new_doc = 1
                dtype = {"no_referral": "REFERRAL", "rx_nonformulary": "LETTER_OF_MEDICAL_NECESSITY"}.get(
                    req["scenario"], w.requirements(req["service_state"], w.category(w.enr[req["enrollment_id"]][2]),
                                                    rl[0]["svc"], d(req["service_date"]))[1])
                docs.append(dict(request_id=req["request_id"], document_type=dtype,
                                 received_at=(filed + timedelta(hours=1)).isoformat(sep=" "),
                                 author_provider_id=req["requesting_provider_id"], page_count=rng.randint(1, 6),
                                 is_complete=1, summary=DOC_SUMMARIES[dtype] + " Submitted with appeal."))
            rationale = {
                "UPHELD": "Original determination supported by plan policy and submitted records.",
                "WITHDRAWN": "Withdrawn by requester; new request to be submitted once coverage is effective.",
                "PENDING": None,
                "OVERTURNED": {
                    "err_doc_overlooked": "Required documentation was in the case file before the original decision.",
                    "err_secondary_dx": "Secondary diagnosis on the request meets clinical criteria.",
                    "med_nec": "External reviewer found criteria met based on full clinical picture.",
                    "oon": "No in-network specialist within access standard; single-case agreement approved.",
                    "rule_change": "Request was complete under the policy in effect on the submission date.",
                }.get(req["scenario"], "New documentation satisfies requirements."),
            }[outcome]
            filed_by = "MEMBER" if req["scenario"].startswith("elig") else ("AUTHORIZED_REP" if level == 3 else "PROVIDER")
            appeals.append((req["request_id"], level, filed_by, filed.isoformat(sep=" "),
                            decided.isoformat(sep=" ") if decided else None, outcome, rv, new_doc, rationale))
            if outcome == "OVERTURNED":
                prior = req["status"]
                req["status"], req["denial_code"] = "APPROVED", None
                for l in rl:
                    if l["line_status"] == "DENIED":
                        l["line_status"], l["denial_code"], l["units_approved"] = "APPROVED", None, l["units"]
                history.append((req["request_id"], rv, decided.isoformat(sep=" "), "APPEAL_OVERTURNED", prior,
                                "APPROVED", f"Level {level} appeal overturned"))
                req["overturned_at"] = decided
            if outcome != "UPHELD":
                break
            prev = decided
    return appeals


def build_payments(w, reqs, lines):
    pays, limit = [], AS_OF
    tn = 880000
    line_ids = {}
    for i, l in enumerate(lines, start=1):
        line_ids[(l["request_id"], l["line_number"])] = i
    for req in reqs:
        if req["status"] != "APPROVED" or req["request_type"] == "PHARMACY":
            continue
        dos = d(req["service_date"])
        cat = w.category(w.enr[req["enrollment_id"]][2])
        approved_at = req.get("overturned_at") or dt(req["decision_at"])
        for l in [x for x in lines if x["request_id"] == req["request_id"] and x["line_status"] == "APPROVED"]:
            paid_on = max(dos, approved_at.date()) + timedelta(days=rng.randint(14, 30))
            if paid_on > limit:
                continue
            allowed = round(w.proc[l["svc"]][6] * (l["units_approved"] or 1) * rng.uniform(0.92, 1.08), 2)
            share = {"MEDICAID": 0.0, "MEDICARE": 0.20}.get(cat, rng.choice([0.1, 0.2, 0.3]))
            mem = round(allowed * share, 2)
            method = rng.choice(["EFT", "EFT", "EFT", "CHECK", "VIRTUAL_CARD"])
            tn += rng.randint(11, 97)
            lid = line_ids[(req["request_id"], l["line_number"])]
            pays.append([req["request_id"], lid, req["requesting_provider_id"], "ORIGINAL", iso(paid_on), allowed,
                         round(allowed - mem, 2), mem, method, f"{method[:3]}{tn}"])
            if req.get("overturned_at"):
                tn += 7
                pays.append([req["request_id"], lid, req["requesting_provider_id"], "INTEREST", iso(paid_on), 0.0,
                             round(allowed * 0.012, 2), 0.0, method, f"{method[:3]}{tn}"])
            if req["scenario"] == "err_missed_stale":
                tn += 7
                rec_on = min(paid_on + timedelta(days=45), limit)
                pays.append([req["request_id"], lid, req["requesting_provider_id"], "RECOUPMENT", iso(rec_on),
                             -allowed, -round(allowed - mem, 2), -mem, "EFT", f"EFT{tn}"])
    # claim corrections (coding/fee schedule adjustments) on a subset of original payments
    originals = [p for p in pays if p[3] == "ORIGINAL"]
    for p in originals[::2][: max(0, 32 - len(pays))]:
        tn += rng.randint(11, 97)
        delta = round(p[5] * rng.choice([-0.06, -0.03, 0.04, 0.05]), 2)
        adj_on = min(d(p[4]) + timedelta(days=rng.randint(20, 60)), limit)
        pays.append([p[0], p[1], p[2], "ADJUSTMENT", iso(adj_on), delta, delta, 0.0, p[8], f"{p[8][:3]}{tn}"])
    return [[i] + p for i, p in enumerate(pays, start=1)]


# ===================================================================== main
def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    itypes = build_insurance_types()
    plans = build_plans(itypes)
    specialties = [(i, *s) for i, s in enumerate(SPECIALTIES, start=1)]
    facilities = build_facilities()
    providers, home = build_providers(specialties, facilities)
    licenses = build_licenses(providers, home)
    scope_rules = build_scope_rules()
    drug_rows = [(i, f"{rng.randint(10000, 79999)}-{rng.randint(1000, 9999)}-{rng.randint(10, 99)}", *dr)
                 for i, dr in enumerate(DRUGS, start=1)]
    brand_id = {r[2]: r[0] for r in drug_rows}
    criteria = [(i, c, brand_id[dr] if dr else None, dx, src, f"{src} guideline (synthetic ref {100 + i})")
                for i, (c, dr, dx, src) in enumerate(CRITERIA, start=1)]
    coverage_rows = [(i, *r) for i, r in enumerate(COVERAGE_RULES, start=1)]
    members, member_seed, _ = build_members(plans, itypes, providers)
    enrollments, plan_model = build_enrollments(member_seed, plans, itypes, providers, specialties, home)
    network = build_network(providers, plans, home, plan_model)
    reviewers = build_reviewers()

    w = World(itypes=itypes, plans=plans, members=members, enrollments=enrollments, providers=providers,
              specialties=specialties, licenses=licenses, scope_rules=scope_rules, drug_rows=drug_rows,
              criteria=criteria, network=network, coverage_rows=coverage_rows)
    reqs, lines, docs = build_requests(w)
    history = adjudicate(w, reqs, lines, docs, reviewers)
    appeals = build_appeals(w, reqs, lines, docs, reviewers, history)
    payments = build_payments(w, reqs, lines)

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)

    def ins(table, rows):
        if rows:
            con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)

    ins("states", STATES)
    ins("insurance_types", itypes)
    ins("insurance_plans", plans)
    ins("specialties", specialties)
    ins("facilities", facilities)
    ins("providers", providers)
    ins("provider_licenses", licenses)
    ins("procedure_codes", PROCEDURES)
    ins("license_scope_rules", scope_rules)
    ins("diagnosis_codes", [(x[0], x[1], x[2], 0 if x[0] == "M54" else 1) for x in DIAGNOSES])
    ins("drugs", drug_rows)
    ins("clinical_criteria", criteria)
    ins("coverage_rules", coverage_rows)
    ins("denial_reason_codes", DENIAL_CODES)
    ins("members", members)
    ins("member_enrollments", enrollments)
    ins("provider_network", network)
    ins("reviewers", reviewers)
    ins("auth_requests", [(r["request_id"], r["request_number"], r["member_id"], r["enrollment_id"],
                           r["requesting_provider_id"], r["servicing_facility_id"], r["service_state"],
                           r["request_type"], r["urgency"], r["is_telehealth"], r["referral_on_file"],
                           r["primary_icd10"], r["secondary_icd10"], r["submitted_at"], r["service_date"],
                           r["status"], r["decision_at"], r["denial_code"], r["assigned_reviewer_id"]) for r in reqs])
    ins("request_line_items", [(i, l["request_id"], l["line_number"],
                                l["svc"] if l["svc"] in w.proc else None,
                                w.drug[l["svc"]][0] if l["svc"] in w.drug else None,
                                l["units"], l["units_approved"],
                                round((w.proc[l["svc"]][6] if l["svc"] in w.proc else 450) * l["units"]
                                      * (1.6 + (i * 37 % 100) / 100), 2),
                                l["line_status"], l["denial_code"]) for i, l in enumerate(lines, start=1)])
    docs.sort(key=lambda x: (x["request_id"], x["received_at"]))
    ins("clinical_documents", [(i, x["request_id"], x["document_type"], x["received_at"], x["author_provider_id"],
                                x["page_count"], x["is_complete"], x["summary"]) for i, x in enumerate(docs, start=1)])
    history.sort(key=lambda h: (h[0], h[2]))
    ins("review_history", [(i, *h) for i, h in enumerate(history, start=1)])
    ins("appeals", [(i, *a) for i, a in enumerate(appeals, start=1)])
    ins("payments", payments)
    con.commit()

    assert not con.execute("PRAGMA foreign_key_check").fetchall(), "orphan rows"
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")]
    for (sql,) in con.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY rowid"):
        print(sql + ";\n")
    print(f"{'table':<24}rows")
    for t in tables:
        print(f"{t:<24}{con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()
