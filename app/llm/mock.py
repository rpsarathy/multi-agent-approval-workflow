"""Deterministic mock provider: same contract as the live adapter, no network.

It derives the recommendation from the validation results in the evidence
bundle with fixed rules, so the same input always yields the same output.
"""
import json

from app.llm.base import Finding, LLMRecommendation, LLMResult, Outcome, Usage


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Used only in mock mode, where nothing is billed."""
    return max(1, (len(text) + 3) // 4)


class MockProvider:
    name = "mock"
    model = "mock-deterministic-v1"

    def count_input_tokens(self, system: str, user: str) -> int:
        return estimate_tokens(system) + estimate_tokens(user)

    def recommend(self, system: str, user: str, max_output_tokens: int) -> LLMResult:
        bundle = json.loads(user.split("<evidence_bundle>", 1)[1].split("</evidence_bundle>", 1)[0])
        rec = rule_based_recommendation(bundle)
        out = rec.model_dump_json()
        return LLMResult(rec, Usage(self.count_input_tokens(system, user), min(estimate_tokens(out), max_output_tokens)),
                         self.model, "end_turn")


def rule_based_recommendation(bundle: dict) -> LLMRecommendation:
    """Deterministic decision table over the validation checks. Also the fallback when the LLM is unavailable."""
    checks = bundle["validation"]["checks"]
    issues = bundle["validation"]["data_issues"]
    permitted = bundle["permitted_actions"]
    hard_failed = [c for c in checks if c["status"] == "FAIL" and c["hard"]]
    soft_failed = [c for c in checks if c["status"] == "FAIL" and not c["hard"]]
    unknown = [c for c in checks if c["status"] == "UNKNOWN"]
    conflicts = [i for i in issues if i["kind"] == "CONFLICT"]
    missing = [i for i in issues if i["kind"] == "MISSING"]

    # Precedence: conflicts need a human physician; failures that more information
    # cannot cure deny; otherwise missing information pends before soft failures deny.
    if conflicts:
        outcome = Outcome.REFER_TO_MEDICAL_DIRECTOR
        summary = "Conflicting records prevent an automated determination; physician review is required."
        basis = conflicts
    elif hard_failed:
        outcome = Outcome.DENY
        summary = f"{len(hard_failed)} policy check(s) failed: " + ", ".join(c["check"] for c in hard_failed) + "."
        basis = hard_failed
    elif unknown or missing:
        outcome = Outcome.PEND_FOR_INFORMATION
        summary = "Required information is missing or incomplete; request it before deciding."
        basis = unknown + missing
    elif soft_failed:
        outcome = Outcome.DENY
        summary = f"{len(soft_failed)} policy check(s) failed: " + ", ".join(c["check"] for c in soft_failed) + "."
        basis = soft_failed
    else:
        outcome = Outcome.APPROVE
        summary = "All eligibility, provider, network, coverage, documentation and criteria checks passed."
        basis = checks

    findings = [Finding(statement=b.get("detail") or b.get("message", ""),
                        evidence_ids=list(b["evidence_ids"]) + ([f"check:{b['check']}"] if "check" in b else []))
                for b in basis]
    proposed = list(permitted.get(outcome.value, []))
    confidence = "HIGH" if outcome in (Outcome.APPROVE, Outcome.DENY) and not unknown else "MEDIUM"
    if bundle.get("reviewer_feedback"):
        summary += f" Revised after reviewer feedback: \"{bundle['reviewer_feedback'][-1]['comment']}\"."
    return LLMRecommendation(outcome=outcome, summary=summary, findings=findings,
                             missing_information=[i["message"] for i in missing] + [c["detail"] for c in unknown],
                             proposed_actions=proposed, confidence=confidence)
