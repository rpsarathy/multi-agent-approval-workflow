"""End-to-end over real sockets: public app and internal API as two separate uvicorn processes."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise TimeoutError(url)


@pytest.fixture()
def two_processes(tmp_path):
    pub, internal = _free_port(), _free_port()
    env = {**os.environ, "LLM_MODE": "mock", "DATABASE_URL": f"sqlite:///{tmp_path / 'e2e.db'}",
           "INTERNAL_API_SECRET": "two-process-secret-0123456789abcdefghijklmnopqrstu",
           "REVIEWER_TOKEN_SECRET": "two-process-reviewer-0123456789abcdefghijklmnopq",
           "INTERNAL_API_BASE_URL": f"http://127.0.0.1:{internal}"}
    logs = {name: open(tmp_path / f"{name}.log", "w") for name in ("internal", "public")}
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "app.internal_main:internal_app", "--port", str(internal)],
                              cwd=ROOT, env=env, stdout=logs["internal"], stderr=subprocess.STDOUT)]
    _wait(f"http://127.0.0.1:{internal}/internal/v1/members/x")       # 422 (no credential) = up
    procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(pub)],
                                  cwd=ROOT, env=env, stdout=logs["public"], stderr=subprocess.STDOUT))
    _wait(f"http://127.0.0.1:{pub}/healthz")
    try:
        yield f"http://127.0.0.1:{pub}", f"http://127.0.0.1:{internal}", tmp_path
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)
        for f in logs.values():
            f.close()


def test_full_workflow_across_processes(two_processes):
    public, internal, tmp = two_processes
    c = httpx.Client(base_url=public, timeout=30)
    tok = c.post("/api/v1/auth/login", json={"reviewer_id": "SYN-R02", "passcode": "director-demo"}).json()["access_token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    run = c.post("/api/v1/workflows", json={"member_id": "SYN-M0004",
                                            "request": {"request_type": "PRIOR_AUTH_REVIEW",
                                                        "auth_request_number": "SYN-PA-0004"}}).json()
    assert run["status"] == "AWAITING_APPROVAL" and run["recommendations"][-1]["payload"]["outcome"] == "DENY"
    done = c.post(f"/api/v1/workflows/{run['run_id']}/decisions",
                  json={"decision": "APPROVE", "recommendation_version": 1}).json()
    assert done["status"] == "COMPLETED"
    assert [a["status"] for a in done["actions"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert c.get(f"/api/v1/workflows/{run['run_id']}/audit").json()["integrity"]["valid"]
    # agents' calls really crossed the process boundary; the public app does not serve them
    served = [ln for ln in (tmp / "internal.log").read_text().splitlines() if "/internal/v1/" in ln and "200 OK" in ln]
    assert len(served) == 7     # 5 retrieval calls + determination + notice
    assert c.get("/internal/v1/members/SYN-M0004").status_code == 404
    # without the shared secret the internal service rejects callers
    r = httpx.get(f"{internal}/internal/v1/members/SYN-M0004",
                  headers={"X-Agent-Id": "member_retrieval_agent", "X-Agent-Token": "guess"})
    assert r.status_code == 401
