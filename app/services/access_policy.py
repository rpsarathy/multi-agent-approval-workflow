"""Authorization policies of THIS APPLICATION (not insurance policy rules).

1. AGENT_TOOL_GRANTS     - which agent identity may call which internal-API operation.
2. approval_permitted()  - which reviewer role may sign off which recommendation outcome.
3. PERMITTED_ACTIONS     - which follow-up actions an approved outcome may trigger.
"""

AGENT_TOOL_GRANTS: dict[str, frozenset[str]] = {
    "member_retrieval_agent": frozenset({"get_member_profile"}),
    "policy_retrieval_agent": frozenset({"get_auth_request", "get_member_age", "get_provider", "get_policy_rules"}),
    # Validation and recommendation agents work only on state handed to them: no tools.
    "action_executor": frozenset({"record_determination", "create_notice"}),
}


def agent_may_call(agent_id: str, operation: str) -> bool:
    return operation in AGENT_TOOL_GRANTS.get(agent_id, frozenset())


# Adverse (DENY) and escalated outcomes need a physician reviewer, mirroring
# utilization-management practice. Rejecting or sending back for revision is
# open to any active reviewer.
_SIGNOFF_ROLES = {
    "APPROVE": {"NURSE_REVIEWER", "MEDICAL_DIRECTOR"},
    "PEND_FOR_INFORMATION": {"NURSE_REVIEWER", "MEDICAL_DIRECTOR"},
    "DENY": {"MEDICAL_DIRECTOR"},
    "REFER_TO_MEDICAL_DIRECTOR": {"MEDICAL_DIRECTOR"},
}
REVIEWER_ROLES = {"NURSE_REVIEWER", "MEDICAL_DIRECTOR"}


def approval_permitted(role: str, decision: str, outcome: str) -> bool:
    if decision in ("REJECT", "REQUEST_REVISION"):
        return role in REVIEWER_ROLES
    return role in _SIGNOFF_ROLES.get(outcome, set())


# Follow-up actions allowed per approved outcome. For a COVERAGE_INQUIRY there is
# no submitted request, so determinations are never recorded.
PERMITTED_ACTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "PRIOR_AUTH_REVIEW": {
        "APPROVE": ("RECORD_DETERMINATION", "NOTIFY_PROVIDER"),
        "DENY": ("RECORD_DETERMINATION", "NOTIFY_PROVIDER"),
        "PEND_FOR_INFORMATION": ("REQUEST_DOCUMENTS",),
        "REFER_TO_MEDICAL_DIRECTOR": ("ESCALATE_TO_MEDICAL_DIRECTOR",),
    },
    "COVERAGE_INQUIRY": {
        "APPROVE": ("NOTIFY_PROVIDER",),
        "DENY": ("NOTIFY_PROVIDER",),
        "PEND_FOR_INFORMATION": ("REQUEST_DOCUMENTS",),
        "REFER_TO_MEDICAL_DIRECTOR": ("ESCALATE_TO_MEDICAL_DIRECTOR",),
    },
}


def permitted_actions(request_type: str, outcome: str) -> tuple[str, ...]:
    return PERMITTED_ACTIONS.get(request_type, {}).get(outcome, ())
