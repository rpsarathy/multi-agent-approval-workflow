import ast
import pathlib

from app import models as m
from app.db import session_scope
from app.services import workflow_store as store
from tests.conftest import decide, pa, start  # noqa: F401

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "agents"


def test_agents_do_not_import_database_modules():
    forbidden = {"sqlalchemy", "sqlite3", "app.db", "app.models", "app.repositories", "app.services.domain",
                 "app.services.workflow_store", "app.services.action_guard", "app.llm.budget", "app.orchestrator",
                 "app.security", "app.config"}
    for path in AGENT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert not any(n == f or n.startswith(f + ".") for f in forbidden), f"{path.name} imports {n}"


def _h(agent, token=None):
    from app.security import agent_token
    return {"X-Agent-Id": agent, "X-Agent-Token": token or agent_token(agent)}


def test_internal_api_is_not_mounted_on_public_app(client):
    assert client.get("/internal/v1/members/SYN-M0001", headers=_h("member_retrieval_agent")).status_code == 404


def test_internal_api_requires_agent_credential(client, internal):
    assert internal.get("/internal/v1/members/SYN-M0001", headers=_h("member_retrieval_agent", "forged")).status_code == 401
    assert internal.get("/internal/v1/members/SYN-M0001").status_code == 422      # headers missing
    # one agent's credential cannot be used to claim another agent's identity
    stolen = _h("policy_retrieval_agent")["X-Agent-Token"]
    assert internal.get("/internal/v1/members/SYN-M0001",
                        headers=_h("member_retrieval_agent", stolen)).status_code == 401


def test_internal_api_enforces_per_agent_grants(client, internal):
    assert internal.get("/internal/v1/members/SYN-M0001", headers=_h("member_retrieval_agent")).status_code == 200
    assert internal.get("/internal/v1/members/SYN-M0001", headers=_h("policy_retrieval_agent")).status_code == 403
    assert internal.get("/internal/v1/members/SYN-M0001", headers=_h("recommendation_agent")).status_code == 403
    body = {"status": "APPROVED", "units_approved": 1, "note": "x", "workflow_run_id": "r", "approval_id": 1}
    assert internal.post("/internal/v1/auth-requests/SYN-PA-0001/determination", json=body,
                         headers={**_h("policy_retrieval_agent"), "Idempotency-Key": "k-12345678"}).status_code == 403


def test_write_without_human_approval_is_refused_by_service_layer(client, internal):
    """Even the action executor's own credential cannot write without an approved run behind it."""
    h = {**_h("action_executor"), "Idempotency-Key": "attack-0001"}
    url = "/internal/v1/auth-requests/SYN-PA-0002/determination"
    r = internal.post(url, json={"status": "APPROVED", "units_approved": 1, "note": "x", "workflow_run_id": "fake", "approval_id": 1}, headers=h)
    assert r.status_code == 403
    # a real run that is still awaiting approval is not enough either
    run = start(client, "SYN-M0002", pa("SYN-PA-0002"))
    r = internal.post(url, json={"status": "APPROVED", "units_approved": 1, "note": "x", "workflow_run_id": run["run_id"], "approval_id": 1},
                      headers={**h, "Idempotency-Key": "attack-0002"})
    assert r.status_code == 403
    notice = {"workflow_run_id": run["run_id"], "approval_id": 1, "member_id": "SYN-M0002",
              "request_number": "SYN-PA-0002", "notice_type": "PROVIDER_DETERMINATION_NOTICE", "body": "x"}
    assert internal.post("/internal/v1/notices", json=notice, headers={**h, "Idempotency-Key": "attack-0003"}).status_code == 403
    with session_scope() as s:
        assert s.get(m.AuthRequest, "SYN-PA-0002").status == "PENDING"
        assert s.query(m.Notice).count() == 0


