"""PII containment: direct identifiers stored in the system of record must never reach agents,
the LLM, workflow state, the audit log, notices, or any public API response."""
import pytest

from app import models as m
from app import privacy
from app.agents.tools import TOOLS
from app.db import session_scope
from app.llm.base import Finding, LLMRecommendation, Outcome
from app.orchestrator import engine
from app.security import agent_token
from evals.harness import SpyProvider, leaks, pii_values, sink_rows, walk_model_fields
from tests.conftest import decide, pa, start


def assert_no_pii(obj, where: str):
    hits = leaks(obj)
    assert hits == [], f"PII found in {where}: {hits[:5]}"


@pytest.fixture()
def spy(client):
    p = SpyProvider()
    engine.set_context(engine.Context(http=engine.context().http, provider=p))
    return p


# -------------------------------------------------------------------- tests
def test_seed_really_contains_pii_so_these_tests_mean_something(client):
    assert len(pii_values()) >= 100
    with session_scope() as s:
        doc = s.query(m.ClinicalDocument).filter_by(request_number="SYN-PA-0001").one()
        assert "Thornbury" in doc.summary and "555-0101" in doc.summary


def test_agent_facing_response_models_have_no_pii_fields():
    for spec in TOOLS.values():
        for model_name, field in walk_model_fields(spec.output_model):
            assert field not in privacy.PII_FIELD_NAMES, f"{model_name}.{field} exposes PII to agents"


def test_internal_api_returns_no_pii_for_any_member(client, internal):
    def h(agent):
        return {"X-Agent-Id": agent, "X-Agent-Token": agent_token(agent)}
    responses = []
    with session_scope() as s:
        members = [x.member_id for x in s.query(m.Member).all()]
        requests = [(r.member_id, r.request_number, r.requesting_provider_id, r.service_state)
                    for r in s.query(m.AuthRequest).all()]
    for mid in members:
        responses.append(internal.get(f"/internal/v1/members/{mid}", headers=h("member_retrieval_agent")).json())
        responses.append(internal.get(f"/internal/v1/members/{mid}/age", params={"on": "2026-11-16"},
                                      headers=h("policy_retrieval_agent")).json())
    for mid, rn, prov, st in requests:
        responses.append(internal.get(f"/internal/v1/members/{mid}/auth-requests/{rn}",
                                      headers=h("policy_retrieval_agent")).json())
        responses.append(internal.get(f"/internal/v1/providers/{prov}", params={"state_code": st, "plan_id": 1},
                                      headers=h("policy_retrieval_agent")).json())
    assert all("detail" not in r for r in responses)
    assert_no_pii(responses, "internal API responses")


def test_age_is_computed_server_side_without_exposing_dob(client, internal):
    r = internal.get("/internal/v1/members/SYN-M0010/age", params={"on": "2026-11-16"},
                     headers={"X-Agent-Id": "policy_retrieval_agent", "X-Agent-Token": agent_token("policy_retrieval_agent")})
    assert r.json() == {"member_id": "SYN-M0010", "on": "2026-11-16", "age_years": 16}
    # the member agent may not call it, and the policy agent may not read the profile
    r = internal.get("/internal/v1/members/SYN-M0010/age", params={"on": "2026-11-16"},
                     headers={"X-Agent-Id": "member_retrieval_agent", "X-Agent-Token": agent_token("member_retrieval_agent")})
    assert r.status_code == 403


def test_full_lifecycle_of_every_scenario_leaks_no_pii(client, spy):
    from app.seed import SCENARIOS
    api_responses = []
    for mid, (req, _) in SCENARIOS.items():
        request = pa(req) if req else {"request_type": "COVERAGE_INQUIRY", "cpt_code": "97110", "icd10_code": "M62.81",
                                       "units": 30, "service_date": "2026-11-20"}
        run = start(client, mid, request)
        run = decide(client, run, "REQUEST_REVISION", "SYN-R02", "Please double-check the evidence").json()
        run = decide(client, run, "APPROVE", "SYN-R02").json()
        assert run["status"] == "COMPLETED", (mid, run["error"])
        rid = run["run_id"]
        api_responses += [run, client.get(f"/api/v1/workflows/{rid}/audit").json(),
                          client.get(f"/api/v1/workflows/{rid}/token-usage").json()]
    api_responses.append(client.get("/api/v1/workflows").json())
    assert len(spy.prompts) >= 40
    assert_no_pii(spy.prompts, "LLM prompts")
    assert_no_pii(sink_rows(), "persisted workflow data")
    assert_no_pii(api_responses, "public API responses")


ADVERSARIAL_NOTE = ("Patient Ava Thornbury (DOB 04/12/1980) called from (415) 555-0101, e-mail ava.t@example.invalid, "
                    "SSN 912-34-5601, lives at 4152 Granite Blvd. Her neighbour bob@example.org also called 212-555-0199.")


