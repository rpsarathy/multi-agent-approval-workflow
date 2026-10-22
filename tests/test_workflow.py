import pytest

from app import models as m
from app.config import get_settings
from app.db import session_scope

from app.orchestrator import engine
from app.services import workflow_store as store
from tests.conftest import auth, decide, pa, start

EXPECTED = {
    "SYN-PA-0001": "APPROVE",
    "SYN-PA-0002": "DENY",
    "SYN-PA-0003": "REFER_TO_MEDICAL_DIRECTOR",
    "SYN-PA-0004": "DENY",
    "SYN-PA-0005": "DENY",
    "SYN-PA-0006": "DENY",
    "SYN-PA-0007": "PEND_FOR_INFORMATION",
    "SYN-PA-0008": "REFER_TO_MEDICAL_DIRECTOR",
    "SYN-PA-0010": "DENY",
}


@pytest.mark.parametrize("request_number,outcome", EXPECTED.items())
def test_each_scenario_reaches_approval_with_expected_outcome(client, request_number, outcome):
    member = "SYN-M00" + request_number[-2:]
    run = start(client, member, pa(request_number))
    assert run["status"] == "AWAITING_APPROVAL"
    rec = run["recommendations"][-1]
    assert rec["payload"]["outcome"] == outcome
    assert rec["generated_by"] == "mock"
    assert rec["guardrail_flags"] == []
    # every finding cites evidence that exists in the evidence catalog
    evidence = run["state"]["validation"]["evidence"]
    assert rec["payload"]["findings"]
    for f in rec["payload"]["findings"]:
        assert f["evidence_ids"] and all(e in evidence for e in f["evidence_ids"])
    # no side effects before approval
    assert run["actions"] == []


def test_mock_mode_is_deterministic(client):
    a = start(client, "SYN-M0007", pa("SYN-PA-0007"))["recommendations"][-1]["payload"]
    b = start(client, "SYN-M0007", pa("SYN-PA-0007"))["recommendations"][-1]["payload"]
    assert a == b


def test_validation_detects_missing_and_conflicting_data(client):
    v = start(client, "SYN-M0007", pa("SYN-PA-0007"))["state"]["validation"]
    assert {i["code"] for i in v["data_issues"] if i["kind"] == "MISSING"} == {"LICENSE_EXPIRY", "REQUIRED_DOCUMENT"}
    v = start(client, "SYN-M0008", pa("SYN-PA-0008"))["state"]["validation"]
    assert any(i["code"] == "MULTIPLE_ACTIVE_ENROLLMENTS" for i in v["data_issues"])
    v = start(client, "SYN-M0003", pa("SYN-PA-0003"))["state"]["validation"]
    conflict = next(i for i in v["data_issues"] if i["code"] == "OVERLAPPING_POLICY_RULES")
    assert set(conflict["evidence_ids"]) == {"coverage_rule:6", "coverage_rule:7"}


