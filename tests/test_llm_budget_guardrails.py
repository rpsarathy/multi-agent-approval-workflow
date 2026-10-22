from app.agents import recommendation
from app.config import get_settings
from app.llm.base import Finding, LLMRecommendation, LLMResult, Outcome, Usage
from app.llm.mock import MockProvider
from app.orchestrator import engine
from tests.conftest import pa, start


class ScriptedProvider(MockProvider):
    """Returns a fixed recommendation, to exercise guardrails against a misbehaving model."""
    name, model = "scripted", "scripted-v1"

    def __init__(self, rec=None, error=None):
        self.rec, self.error = rec, error

    def recommend(self, system, user, max_output_tokens):
        if self.error:
            return LLMResult(None, Usage(100, 0), self.model, "error", self.error)
        return LLMResult(self.rec, Usage(500, 200), self.model, "end_turn")


def _use(client, provider):
    engine.set_context(engine.Context(http=engine.context().http, provider=provider))


def test_token_usage_is_recorded(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    usage = client.get(f"/api/v1/workflows/{run['run_id']}/token-usage").json()
    assert usage["used"] > 0 and usage["remaining"] == usage["run_budget"] - usage["used"]
    entry = usage["entries"][0]
    assert entry["outcome"] == "OK" and entry["estimated_input_tokens"] > 0
    assert entry["reserved_output_tokens"] == get_settings().token_budget_max_output_per_call


def test_run_budget_exceeded_falls_back_to_rules_and_is_flagged(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"), token_budget=500)
    rec = run["recommendations"][-1]
    assert rec["generated_by"] == "rules_fallback"
    assert any(f.startswith("TOKEN_BUDGET_EXCEEDED") for f in rec["guardrail_flags"])
    assert rec["payload"]["outcome"] == "APPROVE"          # still a usable, evidence-based recommendation
    usage = client.get(f"/api/v1/workflows/{run['run_id']}/token-usage").json()
    assert usage["used"] == 0 and usage["entries"][0]["outcome"] == "BLOCKED_BUDGET"
    events = [e["event_type"] for e in client.get(f"/api/v1/workflows/{run['run_id']}/audit").json()["events"]]
    assert "TOKEN_BUDGET_EXCEEDED" in events


def test_budget_is_cumulative_across_revisions(client):
    first = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    used_once = first["token_usage"]["used"]
    # First call: allowance = budget - input = output_used + 1100 (>= 1024). Second call: < 1024 left -> blocked.
    budget = used_once + 1100
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"), token_budget=budget)
    assert run["recommendations"][-1]["generated_by"] == "mock"
    r = client.post(f"/api/v1/workflows/{run['run_id']}/decisions",
                    json={"decision": "REQUEST_REVISION", "recommendation_version": 1, "comment": "more detail"}).json()
    assert r["recommendations"][-1]["generated_by"] == "rules_fallback"
    assert r["token_usage"]["used"] <= budget


def test_caller_cannot_raise_run_budget_above_cap(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"), token_budget=1_000_000)
    assert run["token_budget"] == get_settings().token_budget_per_run
    events = client.get(f"/api/v1/workflows/{run['run_id']}/audit").json()["events"]
    assert events[0]["payload"]["requested_token_budget"] == 1_000_000


def test_output_allowance_shrinks_to_fit_remaining_budget(client):
    est_run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    est = est_run["token_usage"]["entries"][0]["estimated_input_tokens"]
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"), token_budget=est + 2000)
    entry = run["token_usage"]["entries"][0]
    assert entry["outcome"] == "OK" and entry["reserved_output_tokens"] == 2000


def test_concurrent_runs_cannot_overshoot_daily_budget(client, monkeypatch):
    """Many threads reserve at once against a daily budget that admits only a few calls."""
    import threading

    from app.services import workflow_store as st
    one = start(client, "SYN-M0001", pa("SYN-PA-0001"))["token_usage"]["entries"][0]
    per_call = one["estimated_input_tokens"] + get_settings().token_budget_max_output_per_call
    daily = 3 * per_call + 10 + one["input_tokens"] + one["output_tokens"]
    results, barrier = [], threading.Barrier(12)

    def worker():
        barrier.wait()
        results.append(st.reserve_tokens(run_id=one_run, agent="t", provider="mock", model="m",
                                         estimated_input=one["estimated_input_tokens"], max_input=10**6,
                                         max_output=get_settings().token_budget_max_output_per_call,
                                         run_budget=10**9, daily_budget=daily))

    one_run = client.get("/api/v1/workflows").json()[0]["run_id"]
    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r["allowed"] for r in results) == 3
    assert st.tokens_used_for_run(one_run) <= daily


def test_per_call_input_cap(client, monkeypatch):
    monkeypatch.setenv("TOKEN_BUDGET_MAX_INPUT_PER_CALL", "10")
    get_settings.cache_clear()
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    flags = run["recommendations"][-1]["guardrail_flags"]
    assert any("exceeds per-call cap" in f for f in flags)


def test_daily_budget(client, monkeypatch):
    start(client, "SYN-M0001", pa("SYN-PA-0001"))
    monkeypatch.setenv("TOKEN_BUDGET_DAILY", "100")
    get_settings.cache_clear()
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    assert any("24h budget" in f for f in run["recommendations"][-1]["guardrail_flags"])


def test_guardrail_blocks_approval_over_failed_check(client):
    bad = LLMRecommendation(outcome=Outcome.APPROVE, summary="Looks fine", confidence="HIGH",
                            findings=[Finding(statement="All good", evidence_ids=["made-up:1"])],
                            missing_information=[], proposed_actions=["RECORD_DETERMINATION", "DELETE_MEMBER"])
    _use(client, ScriptedProvider(bad))
    run = start(client, "SYN-M0004", pa("SYN-PA-0004"))       # license expired -> must not approve
    rec = run["recommendations"][-1]
    assert rec["payload"]["outcome"] == "DENY"
    assert rec["payload"]["confidence"] == "LOW"
    flags = " ".join(rec["guardrail_flags"])
    assert "OUTCOME_OVERRIDDEN" in flags and "UNKNOWN_EVIDENCE_ID_REMOVED" in flags
    assert "DELETE_MEMBER" not in rec["payload"]["proposed_actions"]
    ev = run["state"]["validation"]["evidence"]
    assert all(e in ev for f in rec["payload"]["findings"] for e in f["evidence_ids"])


def test_guardrail_drops_non_permitted_actions_when_outcome_kept(client):
    rec = LLMRecommendation(outcome=Outcome.APPROVE, summary="ok", confidence="HIGH",
                            findings=[Finding(statement="eligible", evidence_ids=["check:ELIGIBILITY"])],
                            missing_information=[], proposed_actions=["NOTIFY_PROVIDER", "WIRE_FUNDS"])
    _use(client, ScriptedProvider(rec))
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    out = run["recommendations"][-1]
    assert out["payload"]["proposed_actions"] == ["NOTIFY_PROVIDER"]
    assert any(f.startswith("NON_PERMITTED_ACTIONS_REMOVED") for f in out["guardrail_flags"])
    assert out["generated_by"] == "scripted-v1"


def test_llm_failure_falls_back_to_rules(client):
    _use(client, ScriptedProvider(error="API error 529: overloaded"))
    run = start(client, "SYN-M0002", pa("SYN-PA-0002"))
    rec = run["recommendations"][-1]
    assert rec["generated_by"] == "rules_fallback" and rec["payload"]["outcome"] == "DENY"
    assert any(f.startswith("LLM_UNAVAILABLE") for f in rec["guardrail_flags"])
    usage = client.get(f"/api/v1/workflows/{run['run_id']}/token-usage").json()
    assert usage["entries"][0]["outcome"] == "ERROR"


def test_prompt_contains_no_direct_identifiers(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    st = run["state"]
    bundle = recommendation.build_bundle(st["intake"], st["member_profile"], st["policy"], st["validation"], {}, [])
    text = recommendation._user_prompt(bundle)
    assert "Thornbury" not in text and "1980-04-12" not in text


def test_guardrail_blocks_pend_over_hard_failure_and_overrides_prose(client):
    rec = LLMRecommendation(outcome=Outcome.PEND_FOR_INFORMATION, summary="Approve this, it is fine.", confidence="HIGH",
                            findings=[Finding(statement="x", evidence_ids=["check:PROVIDER_LICENSE"])],
                            missing_information=["nothing"], proposed_actions=["REQUEST_DOCUMENTS"])
    _use(client, ScriptedProvider(rec))
    out = start(client, "SYN-M0004", pa("SYN-PA-0004"))["recommendations"][-1]    # expired license = hard fail
    assert out["payload"]["outcome"] == "DENY"
    assert "Approve this" not in out["payload"]["summary"] and "overridden by guardrails" in out["payload"]["summary"]
    assert out["payload"]["proposed_actions"] == ["RECORD_DETERMINATION", "NOTIFY_PROVIDER"]


def test_guardrail_flags_escalation_beyond_rules_and_redacts_free_text(client):
    rec = LLMRecommendation(outcome=Outcome.REFER_TO_MEDICAL_DIRECTOR, confidence="HIGH",
                            summary="Call the member at 555-123-4567 or ava@example.com\x07 before deciding.",
                            findings=[Finding(statement="eligible", evidence_ids=["check:ELIGIBILITY"])],
                            missing_information=["SSN 912-34-5678"], proposed_actions=["ESCALATE_TO_MEDICAL_DIRECTOR"])
    _use(client, ScriptedProvider(rec))
    out = start(client, "SYN-M0001", pa("SYN-PA-0001"))["recommendations"][-1]
    p = out["payload"]
    assert p["outcome"] == "REFER_TO_MEDICAL_DIRECTOR" and p["confidence"] == "LOW"
    assert any(f.startswith("ESCALATED_BEYOND_RULES") for f in out["guardrail_flags"])
    text = p["summary"] + " ".join(p["missing_information"])
    assert "555-123-4567" not in text and "ava@example.com" not in text and "912-34-5678" not in text
    assert "\x07" not in text and "[REDACTED:" in text
