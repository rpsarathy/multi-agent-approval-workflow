"""Opt-in live smoke test against the Anthropic API. Skipped unless RUN_LIVE_LLM_TESTS=1.

It makes one count_tokens call and one small generation (costs a few cents), and checks the
structured output parses into LLMRecommendation and that usage is reported.

    RUN_LIVE_LLM_TESTS=1 ANTHROPIC_API_KEY=... [ANTHROPIC_WORKSPACE_ID=...] pytest tests/test_live_smoke.py -q
"""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM_TESTS") != "1",
                                reason="live LLM test; set RUN_LIVE_LLM_TESTS=1 to run (billed)")


def test_live_structured_recommendation():
    from app.agents.recommendation import SYSTEM_PROMPT
    from app.llm.anthropic_provider import AnthropicProvider
    from app.llm.base import LLMRecommendation

    p = AnthropicProvider(os.environ.get("LLM_MODEL", "claude-opus-5-5"), "low", os.environ.get("ANTHROPIC_API_KEY"),
                          True, 120, os.environ.get("ANTHROPIC_WORKSPACE_ID", ""))
    bundle = {"validation": {"checks": [{"check": "ELIGIBILITY", "status": "PASS", "detail": "active", "hard": True,
                                         "evidence_ids": ["enrollment:1"]}], "data_issues": [], "summary": {}},
              "evidence": {"enrollment:1": "Enrollment active", "check:ELIGIBILITY": "ELIGIBILITY PASS"},
              "permitted_actions": {"APPROVE": ["NOTIFY_PROVIDER"]}, "reviewer_feedback": []}
    user = "Prepare the recommendation.\n<evidence_bundle>\n" + json.dumps(bundle) + "\n</evidence_bundle>"
    assert p.count_input_tokens(SYSTEM_PROMPT, user) > 100
    result = p.recommend(SYSTEM_PROMPT, user, 4000)
    assert result.error is None, result.error
    assert isinstance(result.recommendation, LLMRecommendation)
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0
