"""Live adapter for the Anthropic Messages API (anthropic==2.1.0).

API surface used, verified against the installed SDK:
  client.messages.count_tokens(model=, system=, messages=, output_format=)
  client.messages.parse(... output_format=<pydantic model>)  -> .parsed_output
  client.beta.messages.parse(... betas=[...], fallbacks="default")  (server-side refusal fallback)
"""
import anthropic
from pydantic import ValidationError

from app.llm.base import LLMRecommendation, LLMResult, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, effort: str, api_key: str | None, server_fallbacks: bool, timeout: float,
                 workspace_id: str = ""):
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile.
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = anthropic.Anthropic(api_key=api_key or None, timeout=timeout, max_retries=2,
                                          default_headers=headers)
        self.model, self.effort, self.server_fallbacks = model, effort, server_fallbacks

    def count_input_tokens(self, system: str, user: str) -> int:
        resp = self.client.messages.count_tokens(model=self.model, system=system,
                                                 messages=[{"role": "user", "content": user}],
                                                 output_format=LLMRecommendation)
        return resp.input_tokens

    def recommend(self, system: str, user: str, max_output_tokens: int) -> LLMResult:
        kwargs = dict(model=self.model, max_tokens=max_output_tokens, system=system,
                      messages=[{"role": "user", "content": user}],
                      output_config={"effort": self.effort}, output_format=LLMRecommendation)
        try:
            if self.server_fallbacks:
                resp = self.client.beta.messages.parse(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.parse(**kwargs)
        except anthropic.RateLimitError as e:
            return self._error(f"rate limited: {e.message}")
        except anthropic.APIStatusError as e:
            return self._error(f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return self._error(f"connection error: {e}")
        except ValidationError as e:
            return self._error(f"structured output failed validation: {e.errors()[:1]}")

        u = resp.usage
        usage = Usage(u.input_tokens or 0, u.output_tokens or 0, u.cache_read_input_tokens or 0,
                      u.cache_creation_input_tokens or 0)
        if resp.stop_reason == "refusal":
            return LLMResult(None, usage, resp.model, "refusal", "model declined the request")
        if resp.stop_reason == "max_tokens":
            return LLMResult(None, usage, resp.model, "max_tokens", "output truncated at max_tokens")
        parsed = resp.parsed_output
        if parsed is None:
            return LLMResult(None, usage, resp.model, resp.stop_reason or "unknown", "no structured output returned")
        return LLMResult(LLMRecommendation.model_validate(parsed.model_dump()), usage, resp.model, resp.stop_reason)

    def _error(self, msg: str) -> LLMResult:
        return LLMResult(None, Usage(0, 0), self.model, "error", msg)


def build_provider(settings) -> "AnthropicProvider | object":
    from app.llm.mock import MockProvider
    if settings.effective_llm_mode == "live":
        return AnthropicProvider(settings.llm_model, settings.llm_effort, settings.anthropic_api_key,
                                 settings.llm_server_fallbacks, settings.llm_timeout_seconds,
                                 settings.anthropic_workspace_id)
    return MockProvider()