def test_pii_in_request_notes_is_redacted_before_storage_and_prompt(client, spy):
    run = start(client, "SYN-M0001", {**pa("SYN-PA-0001"), "notes": ADVERSARIAL_NOTE})
    notes = run["request"]["notes"]
    for kind in ("NAME", "DOB", "PHONE", "EMAIL", "SSN", "ADDRESS"):
        assert f"[REDACTED:{kind}]" in notes
    assert_no_pii(sink_rows(), "persisted workflow data")
    assert_no_pii(spy.prompts, "LLM prompts")
    assert "bob@example.org" not in str(sink_rows()) and "212-555-0199" not in str(spy.prompts)
    events = client.get(f"/api/v1/workflows/{run['run_id']}/audit").json()["events"]
    redaction = next(e for e in events if e["event_type"] == "PII_REDACTED")
    assert redaction["payload"]["kinds"]["NAME"] >= 1 and "Thornbury" not in str(redaction)


def test_pii_in_reviewer_comment_is_redacted(client, spy):
    run = start(client, "SYN-M0007", pa("SYN-PA-0007"))
    comment = "Spoke to Grace Lindqvist on (415) 555-0107; DOB July 19, 1987. Recheck documents."
    run = decide(client, run, "REQUEST_REVISION", "SYN-R01", comment).json()
    stored = run["approvals"][0]["comment"]
    assert "Lindqvist" not in stored and "555-0107" not in stored and "1987" not in stored
    assert "Recheck documents." in stored                    # the non-PII content is kept
    assert run["state"]["reviewer_feedback"][0]["comment"] == stored
    assert_no_pii(sink_rows(), "persisted workflow data")
    assert_no_pii(spy.prompts, "LLM prompts")


def test_pii_written_by_the_model_is_redacted_before_storage_and_notices(client):
    leaky = LLMRecommendation(
        outcome=Outcome.APPROVE, confidence="HIGH",
        summary="Approve for Ava Thornbury (born 1980-04-12); confirm at ava.t@example.invalid or 415-555-0101.",
        findings=[Finding(statement="Thornbury is eligible", evidence_ids=["check:ELIGIBILITY"])],
        missing_information=["Call Ava at (415) 555-0101"], proposed_actions=["RECORD_DETERMINATION", "NOTIFY_PROVIDER"])
    engine.set_context(engine.Context(http=engine.context().http, provider=SpyProvider(scripted=leaky)))
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    rec = run["recommendations"][-1]
    assert rec["payload"]["outcome"] == "APPROVE"
    assert any(f.startswith("PII_REDACTED_FROM_MODEL_OUTPUT") for f in rec["guardrail_flags"])
    run = decide(client, run, "APPROVE", "SYN-R01").json()
    assert run["status"] == "COMPLETED"
    assert_no_pii(sink_rows(), "persisted workflow data incl. notices and determination notes")


def test_prompt_filter_is_a_last_line_of_defence(client, spy, monkeypatch):
    from app.agents import recommendation
    real = recommendation.build_bundle

    def leaky_bundle(*a, **kw):
        b = real(*a, **kw)
        b["member"]["contact"] = "Ava Thornbury, ava.t@example.invalid, 912-34-5601"   # a future bug
        return b

    monkeypatch.setattr(recommendation, "build_bundle", leaky_bundle)
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    assert any(f.startswith("PII_REDACTED_FROM_PROMPT") for f in run["recommendations"][-1]["guardrail_flags"])
    assert_no_pii(spy.prompts, "LLM prompts")


def test_ui_reviewer_listing_exposes_staff_names_only(client):
    # Reviewer display names are staff (not member) data; members never appear here.
    assert_no_pii(client.get("/api/v1/demo/reviewers").json(), "reviewer listing")


@pytest.mark.parametrize("text,kinds", [
    ("DOB: 04/12/1980", {"DOB"}), ("date of birth 1980-04-12", {"DOB"}), ("born on April 12, 1980", {"DOB"}),
    ("call (415) 555-0101", {"PHONE"}), ("call 415.555.0101", {"PHONE"}), ("call +1 415 555 0101", {"PHONE"}),
    ("ssn 912-34-5601", {"SSN"}), ("SSN: 912345601", {"SSN"}), ("mail a.b+c@x.co.uk", {"EMAIL"}),
    ("lives at 4152 Granite Blvd", {"ADDRESS"}),
])
def test_pattern_redaction_catches_identifier_formats(text, kinds):
    assert set(privacy.redact(text).kinds) == kinds


@pytest.mark.parametrize("benign", [
    "Service date 2026-11-16, submitted 2026-10-19", "NPI 9000000029", "CPT 72148 dx M51.26 units 30",
    "Request SYN-PA-0001 enrollment 12", "Rule SYN-CA imaging 4.2 effective 2020-01-01", "Member SYN-M0001 age 46",
])
def test_pattern_redaction_leaves_clinical_and_admin_data_alone(benign):
    assert privacy.redact(benign).text == benign
