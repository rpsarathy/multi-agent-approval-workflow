"""Provider-neutral LLM contract for the recommendation agent."""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class Outcome(str, Enum):
    APPROVE = "APPROVE"
    DENY = "DENY"
    PEND_FOR_INFORMATION = "PEND_FOR_INFORMATION"
    REFER_TO_MEDICAL_DIRECTOR = "REFER_TO_MEDICAL_DIRECTOR"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(description="One factual statement supporting the recommendation.")
    evidence_ids: list[str] = Field(description="IDs from the evidence bundle that support the statement.")


class LLMRecommendation(BaseModel):
    """Structured output the model must return. Kept free of numeric/length constraints so it
    maps cleanly onto JSON-schema structured outputs; limits are enforced by the guardrails."""
    model_config = ConfigDict(extra="forbid")
    outcome: Outcome
    summary: str = Field(description="Two or three sentences for the human reviewer.")
    findings: list[Finding]
    missing_information: list[str]
    proposed_actions: list[str] = Field(description="Subset of permitted_actions from the bundle.")
    confidence: str = Field(description="LOW, MEDIUM or HIGH")


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class LLMResult:
    recommendation: LLMRecommendation | None
    usage: Usage
    model: str
    stop_reason: str
    error: str | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    def count_input_tokens(self, system: str, user: str) -> int: ...

    def recommend(self, system: str, user: str, max_output_tokens: int) -> LLMResult: ...
