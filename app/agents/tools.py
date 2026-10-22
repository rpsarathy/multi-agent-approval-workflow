"""Schema-validated tool layer: agents -> tool call -> internal HTTP API.

Each tool has a Pydantic input model (validated before the request leaves) and
a Pydantic output model (validated on return). Agents get a ToolClient bound to
their own identity, so the internal API can enforce per-agent grants.
"""
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import schemas as s


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetMemberProfileIn(_In):
    member_id: str = Field(pattern=r"^[A-Za-z0-9\-]{1,16}$")


class GetMemberAgeIn(_In):
    member_id: str = Field(pattern=r"^[A-Za-z0-9\-]{1,16}$")
    on: date


class GetAuthRequestIn(_In):
    member_id: str = Field(pattern=r"^[A-Za-z0-9\-]{1,16}$")
    request_number: str = Field(pattern=r"^[A-Za-z0-9\-]{1,24}$")


class GetProviderIn(_In):
    provider_id: int = Field(ge=1)
    state_code: str = Field(pattern=r"^[A-Z]{2}$")
    plan_id: int = Field(ge=1)


class GetPolicyRulesIn(_In):
    state_code: str = Field(pattern=r"^[A-Z]{2}$")
    insurance_category: str = Field(pattern=r"^(COMMERCIAL|MEDICARE|MEDICAID)$")
    cpt_code: str = Field(pattern=r"^[A-Z0-9]{4,8}$")
    on: date


class RecordDeterminationIn(_In):
    request_number: str = Field(pattern=r"^[A-Za-z0-9\-]{1,24}$")
    body: s.DeterminationIn


class CreateNoticeIn(_In):
    body: s.NoticeIn


@dataclass(frozen=True)
class ToolSpec:
    name: str
    method: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    build: Callable[[Any], tuple[str, dict | None, dict | None]]   # args -> (path, query, json)


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("get_member_profile", "GET", GetMemberProfileIn, s.MemberProfile,
             lambda a: (f"/members/{a.member_id}", None, None)),
    ToolSpec("get_member_age", "GET", GetMemberAgeIn, s.MemberAgeOut,
             lambda a: (f"/members/{a.member_id}/age", {"on": a.on.isoformat()}, None)),
    ToolSpec("get_auth_request", "GET", GetAuthRequestIn, s.AuthRequestOut,
             lambda a: (f"/members/{a.member_id}/auth-requests/{a.request_number}", None, None)),
    ToolSpec("get_provider", "GET", GetProviderIn, s.ProviderOut,
             lambda a: (f"/providers/{a.provider_id}", {"state_code": a.state_code, "plan_id": a.plan_id}, None)),
    ToolSpec("get_policy_rules", "GET", GetPolicyRulesIn, s.PolicyRulesOut,
             lambda a: ("/policy-rules", {"state_code": a.state_code, "insurance_category": a.insurance_category,
                                          "cpt_code": a.cpt_code, "on": a.on.isoformat()}, None)),
    ToolSpec("record_determination", "POST", RecordDeterminationIn, s.DeterminationOut,
             lambda a: (f"/auth-requests/{a.request_number}/determination", None, a.body.model_dump(mode="json"))),
    ToolSpec("create_notice", "POST", CreateNoticeIn, s.NoticeOut,
             lambda a: ("/notices", None, a.body.model_dump(mode="json"))),
]}


class ToolError(Exception):
    def __init__(self, tool: str, status: int | None, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool, self.status, self.message = tool, status, message


@dataclass
class ToolCallRecord:
    agent: str
    tool: str
    args: dict
    status: int | None
    ok: bool
    latency_ms: int
    error: str | None = None


class ToolClient:
    """Calls the internal API as one agent identity, with the credential the orchestrator minted for it.
    `on_call` receives a ToolCallRecord for auditing."""

    def __init__(self, http: httpx.Client, agent_id: str, credential: str,
                 on_call: Callable[[ToolCallRecord], None] | None = None):
        self.http, self.agent_id, self.on_call = http, agent_id, on_call
        self._credential = credential

    def call(self, tool_name: str, idempotency_key: str | None = None, **kwargs) -> BaseModel:
        spec = TOOLS.get(tool_name)
        if spec is None:
            raise ToolError(tool_name, None, "unknown tool")
        try:
            args = spec.input_model(**kwargs)
        except ValidationError as e:
            self._record(tool_name, kwargs, None, False, 0, f"input validation: {e.errors()[0]['msg']}")
            raise ToolError(tool_name, None, f"invalid input: {e.errors()}") from e
        path, query, body = spec.build(args)
        headers = {"X-Agent-Id": self.agent_id, "X-Agent-Token": self._credential}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        t0 = time.monotonic()
        resp = self.http.request(spec.method, "/internal/v1" + path, params=query, json=body, headers=headers)
        ms = int((time.monotonic() - t0) * 1000)
        logged_args = args.model_dump(mode="json")
        if resp.status_code >= 400:
            detail = _detail(resp)
            self._record(tool_name, logged_args, resp.status_code, False, ms, detail)
            raise ToolError(tool_name, resp.status_code, detail)
        try:
            out = spec.output_model.model_validate(resp.json())
        except ValidationError as e:
            self._record(tool_name, logged_args, resp.status_code, False, ms, "output validation failed")
            raise ToolError(tool_name, resp.status_code, f"invalid output: {e.errors()}") from e
        self._record(tool_name, logged_args, resp.status_code, True, ms)
        return out

    def _record(self, tool, args, status, ok, ms, error=None):
        if self.on_call:
            self.on_call(ToolCallRecord(self.agent_id, tool, args, status, ok, ms, error))


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail"))
    except Exception:
        return resp.text[:300]
