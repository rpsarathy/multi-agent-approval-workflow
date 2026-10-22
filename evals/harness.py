"""Shared harness for evals and PII tests: an isolated app instance, PII inventory, sink capture."""
import os
import tempfile
import typing
from contextlib import contextmanager

from pydantic import BaseModel
from sqlalchemy import inspect as sa_inspect

from app import privacy
from app.llm.base import LLMRecommendation, LLMResult, Usage
from app.llm.mock import MockProvider


def configure_env(live: bool = False) -> None:
    """Must run before the app is used. Mock mode unless the caller explicitly opted into live."""
    os.environ["LLM_MODE"] = "live" if live else "mock"
    os.environ["INTERNAL_API_BASE_URL"] = ""
    os.environ.setdefault("INTERNAL_API_SECRET", "eval-internal-secret-0123456789abcdefghijklmnop")
    os.environ.setdefault("REVIEWER_TOKEN_SECRET", "eval-reviewer-secret-0123456789abcdefghijklmnop")


class SpyProvider:
    """Wraps any provider and records every prompt that would be sent to the LLM.
    With `scripted`, returns that recommendation instead of calling the wrapped provider."""

    def __init__(self, inner=None, scripted: LLMRecommendation | None = None):
        self.inner = inner or MockProvider()
        self.name, self.model = self.inner.name, self.inner.model
        self.prompts: list[str] = []
        self.scripted = scripted

    def count_input_tokens(self, system, user):
        self.prompts.append(system + "\n" + user)
        return self.inner.count_input_tokens(system, user)

    def recommend(self, system, user, max_output_tokens):
        self.prompts.append(system + "\n" + user)
        if self.scripted:
            return LLMResult(self.scripted, Usage(400, 200), "scripted", "end_turn")
        return self.inner.recommend(system, user, max_output_tokens)


@contextmanager
def isolated_app(provider=None):
    """Fresh temp database + seeded data + public client (signed in as the medical director)."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.db import init_engine
    from app.internal_main import internal_app
    from app.llm.anthropic_provider import build_provider
    from app.main import app
    from app.orchestrator import engine
    from app.seed import DEMO_PASSCODES, seed

    with tempfile.TemporaryDirectory() as tmp:
        get_settings.cache_clear()
        init_engine(f"sqlite:///{tmp}/eval.db")
        seed()
        spy = SpyProvider(inner=provider or build_provider(get_settings()))
        internal = TestClient(internal_app, base_url="http://internal")
        with TestClient(app) as c:
            engine.set_context(engine.Context(http=internal, provider=spy))
            tok = c.post("/api/v1/auth/login", json={"reviewer_id": "SYN-R02", "passcode": DEMO_PASSCODES["SYN-R02"]})
            c.headers["Authorization"] = f"Bearer {tok.json()['access_token']}"
            yield c, internal, spy
        get_settings.cache_clear()


def pii_values(member_id: str | None = None) -> list[str]:
    """Every direct identifier on record (for one member, or all), in the formats it may appear in."""
    from app import models as m
    from app.db import session_scope
    out = []
    with session_scope() as s:
        q = s.query(m.Member)
        for x in ([s.get(m.Member, member_id)] if member_id else q.all()):
            dob = x.date_of_birth
            out += [f"{x.first_name} {x.last_name}", x.first_name, x.last_name, dob.isoformat(), f"{dob:%m/%d/%Y}",
                    x.email, x.phone, x.phone.replace("(", "").replace(") ", "-"), x.ssn, x.ssn.replace("-", ""),
                    x.address_line1]
    return [v for v in out if v]


def sink_rows() -> dict[str, list]:
    """Everything the workflow persisted, plus determination notes written on auth requests."""
    from app import models as m
    from app.db import session_scope
    sinks = [m.WorkflowRun, m.WorkflowCheckpoint, m.Recommendation, m.Approval, m.ActionExecution,
             m.TokenLedgerEntry, m.AuditEvent, m.Notice, m.IdempotencyRecord]
    out = {}
    with session_scope() as s:
        for model in sinks:
            out[model.__tablename__] = [{c.key: getattr(r, c.key) for c in sa_inspect(model).mapper.column_attrs}
                                        for r in s.query(model).all()]
        out["auth_requests.decision_note"] = [r.decision_note for r in s.query(m.AuthRequest).all() if r.decision_note]
    return out


def jsonable(obj):
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj if isinstance(obj, (str, int, float, bool, type(None))) else str(obj)


def leaks(obj, extra_values: list[str] = ()) -> list[tuple[str, str]]:
    return privacy.find_pii(jsonable(obj), pii_values() + list(extra_values))


def walk_model_fields(model: type[BaseModel], seen=None):
    seen = seen if seen is not None else set()
    if model in seen:
        return
    seen.add(model)
    for name, f in model.model_fields.items():
        yield model.__name__, name
        args = [f.annotation, *typing.get_args(f.annotation)]
        args += [a for x in typing.get_args(f.annotation) for a in typing.get_args(x)]
        for arg in args:
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                yield from walk_model_fields(arg, seen)
