import os

# Force deterministic, offline mode regardless of the developer's .env.
os.environ["LLM_MODE"] = "mock"
os.environ["INTERNAL_API_BASE_URL"] = ""
os.environ["INTERNAL_API_SECRET"] = "test-internal-secret-0123456789abcdefghijklmnop"
os.environ["REVIEWER_TOKEN_SECRET"] = "test-reviewer-secret-0123456789abcdefghijklmnop"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import init_engine  # noqa: E402
from app.orchestrator import engine  # noqa: E402
from app.seed import DEMO_PASSCODES, seed  # noqa: E402

_tokens: dict[str, str] = {}


@pytest.fixture()
def internal(tmp_path):
    """The agents' internal API (separate ASGI app)."""
    from app.internal_main import internal_app
    return TestClient(internal_app, base_url="http://internal")


@pytest.fixture()
def client(tmp_path, internal):
    """Public app, logged in as the nurse reviewer by default."""
    get_settings.cache_clear()
    init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    seed()
    _tokens.clear()
    from app.main import app
    with TestClient(app) as c:
        engine.set_context(engine.Context(http=internal))
        c.headers.update(auth(c, "SYN-R01"))
        yield c
    get_settings.cache_clear()


def auth(client, reviewer_id):
    if reviewer_id not in _tokens:
        r = client.post("/api/v1/auth/login", json={"reviewer_id": reviewer_id, "passcode": DEMO_PASSCODES[reviewer_id]},
                        headers={"Authorization": ""})
        assert r.status_code == 200, r.text
        _tokens[reviewer_id] = r.json()["access_token"]
    return {"Authorization": f"Bearer {_tokens[reviewer_id]}"}


def start(client, member_id, request, **extra):
    r = client.post("/api/v1/workflows", json={"member_id": member_id, "request": request, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def pa(n):
    return {"request_type": "PRIOR_AUTH_REVIEW", "auth_request_number": n}


def decide(client, run, decision, reviewer, comment=None, version=None):
    v = version or run["recommendations"][-1]["version"]
    return client.post(f"/api/v1/workflows/{run['run_id']}/decisions", headers=auth(client, reviewer),
                       json={"decision": decision, "recommendation_version": v, "comment": comment})
