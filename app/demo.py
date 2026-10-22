"""Run every synthetic scenario end to end from the command line.

    python -m app.demo                 # agents only; stop at AWAITING_APPROVAL
    python -m app.demo --approve       # also approve as the medical director and execute actions

Uses its own database file (data/demo.db) so it never touches the server's data.
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/demo.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.internal_main import internal_app  # noqa: E402
from app.main import app  # noqa: E402
from app.orchestrator import engine  # noqa: E402
from app.seed import DEMO_PASSCODES, seed  # noqa: E402


def main(approve: bool) -> None:
    seed(reset=True)
    internal = TestClient(internal_app, base_url="http://internal")
    with TestClient(app) as c:
        engine.set_context(engine.Context(http=internal))
        login = c.post("/api/v1/auth/login", json={"reviewer_id": "SYN-R02", "passcode": DEMO_PASSCODES["SYN-R02"]})
        c.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        print(f"LLM mode: {c.get('/healthz').json()}\n")
        for sc in c.get("/api/v1/demo/scenarios").json():
            run = c.post("/api/v1/workflows", json={"member_id": sc["member_id"], "request": sc["request"]}).json()
            rec = run["recommendations"][-1] if run["recommendations"] else None
            line = f"{sc['member_id']}  {run['status']:<18} {rec['payload']['outcome'] if rec else '-':<26}"
            line += f" tokens={run['token_usage']['used']:<6} {sc['description']}"
            print(line)
            if rec and rec["guardrail_flags"]:
                print("      flags:", rec["guardrail_flags"])
            if approve and run["status"] == "AWAITING_APPROVAL":
                done = c.post(f"/api/v1/workflows/{run['run_id']}/decisions",
                              json={"decision": "APPROVE", "recommendation_version": rec["version"]}).json()
                print("      approved ->", done["status"], [(a["action_type"], a["status"]) for a in done["actions"]])


if __name__ == "__main__":
    main("--approve" in sys.argv)