def test_approve_executes_permitted_actions_and_completes(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    r = decide(client, run, "APPROVE", "SYN-R01")
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "COMPLETED"
    assert [a["action_type"] for a in run["actions"]] == ["RECORD_DETERMINATION", "NOTIFY_PROVIDER"]
    assert all(a["status"] == "SUCCEEDED" for a in run["actions"])
    det = run["actions"][0]["result"]
    assert det["previous_status"] == "PENDING" and det["status"] == "APPROVED"
    assert run["approvals"][0]["decision"] == "APPROVE"


def test_reject_persists_and_executes_nothing(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    run = decide(client, run, "REJECT", "SYN-R01", "Not convinced").json()
    assert run["status"] == "REJECTED"
    assert run["actions"] == []
    assert run["approvals"][0]["comment"] == "Not convinced"
    # a rejected run cannot be approved afterwards
    assert decide(client, run, "APPROVE", "SYN-R01").status_code == 409


def test_request_revision_creates_new_version_and_stale_approval_is_refused(client):
    run = start(client, "SYN-M0007", pa("SYN-PA-0007"))
    run = decide(client, run, "REQUEST_REVISION", "SYN-R01", "Please list exactly which documents are missing").json()
    assert run["status"] == "AWAITING_APPROVAL"
    assert [r["version"] for r in run["recommendations"]] == [1, 2]
    assert "reviewer feedback" in run["recommendations"][-1]["payload"]["summary"]
    stale = decide(client, run, "APPROVE", "SYN-R01", version=1)
    assert stale.status_code == 409 and "stale" in stale.text
    run = decide(client, run, "APPROVE", "SYN-R01").json()
    assert run["status"] == "COMPLETED"
    assert [a["action_type"] for a in run["actions"]] == ["REQUEST_DOCUMENTS"]


def test_revision_requires_comment(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    assert decide(client, run, "REQUEST_REVISION", "SYN-R01").status_code == 422


def test_deny_requires_medical_director(client):
    run = start(client, "SYN-M0004", pa("SYN-PA-0004"))
    r = decide(client, run, "APPROVE", "SYN-R01")           # nurse cannot sign off an adverse determination
    assert r.status_code == 403
    assert store.get_run(run["run_id"])["status"] == "AWAITING_APPROVAL"
    events = [e["event_type"] for e in client.get(f"/api/v1/workflows/{run['run_id']}/audit").json()["events"]]
    assert "APPROVAL_DENIED" in events
    run = decide(client, run, "APPROVE", "SYN-R02").json()
    assert run["status"] == "COMPLETED"
    assert run["actions"][0]["result"]["status"] == "DENIED"


def test_inactive_reviewer_cannot_log_in_or_use_existing_token(client):
    r = client.post("/api/v1/auth/login", json={"reviewer_id": "SYN-R03", "passcode": "inactive-demo"},
                    headers={"Authorization": ""})
    assert r.status_code == 401
    # a token issued while active stops working on every endpoint as soon as the account is deactivated
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    h = auth(client, "SYN-R02")
    from app import models as m
    from app.db import session_scope
    with session_scope() as s:
        s.get(m.Reviewer, "SYN-R02").is_active = False
    rid = run["run_id"]
    for method, url, body in [("get", "/api/v1/workflows", None), ("get", f"/api/v1/workflows/{rid}", None),
                              ("get", f"/api/v1/workflows/{rid}/audit", None),
                              ("get", f"/api/v1/workflows/{rid}/token-usage", None),
                              ("post", f"/api/v1/workflows/{rid}/resume", None),
                              ("post", f"/api/v1/workflows/{rid}/decisions",
                               {"decision": "APPROVE", "recommendation_version": 1}),
                              ("post", "/api/v1/workflows", {"member_id": "SYN-M0001", "request": pa("SYN-PA-0001")})]:
        resp = client.request(method.upper(), url, json=body, headers=h)
        assert resp.status_code == 401, (url, resp.status_code)


def test_expired_reviewer_token_rejected(client, monkeypatch):
    monkeypatch.setenv("REVIEWER_TOKEN_TTL_SECONDS", "-1")
    get_settings.cache_clear()
    from app.security import issue_reviewer_token
    token, _ = issue_reviewer_token("SYN-R02")
    assert client.get("/api/v1/workflows", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_auth_required_and_identity_only_from_token(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    url = f"/api/v1/workflows/{run['run_id']}/decisions"
    body = {"decision": "APPROVE", "recommendation_version": 1}
    assert client.post(url, json=body, headers={"Authorization": ""}).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": "Bearer forged.token"}).status_code == 401
    # the reviewer identity cannot be supplied in the body
    assert client.post(url, json={**body, "reviewer_id": "SYN-R02"}).status_code == 422
    bad = client.post("/api/v1/auth/login", json={"reviewer_id": "SYN-R02", "passcode": "guess"}, headers={"Authorization": ""})
    assert bad.status_code == 401
    assert client.get("/api/v1/workflows", headers={"Authorization": ""}).status_code == 401


INQUIRY = {"request_type": "COVERAGE_INQUIRY", "cpt_code": "97110", "icd10_code": "M62.81", "units": 30,
           "service_date": "2026-11-20"}


def test_coverage_inquiry_over_unit_limit_is_a_partial_approval_and_records_nothing(client):
    run = start(client, "SYN-M0009", INQUIRY)
    p = run["recommendations"][-1]["payload"]
    assert p["outcome"] == "APPROVE"
    assert p["determination"] == {"status": "APPROVED", "denial_code": "COV-04", "units_approved": 20}
    units = next(c for c in run["state"]["validation"]["checks"] if c["check"] == "UNITS")
    assert units["status"] == "PARTIAL" and units["approved_units"] == 20
    run = decide(client, run, "APPROVE", "SYN-R01").json()
    assert [a["action_type"] for a in run["actions"]] == ["NOTIFY_PROVIDER"]     # no request to record against
    with session_scope() as s:
        notice = s.query(m.Notice).filter_by(member_id="SYN-M0009").one().body
    assert "20 unit(s) approved" in notice and "COV-04" in notice


def test_service_without_prior_auth_skips_clinical_review_like_the_reference(client):
    with session_scope() as s:
        s.get(m.CoverageRule, 4).requires_prior_auth = False
    run = start(client, "SYN-M0009", INQUIRY)
    checks = {c["check"]: c["status"] for c in run["state"]["validation"]["checks"]}
    assert checks["DOCUMENTATION"] == checks["MEDICAL_NECESSITY"] == checks["UNITS"] == "NOT_APPLICABLE"
    p = run["recommendations"][-1]["payload"]
    assert p["outcome"] == "APPROVE" and p["determination"]["units_approved"] == 30


@pytest.mark.parametrize("member,request_number,code", [
    ("SYN-M0002", "SYN-PA-0002", "REF-01"), ("SYN-M0004", "SYN-PA-0004", "LIC-01"),
    ("SYN-M0005", "SYN-PA-0005", "NET-01"), ("SYN-M0006", "SYN-PA-0006", "ELG-01"),
    ("SYN-M0010", "SYN-PA-0010", "COV-03"),
])
def test_denials_record_the_reference_denial_reason_code(client, member, request_number, code):
    run = start(client, member, pa(request_number))
    assert run["recommendations"][-1]["payload"]["determination"] == {"status": "DENIED", "denial_code": code,
                                                                      "units_approved": 0}
    run = decide(client, run, "APPROVE", "SYN-R02").json()
    assert run["status"] == "COMPLETED"
    with session_scope() as s:
        req = s.get(m.AuthRequest, request_number)
        assert (req.status, req.denial_code, req.units_approved) == ("DENIED", code, 0)
        assert s.get(m.DenialReason, code) is not None
        assert code in s.query(m.Notice).filter_by(request_number=request_number).one().body


def test_approval_records_units_and_no_reason_code(client):
    run = decide(client, start(client, "SYN-M0001", pa("SYN-PA-0001")), "APPROVE", "SYN-R01").json()
    assert run["status"] == "COMPLETED"
    with session_scope() as s:
        req = s.get(m.AuthRequest, "SYN-PA-0001")
        assert (req.status, req.denial_code, req.units_approved) == ("APPROVED", None, 1)


def test_medical_necessity_denial_uses_mn_code_by_icd_chapter(client):
    # Make SYN-PA-0007 complete so its only problem is the diagnosis (M54.50; criteria list M51.26, same chapter).
    from datetime import date as _date
    with session_scope() as s:
        s.get(m.ProviderLicense, 4).expiry_date = _date(2028, 1, 31)
        s.query(m.ClinicalDocument).filter_by(request_number="SYN-PA-0007").one().is_complete = True
    p = start(client, "SYN-M0007", pa("SYN-PA-0007"))["recommendations"][-1]["payload"]
    assert p["outcome"] == "DENY" and p["determination"]["denial_code"] == "MN-01"
    with session_scope() as s:
        s.get(m.AuthRequest, "SYN-PA-0007").primary_icd10 = "G47.33"      # different chapter -> MN-02
    p = start(client, "SYN-M0007", pa("SYN-PA-0007"))["recommendations"][-1]["payload"]
    assert p["determination"]["denial_code"] == "MN-02"


@pytest.mark.parametrize("submitted,status,outcome", [
    ("2026-12-31 09:00:00", "PASS", "APPROVE"),        # retro, 45 days after service: within limit
    ("2027-03-01 09:00:00", "FAIL", "DENY"),           # retro, 105 days after service: ADM-01
])
def test_retro_requests_are_checked_for_timely_filing(client, submitted, status, outcome):
    from datetime import datetime as _dt
    with session_scope() as s:
        s.get(m.AuthRequest, "SYN-PA-0001").submitted_at = _dt.fromisoformat(submitted)
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    check = next(c for c in run["state"]["validation"]["checks"] if c["check"] == "TIMELY_FILING")
    assert check["status"] == status
    p = run["recommendations"][-1]["payload"]
    assert p["outcome"] == outcome
    if outcome == "DENY":
        assert p["determination"]["denial_code"] == "ADM-01"


def test_every_seeded_request_is_filed_on_time(client):
    for req in ("SYN-PA-0001", "SYN-PA-0002", "SYN-PA-0003", "SYN-PA-0004", "SYN-PA-0005", "SYN-PA-0006",
                "SYN-PA-0007", "SYN-PA-0008", "SYN-PA-0010"):
        run = start(client, "SYN-M00" + req[-2:], pa(req))
        check = next(c for c in run["state"]["validation"]["checks"] if c["check"] == "TIMELY_FILING")
        assert check["status"] == "PASS" and "Prospective" in check["detail"], req


def test_unknown_diagnosis_is_invalid_request(client):
    run = start(client, "SYN-M0009", {**INQUIRY, "icd10_code": "Z99.99"})
    assert run["status"] == "INVALID_REQUEST" and "Z99.99" in run["error"]


def test_intake_rejects_bad_requests(client):
    r = client.post("/api/v1/workflows", json={"member_id": "SYN-M0001", "request": {"request_type": "PRIOR_AUTH_REVIEW"}})
    assert r.status_code == 422
    r = client.post("/api/v1/workflows", json={"member_id": "SYN-M0001",
                                               "request": {"request_type": "COVERAGE_INQUIRY", "cpt_code": "72148"}})
    assert r.status_code == 422


def test_unknown_member_or_cross_member_request_fails_safely(client):
    run = start(client, "SYN-M0099", pa("SYN-PA-0001"))
    assert run["status"] == "INVALID_REQUEST" and "not found" in run["error"]
    # member 2 cannot read member 1's request
    run = start(client, "SYN-M0002", pa("SYN-PA-0001"))
    assert run["status"] == "INVALID_REQUEST" and run["current_step"] == "retrieve_policy"
    assert run["recommendations"] == [] and run["actions"] == []
    assert client.post(f"/api/v1/workflows/{run['run_id']}/resume").status_code == 409


def test_failed_step_resumes_from_checkpoint(client, monkeypatch):
    calls = {"n": 0}
    original = engine.node_validate

    def flaky(run, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return original(run, ctx)

    monkeypatch.setattr(engine, "node_validate", flaky)
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    assert run["status"] == "FAILED" and run["current_step"] == "validate"
    steps_before = [c["step"] for c in run["checkpoints"]]
    run = client.post(f"/api/v1/workflows/{run['run_id']}/resume").json()
    assert run["status"] == "AWAITING_APPROVAL"
    # earlier steps were not re-executed: their checkpoints appear once
    steps = [c["step"] for c in run["checkpoints"]]
    assert steps[:len(steps_before)] == steps_before
    assert steps.count("retrieve_member") == 1
    assert client.post(f"/api/v1/workflows/{run['run_id']}/resume").status_code == 409


def test_action_failure_is_resumable_without_repeating_side_effects(client, monkeypatch):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    from app.agents import tools as tools_mod
    real_call = tools_mod.ToolClient.call

    def failing_notice(self, name, **kw):
        if name == "create_notice":
            raise tools_mod.ToolError(name, 503, "notice service down")
        return real_call(self, name, **kw)

    monkeypatch.setattr(tools_mod.ToolClient, "call", failing_notice)
    run = decide(client, run, "APPROVE", "SYN-R01").json()
    assert run["status"] == "FAILED" and run["current_step"] == "execute_actions"
    assert {a["action_type"]: a["status"] for a in run["actions"]} == {"RECORD_DETERMINATION": "SUCCEEDED",
                                                                       "NOTIFY_PROVIDER": "FAILED"}
    monkeypatch.setattr(tools_mod.ToolClient, "call", real_call)
    run = client.post(f"/api/v1/workflows/{run['run_id']}/resume").json()
    assert run["status"] == "COMPLETED"
    assert all(a["status"] == "SUCCEEDED" for a in run["actions"])
    _assert_single_side_effects("SYN-PA-0001")


class Crash(BaseException):
    """Simulates the process dying: not caught by the engine's `except Exception`."""


def _assert_single_side_effects(request_number):
    from app import models as m
    from app.db import session_scope
    with session_scope() as s:
        assert s.query(m.Notice).filter_by(request_number=request_number).count() == 1
        assert s.get(m.AuthRequest, request_number).status != "PENDING"


def test_crash_after_side_effect_replays_by_idempotency_key(client, monkeypatch):
    """Process dies after the server committed record_determination but before the step checkpoint."""
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    real_save = store.save_step
    crashed = {"done": False}

    def crash_once(run_id, expected_version, step, *a, **kw):
        if step == "execute_actions" and not crashed["done"]:
            crashed["done"] = True
            raise Crash("simulated crash after side effects")
        return real_save(run_id, expected_version, step, *a, **kw)

    monkeypatch.setattr(store, "save_step", crash_once)
    with pytest.raises(Crash):      # driven directly: a BaseException would tear down the HTTP test client
        engine.decide(run["run_id"], "APPROVE", "SYN-R01", 1, None)
    monkeypatch.setattr(store, "save_step", real_save)
    assert store.get_run(run["run_id"])["status"] == "RUNNING"          # stuck mid-step; lease released by crash
    recovered = engine.recover_interrupted_runs()                        # what startup does
    assert run["run_id"] in recovered
    run = client.post(f"/api/v1/workflows/{run['run_id']}/resume").json()
    assert run["status"] == "COMPLETED", run["error"]
    # replayed, not re-executed: no 409 "already APPROVED", exactly one notice
    assert all(a["status"] == "SUCCEEDED" for a in run["actions"])
    _assert_single_side_effects("SYN-PA-0001")


def test_running_run_with_live_lease_cannot_be_resumed_but_expired_lease_can(client, monkeypatch):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    assert client.post(f"/api/v1/workflows/{run['run_id']}/resume").status_code == 409   # AWAITING_APPROVAL
    real_save = store.save_step

    def crash(run_id, expected_version, step, *a, **kw):
        raise Crash("crash")

    monkeypatch.setattr(store, "save_step", crash)
    with pytest.raises(Crash):
        engine.decide(run["run_id"], "APPROVE", "SYN-R01", 1, None)
    monkeypatch.setattr(store, "save_step", real_save)
    rid = run["run_id"]
    # Another live worker holds the lease: resume and recovery must both leave the run alone.
    assert store.acquire_lease(rid, "other-worker", ttl=60)
    r = client.post(f"/api/v1/workflows/{rid}/resume")
    assert r.status_code == 409 and "lease held" in r.text
    assert rid not in engine.recover_interrupted_runs()
    # ...and that worker cannot be displaced while its lease is live
    assert engine._advance(rid)["status"] == "RUNNING"
    # Its lease expires (worker died): the run becomes resumable.
    assert store.acquire_lease(rid, "other-worker", ttl=-1)
    run = client.post(f"/api/v1/workflows/{rid}/resume").json()
    assert run["status"] == "COMPLETED"
    _assert_single_side_effects("SYN-PA-0001")


def test_worker_that_lost_its_lease_cannot_write_a_checkpoint(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    r = store.get_run(run["run_id"])
    assert store.acquire_lease(r["run_id"], "worker-b", ttl=60)
    with pytest.raises(store.StaleRun):
        store.save_step(r["run_id"], r["version"], "x", "x", "RUNNING", r["state"], lease_owner="worker-a")


def test_heartbeat_renews_lease_during_long_steps(client, monkeypatch):
    import time
    monkeypatch.setenv("RUN_LEASE_SECONDS", "1")
    get_settings.cache_clear()
    original = engine.node_validate
    seen = {}

    def slow(run_, ctx):
        time.sleep(1.6)                                      # longer than the lease itself
        seen["still_ours"] = not store.acquire_lease(run_["run_id"], "intruder", ttl=1)
        return original(run_, ctx)

    monkeypatch.setattr(engine, "node_validate", slow)
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    assert seen["still_ours"] and run["status"] == "AWAITING_APPROVAL"


def test_recovery_settles_orphaned_token_reservations(client, monkeypatch):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    res = store.reserve_tokens(run_id=run["run_id"], agent="t", provider="mock", model="m", estimated_input=1000,
                               max_input=10**6, max_output=2000, run_budget=10**6, daily_budget=10**9)
    monkeypatch.setenv("TOKEN_RESERVATION_EXPIRY_SECONDS", "-1")
    get_settings.cache_clear()
    engine.recover_interrupted_runs()
    entry = next(e for e in store.ledger(run["run_id"]) if e["estimated_input_tokens"] == 1000)
    assert entry["outcome"] == "ABANDONED" and entry["input_tokens"] + entry["output_tokens"] == 3000
    assert res["allowed"]


def test_unknown_procedure_is_invalid_request(client):
    run = start(client, "SYN-M0009", {"request_type": "COVERAGE_INQUIRY", "cpt_code": "99999", "icd10_code": "M62.81",
                                       "service_date": "2026-11-20"})
    assert run["status"] == "INVALID_REQUEST" and "99999" in run["error"]