def test_approved_run_cannot_be_used_to_write_a_contradicting_determination(client, internal, monkeypatch):
    """An APPROVE of a DENY recommendation must not be usable to record APPROVED, or to touch another request."""
    run = start(client, "SYN-M0004", pa("SYN-PA-0004"))
    from app.orchestrator import engine
    seen = {}

    def spy(run_, ctx):          # stop right before actions execute, holding the approved state
        seen["approval_id"] = run_["state"]["approval_id"]
        raise RuntimeError("pause")

    monkeypatch.setattr(engine, "node_execute_actions", spy)
    decide(client, run, "APPROVE", "SYN-R02")
    from app.services import workflow_store as st
    r = st.get_run(run["run_id"])
    st.save_step(run["run_id"], r["version"], "resume", "execute_actions", "RUNNING", r["state"], [])
    h = {**_h("action_executor"), "Idempotency-Key": "contradict-1"}
    body = {"status": "APPROVED", "units_approved": 1, "note": "x", "workflow_run_id": run["run_id"], "approval_id": seen["approval_id"]}
    r = internal.post("/internal/v1/auth-requests/SYN-PA-0004/determination", json=body, headers=h)
    assert r.status_code == 403 and "contradicts" in r.text
    r = internal.post("/internal/v1/auth-requests/SYN-PA-0001/determination", json={**body, "status": "DENIED"},
                      headers={**h, "Idempotency-Key": "contradict-2"})
    assert r.status_code == 403 and "does not match" in r.text
    # right status, wrong reason code (the approved determination is LIC-01, 0 units)
    r = internal.post("/internal/v1/auth-requests/SYN-PA-0004/determination",
                      json={**body, "status": "DENIED", "denial_code": "REF-01", "units_approved": 0},
                      headers={**h, "Idempotency-Key": "contradict-3"})
    assert r.status_code == 403 and "differ from the approved determination" in r.text


def test_tool_inputs_are_schema_validated(client, internal):
    from app.agents.tools import ToolClient, ToolError
    from app.security import agent_token
    tc = ToolClient(internal, "member_retrieval_agent", agent_token("member_retrieval_agent"))
    try:
        tc.call("get_member_profile", member_id="x'; DROP TABLE members;--")
        raise AssertionError("expected ToolError")
    except ToolError as e:
        assert "invalid input" in e.message


def test_member_profile_does_not_expose_contact_details(client, internal):
    body = internal.get("/internal/v1/members/SYN-M0001", headers=_h("member_retrieval_agent")).json()
    assert "email" not in body and body["has_email_on_file"] is True and body["is_synthetic"] is True


def test_tool_calls_are_audited(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    events = client.get(f"/api/v1/workflows/{run['run_id']}/audit").json()["events"]
    tools = [e["payload"]["tool"] for e in events if e["event_type"] == "TOOL_CALL"]
    assert tools == ["get_member_profile", "get_auth_request", "get_member_age", "get_provider", "get_policy_rules"]
    types = [e["event_type"] for e in events]
    for t in ("WORKFLOW_STARTED", "INTAKE_COMPLETED", "VALIDATION_COMPLETED", "TOKEN_BUDGET_CHECKED",
              "RECOMMENDATION_CREATED", "APPROVAL_REQUESTED"):
        assert t in types


def test_audit_chain_verifies_and_detects_tampering(client):
    run = start(client, "SYN-M0001", pa("SYN-PA-0001"))
    decide(client, run, "APPROVE", "SYN-R01")
    audit = client.get(f"/api/v1/workflows/{run['run_id']}/audit").json()
    assert audit["integrity"]["valid"] is True and audit["integrity"]["events"] > 10
    with session_scope() as s:
        ev = s.query(m.AuditEvent).filter_by(run_id=run["run_id"], event_type="HUMAN_APPROVE").one()
        ev.payload = {**ev.payload, "role": "MEDICAL_DIRECTOR"}
        bad_seq = ev.seq
    result = store.verify_audit_chain(run["run_id"])
    assert result == {"valid": False, "events": bad_seq, "first_invalid_seq": bad_seq}


def test_synthetic_labels(client):
    with session_scope() as s:
        for model in (m.Member, m.Plan, m.AuthRequest, m.CoverageRule, m.Provider):
            assert all(r.is_synthetic for r in s.query(model).all())
        assert all(r.member_id.startswith("SYN-") for r in s.query(m.Member).all())
