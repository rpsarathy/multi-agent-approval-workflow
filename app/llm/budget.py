"""Token budget guard (infrastructure; injected into the recommendation agent by the orchestrator).

Before every LLM call `reserve()` atomically (see workflow_store.reserve_tokens):
  1. rejects input above TOKEN_BUDGET_MAX_INPUT_PER_CALL;
  2. sizes max_tokens = min(per-call output cap, run budget - run used - this input) and
     rejects the call if fewer than 1024 output tokens would remain;
  3. rejects the call if the rolling 24h total would exceed TOKEN_BUDGET_DAILY;
  4. otherwise records a RESERVED ledger row for the worst case (input + max_tokens),
     which counts against both budgets until `settle()` replaces it with actual usage.
Because the worst case is reserved before the call, an allowed call cannot overshoot.
"""
from app.config import get_settings
from app.services import workflow_store as store


class TokenBudget:
    def __init__(self, run_id: str, run_budget: int, agent: str):
        cfg = get_settings()
        self.run_id, self.run_budget, self.agent = run_id, run_budget, agent
        self.max_input = cfg.token_budget_max_input_per_call
        self.max_output = cfg.token_budget_max_output_per_call
        self.daily_budget = cfg.token_budget_daily

    def reserve(self, estimated_input: int, provider: str, model: str) -> dict:
        return store.reserve_tokens(run_id=self.run_id, agent=self.agent, provider=provider, model=model,
                                    estimated_input=estimated_input, max_input=self.max_input,
                                    max_output=self.max_output, run_budget=self.run_budget,
                                    daily_budget=self.daily_budget)

    def settle(self, reservation: dict, *, ok: bool, input_tokens: int, output_tokens: int, cache_read: int = 0,
               cache_creation: int = 0, detail: str | None = None) -> None:
        store.settle_tokens(reservation["entry_id"], outcome="OK" if ok else "ERROR", input_tokens=input_tokens,
                            output_tokens=output_tokens, cache_read=cache_read, cache_creation=cache_creation,
                            detail=detail)
