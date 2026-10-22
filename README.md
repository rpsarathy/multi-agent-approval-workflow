# Prior-Authorization Multi-Agent Orchestrator

This is a runnable multi-agent workflow. It takes a **customer (member) ID** and a **business request**. Agents retrieve the member's details, their insurance policy and the policy rules that apply. All retrieval goes through schema-validated tools that call an internal API, never the database directly. The workflow then:

* validates the data and flags anything missing or conflicting;
* has an LLM produce an **evidence-cited recommendation**, within a **token budget**;
* waits for a **human reviewer** to approve, reject or request a revision;
* only after approval, carries out **explicitly permitted follow-up actions**.

Every step is checkpointed, so a run survives a crash, and every step is written to a **tamper-evident audit log**. Personal data (PII) never reaches the agents, the LLM or the stored workflow history.

> **Assumptions:** every interpretation and every departure from the reference files is listed, with its rationale, in [section 2](#2-assumptions-and-deviations-from-the-reference).

> **All data is synthetic.** Every seeded member, plan, request and reviewer ID starts with `SYN-` (NPIs and numeric keys are invented too, but unprefixed), and every domain row has `is_synthetic = 1`. The dataset was written for this project. It reuses concepts from the reference files, but none of their records.

---

## Quick start

Everything runs offline by default. The LLM runs in deterministic mock mode, so no API key is needed and nothing is billed.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock

.venv/bin/python -m pytest -q                   # 97 tests, offline (1 opt-in live test is skipped)
.venv/bin/python -m evals.run_evals             # PII-leakage, redaction and decision-quality evals
.venv/bin/python -m app.demo --approve          # all 10 scenarios end to end, printed in the terminal
.venv/bin/uvicorn app.main:app --port 8000      # approval UI at http://127.0.0.1:8000 (API docs at /docs)
```

**In the UI:**

1. Sign in as `SYN-R02`, passcode `director-demo` (a medical director).
2. Pick a scenario and click **Run agents**.
3. Look at the validation checks, the cited evidence, the guardrail flags and the token usage.
4. **Approve**, **Request revision** or **Reject**.
5. Watch the follow-up actions and the audit trail. The audit trail shows "hash chain valid" when the log is intact.

| Reviewer | Role | Passcode | What they can do |
|---|---|---|---|
| `SYN-R01` | Nurse reviewer | `nurse-demo` | Approve APPROVE and PEND recommendations. Reject or request revision on anything |
| `SYN-R02` | Medical director | `director-demo` | Everything, including approving DENY and REFER recommendations |
| `SYN-R03` | Inactive account | `inactive-demo` | Nothing. Login is refused |

---

## How the brief maps to the implementation

| Requirement | How it is met | Where | Proven by |
|---|---|---|---|
| 1. Accept a customer ID and a business request | `POST /api/v1/workflows` with `PRIOR_AUTH_REVIEW` (an existing request) or `COVERAGE_INQUIRY` (a would-it-be-covered question). Intake validates the request before a run is created | `app/api/workflows.py`, `app/agents/intake.py` | `test_intake_rejects_bad_requests` |
| 2. Customer info via API-backed DB tools | The member agent calls `get_member_profile`. The call goes tool → internal HTTP API → service → repository → DB | `app/agents/retrieval.py`, `app/agents/tools.py`, `app/api/internal.py` | `test_tool_calls_are_audited`, `test_full_workflow_across_processes` |
| 3. Policy details and applicable rules | The policy agent fetches the enrollment/plan, the auth request, the provider (license and network), coverage rules effective on the service date, and clinical criteria | `app/agents/retrieval.py`, `app/services/domain.py` | `test_each_scenario_reaches_approval_with_expected_outcome` |
| 4. Validate; find missing or conflicting data | 11 deterministic checks (`PASS` / `FAIL` / `UNKNOWN` / `PARTIAL` / `NOT_APPLICABLE`) in the reference's order, each failure carrying the reference's denial reason code, plus data issues (`MISSING` / `CONFLICT` / `WARNING`), all citing evidence IDs | `app/agents/validation.py` | `test_validation_detects_missing_and_conflicting_data` |
| 5. LLM evidence-based recommendation | Structured output. Every finding must cite evidence IDs. Guardrails accept only the outcome the rules support, or an escalation to a physician | `app/agents/recommendation.py`, `app/llm/` | `test_guardrail_*`, decisions eval |
| 6. Submit for human approval | The run stops at `AWAITING_APPROVAL`. Reviewers authenticate, and their role decides what they may approve | `app/orchestrator/engine.py`, `app/services/access_policy.py` | `test_deny_requires_medical_director` |
| 7. Persist approve / reject / revise | An `approvals` row, a checkpoint and an audit event, all in one transaction. A revision creates a new recommendation version, and approving a stale version returns 409 | `app/services/workflow_store.py` | `test_request_revision_creates_new_version_and_stale_approval_is_refused` |
| 8. Permitted follow-up action only after approval | Checked three times: by the orchestrator, by the action executor, and by the service layer inside the write transaction. The determination written (status, reason code, units) must match the approved one. Writes are idempotent | `app/agents/actions.py`, `app/services/action_guard.py` | `test_write_without_human_approval_is_refused_by_service_layer` |
| 9. Durable, auditable history | Per-step checkpoints, resume after a crash, worker leases, and a hash-chained audit log | `app/services/workflow_store.py` | `test_crash_after_side_effect_replays_by_idempotency_key`, `test_audit_chain_verifies_and_detects_tampering` |
| Agents never touch the DB | Agents import no DB, ORM, repository, store, orchestrator, config or secrets modules. A test checks this by parsing their source code | `app/agents/` | `test_agents_do_not_import_database_modules` |
| Token budget | Per-call, per-run and rolling 24-hour limits. The worst case is reserved before each call and settled afterwards, and a blocked call falls back to the rules | `app/llm/budget.py` | `test_llm_budget_guardrails.py` |
| No PII to agents or the LLM | Identifiers stay in the service layer. Free text is redacted, and the prompt and the model's output are filtered | `app/privacy.py`, `app/services/privacy_service.py` | `tests/test_pii.py`, leakage eval |
| Live and mock LLM modes | `LLM_MODE=mock\|live\|auto`. Mock is deterministic and needs no credentials | `app/llm/anthropic_provider.py`, `app/llm/mock.py` | the whole suite (mock), `tests/test_live_smoke.py` (opt-in) |
| Seeded synthetic data | 10 members, each built to exercise one workflow path | `app/seed.py` | `test_synthetic_labels` |

---

## 1. Reference files and how they were interpreted

**Files inspected:** `healthcare_db/build_db.py` (a 1,690-line generator) and `healthcare_db/healthcare.db` (its SQLite output: 24 tables, mostly 30–50 rows each).

**What they describe.** A synthetic **health-insurance prior-authorization** domain. It has members, enrollments, plans and plan types; providers with state licenses and network participation; procedure and diagnosis codes; coverage rules; clinical criteria; auth requests with line items and documents; review history; appeals; and payments. A rule engine (`evaluate()`) adjudicates requests in this order: eligibility → retro timely filing (90 days) → OIG exclusion → licensure in the service state → network → referral, then per service line: license scope → coverage → age → formulary → auto-approval when no prior auth is required → documentation → medical necessity → unit limit (partial approval). Every denial carries a reason code mapped to a CARC code (`denial_reason_codes`). About 28 named scenarios (terminated coverage, expired license, out-of-network on an HMO, missing referral, mid-year rule change, and so on) have deliberate reviewer errors layered on top.

**How "policy" is used.** In the reference files, "policy" means the payer's **coverage/medical policy**: the rules that decide whether a plan covers a service (for example, mandate citations such as "Plan medical policy" in `coverage_rules`). This project keeps that meaning for **policy rules**. It uses "policy details" for the member's plan and enrollment, which those rules apply to. The application's own authorization rules are kept separate:

| Term | Meaning here | Tables / code |
|---|---|---|
| Customer | A health-plan **member** | `members` |
| Policy details | The member's **insurance plan and enrollment**: product, HMO/PPO, referral and out-of-network rules, coverage dates | `plans`, `insurance_products`, `enrollments` |
| Policy rules | **Coverage rules** (by state, insurance category and procedure, effective-dated, with required document, age range and unit limit) and **clinical criteria** (which diagnoses qualify) | `coverage_rules`, `clinical_criteria` |
| Application authorization policy (*not* insurance) | Which agent may call which tool, which reviewer role may approve which outcome, and which actions an approved outcome allows | `app/services/access_policy.py` |

**Reused from the reference:** the plan/product model; enrollment status and coverage window; state licenses with status and expiry; exclusion of providers on the federal (OIG) exclusion list; dated network participation; effective-dated coverage rules; diagnosis-based criteria (with `MN-01`/`MN-02` by ICD chapter); documents with completeness; the 90-day retro filing limit; partial approval at the unit limit; skipping clinical review when no prior auth is required; the denial reason code scheme (internal code → CARC); reviewer roles; the order of the adjudication checks; and the scenario ideas.

**Assumptions.** Every assumption, and every place the implementation departs from the reference, is listed in the next section.

---

## 2. Assumptions and deviations from the reference

The brief left several points open, and the reference files are context rather than a specification. These are the decisions I made. The **Source** column says whether a rule follows the reference (`build_db.py`) or is my own call. The **vs. reference** column states each difference plainly. All of them are easy to change; the code location is given.

### 2.1 Domain interpretation

| # | Assumption | Source | Rationale |
|---|---|---|---|
| D1 | The domain is **health-insurance prior authorization** | Reference | Every table and the rule engine in `build_db.py` describe it |
| D2 | "Customer" = health-plan **member**; "customer identifier" = `member_id` | Reference | `members` is the only customer-like entity |
| D3 | "Policy details" = the member's **enrollment and insurance plan** (product, HMO/PPO, referral and out-of-network rules, coverage dates) | My mapping | These are the plan facts the reference's coverage policy is applied to |
| D4 | "Policy rules" = the payer's **coverage policy**: coverage rules (by state, insurance category and procedure, effective-dated) and **clinical criteria** (which diagnoses qualify) | Reference | This is what the reference calls policy, and what its engine applies |
| D5 | The application's own permissions (which agent may call which tool, which reviewer may approve what, which actions are allowed) are a **separate authorization policy** and are never called "policy rules" | Brief | The brief asked not to mix insurance policies with authorization policies (`app/services/access_policy.py`) |
| D6 | "Business request" = one of two types: `PRIOR_AUTH_REVIEW` (review a submitted auth request) or `COVERAGE_INQUIRY` (would this service be covered, with no request submitted yet) | My call | The brief left the request type open. These two cover a review and a pre-check |

### 2.2 Business-rule decisions

| # | Assumption | Source | vs. reference | Rationale / impact |
|---|---|---|---|---|
| R1 | **Check order:** eligibility → timely filing → OIG exclusion → licensure → network → referral → coverage → age → no-prior-auth short-circuit → documentation → medical necessity → units | Reference | **Same order**, minus license scope and formulary (not modeled) | `app/agents/validation.py` |
| R2 | **Coverage rules are looked up by the plan's state.** Licensure is checked in the service state | My call | **Differs.** The reference looks up coverage by the *service* state | State insurance mandates generally follow the state where the policy is issued. **No demo outcome changes:** service state equals plan state in every seeded scenario. To change it, edit the `get_policy_rules` call in `app/agents/retrieval.py` |
| R3 | **Missing or incomplete required documents → `PEND_FOR_INFORMATION`.** It never becomes a denial | Partly reference | **Differs.** The reference pends until the decision deadline (the shorter of the state deadline and the plan's turnaround, or 72 hours if expedited), then denies (`DOC-02`/`DOC-03`). Decision deadlines are not modeled | A human decides when to stop waiting. The `DOC-02`/`DOC-03` codes are already attached to the check, so enforcing a deadline later is straightforward |
| R4 | **Conflicting records go to a medical director** (`REFER_TO_MEDICAL_DIRECTOR`). Conflicts are: two or more coverage rules effective on the same date with different terms, more than one active enrollment on the service date, or a request that has already been decided | My call | **Not in the reference.** It takes the first matching rule and ignores duplicate enrollments | No rule can settle contradictory data, so someone with authority has to decide. This modeling is debatable; see 2.5 (G1). Scenarios SYN-M0003, SYN-M0008 |
| R5 | **Outcome precedence:** conflict → REFER; *hard* failure (eligibility, timely filing, exclusion, license, network, referral, coverage, age) → DENY; missing or unknown information → PEND; *soft* failure (medical necessity) → DENY; otherwise → APPROVE (partial if the unit limit applies) | My call | The reference returns the first failure at request level, then aggregates per line | Missing information shouldn't produce a denial that the information could reverse. A hard failure can't be fixed by more information. Scenario SYN-M0007 pends although its diagnosis also fails criteria, because its notes are incomplete |
| R6 | **Annual unit limit exceeded → partial approval:** APPROVE with units reduced to the limit and reason code `COV-04` | Reference | **Same** | Scenario SYN-M0009: 30 units requested, 20 approvable |
| R7 | **A license on probation passes** the license check, with no escalation | Partly reference | **Differs.** The reference's code escalates probation cases to a medical director, although its generated data contains no such escalation | Not needed by any demo scenario. Could be added as a `WARNING` that forces REFER |
| R8 | **Missing license expiry date → `UNKNOWN` → PEND** (credentialing must verify) | Reference | Same | Scenario SYN-M0007 |
| R9 | **Out of network:** fails on HMO/EPO plans. Mine also checks the plan's out-of-network benefit flag | Reference | Same in effect. The reference ignores the `oon_coverage` flag | Scenario SYN-M0005 |
| R10 | **Referral** is required when the plan requires one and the provider is not in primary care | Reference | Same | Scenarios SYN-M0002, SYN-M0005 |
| R11 | **Medical necessity uses the primary diagnosis only.** The code is `MN-01` when a qualifying diagnosis is in the same ICD chapter, otherwise `MN-02` | Reference (codes) | **Differs.** The reference also accepts a qualifying secondary diagnosis | Simplification: one diagnosis per request |
| R12 | **No state coverage rule → the procedure's default** (covered; its default prior-auth and document requirements), flagged `WARNING` | Reference | Same | Keeps unknown combinations reviewable rather than failing them |
| R13 | **No prior auth required → clinical checks are skipped** (documentation, medical necessity and units become `NOT_APPLICABLE`), as in the reference. **The case still goes to a human** | Reference + brief | **Differs only in sign-off.** The reference auto-approves these with no reviewer | The brief requires human approval before any follow-up action |
| R14 | **Who may approve what:** a nurse reviewer or medical director for APPROVE or PEND; **only a medical director for DENY or REFER**. Anyone may REJECT, or REQUEST_REVISION with a comment (3 revisions at most) | My call | **Stricter.** The reference sends only medical-necessity denials (`MN-*`) to a medical director. Administrative denials go to nurses, and pharmacy cases to pharmacists | Conservative default for adverse decisions. To match the reference, limit the medical-director requirement to medical-necessity denials in `approval_permitted()` in `app/services/access_policy.py` |
| R15 | **Follow-up actions** are fixed per approved outcome: record the determination and notify the provider (APPROVE/DENY), request documents (PEND), escalate (REFER). A coverage inquiry never records a determination, because there is no request to update | My call | The reference has no action layer | Nothing leaves the machine: notices go to a local `notices` outbox table |
| R16 | **Denial reason codes:** every failing check carries the reference's code: `ELG-01/02`, `ADM-01`, `LIC-01/02/04`, `NET-01`, `REF-01`, `COV-01/03/04`, `DOC-02/03`, `MN-01/02`. A recorded determination stores the status, the reason code (denials and partial approvals) and the units approved, and notices quote them | Reference | **Same scheme** (internal code mapped to a CARC code). Only the codes this workflow can produce are seeded | The determination is derived from the checks, never from the LLM, and the service layer refuses a write whose code or units differ from the approved determination |
| R17 | **Timely filing:** a request submitted after its service date is retrospective. More than 90 days late → `FAIL`, `ADM-01` | Reference | Same 90-day limit. Retro is inferred from the dates, because request types are not modeled | All seeded requests are prospective. A test exercises 45-day and 105-day retro filings |
| R18 | **A coverage inquiry has no documents to inspect:** a required document is reported as a future requirement (`NOT_APPLICABLE`), not as missing | My call | Not in the reference (no inquiries) | An inquiry asks whether the service is covered, not whether a submission is complete |

### 2.3 Scope simplifications

| # | Assumption | Rationale |
|---|---|---|
| S1 | **New, smaller schema and dataset:** 17 domain tables and 10 hand-written members, each built for one workflow path. Every row is synthetic (`is_synthetic`). Member, plan, request and reviewer IDs are prefixed `SYN-`; NPIs and numeric keys are invented but unprefixed | Readable, deterministic scenarios. Nothing is copied from the reference database. The denial reason codes reuse the reference's code scheme |
| S2 | **One service line and one diagnosis per request** | The reference has multi-line requests (7) and secondary diagnoses (11) |
| S3 | **Not modeled:** see 2.5 (G2) for the full list | None of these affects the requested workflow, and the schema can be extended |
| S4 | **Members carry synthetic personal identifiers** (name, date of birth, sex, an SSN from the never-issued 9xx range, address, phone, email), and document text names the patient | So the PII controls are tested against realistic data. None of it leaves the service layer (section 8) |
| S5 | **Dates are set in late 2026:** requests submitted 2026-10-19, services in 2026-11 | Keeps the scenarios current |

### 2.4 Engineering and operational assumptions

| # | Assumption | Rationale |
|---|---|---|
| E1 | **The LLM defaults to deterministic mock mode.** Live mode is opt-in (`LLM_MODE=live`, or `auto`, which goes live when a key is present) | Runs and tests offline, needs no credentials, nothing billed by accident, reproducible results |
| E2 | **"Check the token budget" means enforcing limits** per call, per run (callers can lower it, never raise it) and per rolling 24 hours, with a rule-based fallback when a call is blocked | My reading of the requirement. Usage is also reported per run |
| E3 | **Live model `claude-opus-5-5` at `medium` effort**, with server-side refusal fallback | Configurable through environment variables |
| E4 | **An explicit persisted state machine instead of LangGraph** | The brief allowed it. LangGraph's APIs couldn't be verified offline, and the brief said not to invent APIs |
| E5 | **SQLite, single host.** Atomic operations rely on SQLite's write lock | Zero setup. Postgres equivalents are listed in the limitations |
| E6 | **Demo-grade reviewer login** (seeded passcodes, signed tokens) | The brief asked for a simple approval interface, not identity management |
| E7 | **"No PII" means no direct identifiers outside the service layer.** Clinical codes (CPT, ICD-10) and the member's age are passed as the minimum needed. Provider and reviewer names are staff data and are shown. Names of *third parties* in free text are not detected (that would need NER) | Data minimization while keeping the task possible (section 8) |

### 2.5 Known gaps (disclosed, not built)

| # | Gap | What the reference does | What this project does | How it would be built |
|---|---|---|---|---|
| G1 | **Conflict modeling is a judgment call** | `rule_change` scenario: the coverage rule changed mid-year, and the case turns on whether the rule at submission or at service applies. With overlapping rules it takes the first match. A member with two plans would be a coordination-of-benefits question (`ELG-04`) | Applies the rules effective on the **service date**. Treats two effective rules with different terms as a conflict, even though a newer rule would normally **supersede** an older one that was never end-dated (SYN-M0003). Treats two active enrollments as a generic conflict and refers the case, rather than determining the primary payer (SYN-M0008) | Rule versioning with explicit supersession (newest effective version wins, older versions end-dated) and a configurable as-of date (submission vs. service). A COB step that ranks enrollments (subscriber before dependent, birthday rule) and returns `ELG-04` when another payer is primary |
| G2 | **Reference concepts not modeled** | Telehealth and the interstate license compact (`imlc_member`, `via_compact`); urgency with an expedited 72-hour turnaround; decision deadlines; `RETRO_AUTH`, `CONCURRENT` and `PHARMACY` request types; `INTAKE`, `PHARMACIST` and `APPEALS_SPECIALIST` roles; `review_history`; appeals (three levels); payments; license-scope rules; formulary and step therapy; multi-line requests; facilities; deliberate reviewer-error scenarios | `urgency` is stored but not used. Retro filing is inferred from dates (R17). `review_history` is replaced by the hash-chained `audit_events`, which records every step, tool call and decision. On the reviewer-error side, license validity is judged on the **expiry date** against the service date, not on the cached status, which catches the reference's `err_missed_stale` pattern (a stale `ACTIVE` status). The other error scenarios (overlooked document, ignored secondary diagnosis) are not reproduced | Each is an additional check, role or request type on the same pipeline: for example a `TELEHEALTH_LICENSURE` check using compact membership, or deadline-driven `DOC-02` denials from R3 |
| G3 | **Revision and pend handling are thin** | Pended cases wait for documents; the payer re-evaluates when they arrive | `REQUEST_REVISION` re-runs only the **recommend** step on the same retrieved data; it does not re-fetch or re-validate. Approving a PEND sends the document request and **ends the run**, leaving the auth request `PENDING` with no path back into that run; new information means starting a new run | Route revisions back to `retrieve_policy` so changed data is picked up; add a `WAITING_FOR_INFORMATION` state that a document-received event resumes |

---

## 3. Architecture

```
       Approval UI (static/index.html)  ── reviewer bearer token ──┐
                                                                   ▼
  Public app  app/main.py      /api/v1/*   (auth, workflows, decisions, audit, token usage)
                                                                   │
  Orchestrator  app/orchestrator/engine.py — explicit persisted state machine
     intake → retrieve_member → retrieve_policy → validate → recommend → AWAITING_APPROVAL
                       APPROVE → execute_actions → COMPLETED
                       REJECT  → REJECTED
                       REQUEST_REVISION → recommend (with the reviewer's feedback) → AWAITING_APPROVAL
     each step: state + checkpoint + audit events (+ recommendation / action results) in ONE transaction
          │                                                │
          │ agents (app/agents)                            │ LLM adapter (app/llm): mock | Anthropic
          │ ToolClient: Pydantic-validated in/out,         │ TokenBudget: reserve → call → settle
          │ per-agent credential minted by orchestrator    │ guardrails + PII filter
          ▼                                                ▼
  Internal app  app/internal_main.py   /internal/v1/*   (separate ASGI app; NOT mounted on the public app)
          ▼   per-agent HMAC credential + per-agent grants; Idempotency-Key on writes
  Service layer  app/services/  (domain.py, action_guard.py = approval re-check, privacy_service.py)
          ▼
  Repository  app/repositories.py  →  SQLite (SQLAlchemy 2)
```

### What happens on one request

1. **Start.** `POST /api/v1/workflows` arrives. Free-text `notes` are redacted, and a run is created with status `RUNNING` and a `WORKFLOW_STARTED` audit event.
2. **`intake`** checks the request. A `PRIOR_AUTH_REVIEW` needs `auth_request_number`. A `COVERAGE_INQUIRY` needs `cpt_code`, `icd10_code` and `service_date`.
3. **`retrieve_member`.** The member agent calls `get_member_profile` and gets back the member ID, state and enrollments, with no identifiers.
4. **`retrieve_policy`.** The policy agent calls `get_auth_request`, `get_member_age` (age on the service date, computed by the server), `get_provider` (license in the service state, network participation for the plan) and `get_policy_rules` (rules effective on the service date, plus clinical criteria).
5. **`validate`.** Eleven deterministic checks run in the reference's order, missing and conflicting data are flagged, and an evidence catalog is built (`enrollment:1`, `coverage_rule:7`, `check:NETWORK`, …).
6. **`recommend`.**
   * The evidence bundle is built.
   * The prompt is screened for PII.
   * Tokens are counted and the budget is reserved.
   * The LLM returns structured output.
   * Guardrails and PII redaction are applied.
   * The recommendation is saved as version N, and the run moves to `AWAITING_APPROVAL`.
7. **Human decision** (`POST /workflows/{id}/decisions`). The reviewer comes from the bearer token, and their role is checked against the outcome. The decision is saved in one transaction.
8. **`execute_actions`** (only after APPROVE). Each permitted action is called with an idempotency key. The service layer re-verifies the approval inside the write. The run ends `COMPLETED`.

**Run statuses:**

* `RUNNING`, `AWAITING_APPROVAL`: in progress.
* `COMPLETED`, `REJECTED`: finished normally.
* `FAILED`: can be resumed from its checkpoint.
* `INVALID_REQUEST`: bad input (unknown member, request or procedure). Final; cannot be resumed.

### Agents and least privilege

| Agent | Responsibility | Internal API operations it may call |
|---|---|---|
| `intake_agent` | Validate and normalize the business request | none |
| `member_retrieval_agent` | Customer profile and enrollments | `get_member_profile` |
| `policy_retrieval_agent` | Auth request, age on the service date, provider, policy rules | `get_auth_request`, `get_member_age`, `get_provider`, `get_policy_rules` |
| `validation_agent` | Deterministic checks; missing and conflicting data | none (works only on state handed to it) |
| `recommendation_agent` | LLM recommendation under budget and guardrails | none (gets only the evidence bundle) |
| `action_executor` | Follow-up actions after approval | `record_determination`, `create_notice` |

### Trust boundaries

* **Agents can't reach the database.** Persistence belongs to the orchestrator. The LLM provider, the budget guard and the PII filter are passed in to the recommendation agent. The action executor returns its results for the orchestrator to store.
* **The internal API is a separate app.** By default it runs in-process over ASGI, without a socket. It can also run as its own service bound to localhost (see Configuration). Either way, the public app never serves `/internal/*` (it returns 404).
* **Each agent has its own credential.** It is an HMAC of the agent's ID under `INTERNAL_API_SECRET`. The orchestrator mints it, so agents never see the secret. One agent can't claim another's identity, and each route checks the caller's grants.
* **Startup refuses unsafe configuration:** a weak secret, or a missing secret when a remote internal API is configured.
* **Every tool call is schema-validated and audited.** Pydantic validates the input before the call and the response after it. The audit records agent, tool, arguments, status and latency.
* **The service layer itself enforces "only after approval"** (`action_guard.py`), inside the write transaction. A valid credential is not enough. The write must belong to a run that:
  * is currently executing its actions;
  * has, as its current approval, an APPROVE of its latest recommendation;
  * proposed this action, and the action is permitted for the outcome;
  * concerns the same member and request;
  * records a determination that matches the outcome (APPROVED for APPROVE, DENIED for DENY).
* **Writes are idempotent on the server.** The `Idempotency-Key` is stored in the same transaction as the write. A replay returns the original response, and reusing a key with a different body returns 409.
* **Reviewers authenticate.** Passcodes are stored as PBKDF2 hashes, and login returns a signed, expiring token. The decision's reviewer identity comes only from the token. Inactive accounts are refused on every request, so deactivating an account immediately invalidates its existing tokens.
* **Shared database.** The internal service reads the run, approval and recommendation tables to authorize writes, so both apps share one database.

---

## 4. Validation, recommendation and guardrails

**Checks.**

In the reference's order (R1), each with a reference denial code on failure (R16):

* *Hard* checks: `ELIGIBILITY` (`ELG-01/02`), `TIMELY_FILING` (`ADM-01`), `PROVIDER_EXCLUSION` (`LIC-04`), `PROVIDER_LICENSE` (`LIC-01/02`), `NETWORK` (`NET-01`), `REFERRAL` (`REF-01`), `COVERAGE` (`COV-01`), `AGE` (`COV-03`). More information can't fix a failure here.
* *Soft* checks: `DOCUMENTATION` (`UNKNOWN` when missing; `DOC-02/03`), `MEDICAL_NECESSITY` (`MN-01/02`), `UNITS` (`PARTIAL` with `COV-04` when over the annual limit). These may change when information arrives.
* If the service needs **no prior authorization**, the three soft checks are skipped, as in the reference (R13).

**Deterministic outcome**, which is also the mock model and the fallback, decided in this order:

1. any conflict → `REFER_TO_MEDICAL_DIRECTOR`;
2. a hard failure → `DENY`;
3. missing or unknown information → `PEND_FOR_INFORMATION`;
4. a soft failure → `DENY`;
5. otherwise → `APPROVE` (with units reduced to the limit if `UNITS` is `PARTIAL`).

**Determination.** A recorded determination contains the status, the reason code (the first hard failure, else the first failure; `COV-04` for a partial approval) and the units approved. It is derived from the checks, never from the LLM (`validation.determination_for`).

**LLM output.** The model returns an outcome, a summary, findings (each with evidence IDs), missing information, proposed actions and a confidence level, validated against a Pydantic schema. The guardrails then:

* drop cited evidence IDs that aren't in the catalog, and any finding left with no evidence;
* accept **only the deterministic outcome**. Any other outcome is overridden and flagged `OUTCOME_OVERRIDDEN`: for example a DENY where the rules say PEND (missing information) or REFER (a conflict), or an APPROVE over a failure. The override also replaces the model's summary and findings with the deterministic ones, so prose arguing for the rejected outcome never reaches a notice;
* allow the one exception, escalation to a medical director, but flag it (`ESCALATED_BEYOND_RULES`);
* intersect the proposed actions with the actions permitted for the outcome;
* redact PII from all model-written text and remove control characters;
* set confidence to LOW whenever any flag fires, and store `rules_outcome` / `agrees_with_rules` so the reviewer can see any disagreement.

**When the LLM is unavailable**, because of a refusal, truncation, an API error, invalid output or a blocked budget, the run still gets the deterministic, evidence-cited recommendation, labeled `rules_fallback`.

---

## 5. Human approval and follow-up actions

**Who may approve what.**

* An APPROVE or PEND recommendation can be approved by a nurse reviewer or a medical director.
* A DENY or REFER recommendation can be approved only by a medical director. This is a conservative choice, stricter than the reference, which requires a medical director only for medical-necessity denials (assumption R14).
* Any active reviewer can REJECT, or REQUEST_REVISION with a comment, up to 3 revisions per run.
* A decision on a stale recommendation version returns 409.
* Refused attempts are audited.

**Follow-up actions permitted after approval:**

| Approved outcome | PRIOR_AUTH_REVIEW | COVERAGE_INQUIRY |
|---|---|---|
| APPROVE / DENY | `RECORD_DETERMINATION` (status, reason code, units approved), `NOTIFY_PROVIDER` (quotes them) | `NOTIFY_PROVIDER` |
| PEND_FOR_INFORMATION | `REQUEST_DOCUMENTS` | `REQUEST_DOCUMENTS` |
| REFER_TO_MEDICAL_DIRECTOR | `ESCALATE_TO_MEDICAL_DIRECTOR` | `ESCALATE_TO_MEDICAL_DIRECTOR` |

Notices are written to a local `notices` outbox table. Nothing is sent anywhere.

---

## 6. Durability, concurrency and audit

* **Atomic steps.** Each step's state, checkpoint, audit events and outputs are committed in one transaction, guarded by an optimistic version number and by the run's worker lease.
* **Worker leases.** Only the worker holding a run's lease may advance it. The lease is taken by an atomic compare-and-set on `lease_owner` / `lease_expires_at`, and renewed by a heartbeat every `RUN_LEASE_SECONDS / 3`, including during long LLM calls. This is safe across processes and workers that share the database. A worker that has lost its lease can't write a checkpoint.
* **Crash recovery.** At startup, `RUNNING` runs whose lease has expired are marked `FAILED`. Token reservations left by calls that died are settled at their worst case.
* **Resume.** `POST /workflows/{id}/resume` atomically claims a `FAILED` run, or a `RUNNING` run with an expired lease, and continues from its last checkpoint. Actions that were already applied are replayed by idempotency key, not executed again. A test kills the process right after a determination is committed, resumes, and checks for no 409 and exactly one notice.
* **Audit log.** Events are chained per run with `hash = sha256(prev_hash + canonical(event))`. `GET /workflows/{id}/audit` returns the events and an integrity check, and a test confirms that editing any event breaks the chain.
* **What is audited:** start, every step, every tool call, token-budget decisions, every recommendation (with its flags), approval requests, every decision (including refused attempts), every action, failures, resumes, recovery, and PII redactions (the kinds removed, never the values).

---

## 7. Token budget

**Before every LLM call,** in one transaction (`app/llm/budget.py`, `workflow_store.reserve_tokens`):

1. **Count the input.** Live mode uses `messages.count_tokens`. Mock mode estimates about 4 characters per token. If counting fails, a local estimate inflated by 40% is used.
2. **Reject** input above `TOKEN_BUDGET_MAX_INPUT_PER_CALL`.
3. **Size the output:** `max_tokens = min(per-call output cap, run budget − run used − this input)`. Reject if fewer than 1,024 output tokens would remain.
4. **Reject** the call if the rolling 24-hour total plus this call would exceed `TOKEN_BUDGET_DAILY`.
5. **Reserve the worst case** (input + `max_tokens`) as a `RESERVED` ledger row, which counts against every budget immediately. After the call, `settle()` replaces it with the actual reported usage: input, output, and cache write and read tokens.

**Why concurrent runs can't overshoot.** The reservation row is inserted *before* the budgets are summed, in the same transaction, and on SQLite that insert takes the write lock. A test fires 12 threads at a budget that fits 3 calls, and exactly 3 succeed.

**Rules:**

* A caller may lower a run's budget, never raise it above `TOKEN_BUDGET_PER_RUN`. Both the requested and the effective value are audited.
* The run budget is cumulative across revisions.
* **When a call is blocked,** no LLM call is made. The run gets the `rules_fallback` recommendation with a `TOKEN_BUDGET_EXCEEDED` flag, the ledger records `BLOCKED_BUDGET`, and an audit event is written. The reviewer sees all of it.

`GET /api/v1/workflows/{id}/token-usage` returns the run's budget, usage, remaining tokens and per-call ledger. The UI shows the same information.

---

## 8. Personal data (PII)

**Policy.** Direct identifiers (name, date of birth, sex, SSN, street address, phone, email) and clinical free text stay inside the service layer. Agents, the LLM, workflow state, checkpoints, the audit log, notices, determination notes and public API responses receive only:

* the pseudonymous `member_id`;
* state, plan and enrollment data;
* clinical codes;
* the member's **age** on the service date, computed by the server;
* document metadata, without document text.

**Enforcement, in layers** (`app/privacy.py`, `app/services/privacy_service.py`):

1. **No PII fields in agent-facing models.** A test walks every agent-facing response model, nested models included, and fails on any field in `PII_FIELD_NAMES`.
2. **Free text is redacted before storage.** This covers request notes and reviewer comments. Redaction runs against the member's own identifiers on record (name variants, date of birth in six formats, phone digits, SSN with or without dashes, address) and against generic patterns (email, phone, SSN, labelled birth dates, street addresses). The identifiers are used inside the service layer and never returned. The audit log records only the *kinds* removed.
3. **The final prompt is filtered** as a last line of defense, flagged `PII_REDACTED_FROM_PROMPT`. A test simulates a future bug that adds contact details to the prompt and checks they are caught.
4. **Model output is redacted** before it is stored, shown, or written into a notice, flagged `PII_REDACTED_FROM_MODEL_OUTPUT`.

**Not covered:** names of *other* people in free text (a relative, a neighbour) would need an NER model. The eval reports these cases as out of scope rather than hiding them. Provider and reviewer names are staff data and are shown.

---

## 9. Testing and evals

**Tests** (`pytest`, offline, about 50 seconds):

| File | Tests | Covers |
|---|---|---|
| `tests/test_workflow.py` | 42 | All scenarios; approve, reject and revise; stale versions; role rules; reviewer authentication and deactivation; intake validation; invalid requests (unknown member, procedure, diagnosis); denial reason codes recorded per scenario; `MN-01`/`MN-02`; partial approval (`COV-04`); the no-prior-auth short-circuit; retro timely filing; resume from checkpoint; crash after a side effect; leases; heartbeat; recovery |
| `tests/test_pii.py` | 26 | No PII fields in agent-facing models; all internal endpoints for all members; every scenario end to end (every prompt, stored row and API response scanned); PII injected into notes, comments, model output and the prompt; redaction formats; clinical data left intact |
| `tests/test_llm_budget_guardrails.py` | 16 | Per-call, per-run, cumulative and daily limits; budget cap; output sizing; concurrent reservations; fallbacks; every guardrail rule, including a model DENY overridden to PEND and to REFER |
| `tests/test_boundaries_and_audit.py` | 11 | Agents can't import DB or secret modules; the internal API is not on the public app; credentials and grants; writes without an approval are refused; writes that contradict the approved determination (status, reason code or units) are refused; schema validation; audit hash chain and tamper detection; synthetic labels |
| `tests/test_two_process.py` | 1 | Public app and internal API as two real uvicorn processes over sockets, running a full workflow |
| `tests/test_evals.py` | 1 | Runs the eval harness in mock mode and asserts every gate passes |
| `tests/test_live_smoke.py` | 1 (skipped) | Opt-in and billed: one real `count_tokens` call and one real generation |

The PII tests were checked by breaking the code on purpose. Disabling redaction failed 4 tests, and leaking a surname through a tool failed 2.

**Evals** (`python -m evals.run_evals`). They print a summary, write a JSON report to `evals/reports/`, and exit non-zero if a gate fails.

| Suite | Dataset | Measures | Gate | Mock result |
|---|---|---|---|---|
| `redaction` | `evals/datasets/redaction_cases.jsonl`, 39 labelled cases | Recall on PII; preservation of clinical and admin text | recall ≥ 0.98, preservation ≥ 0.98 | 1.0 / 1.0 (out-of-scope cases reported: 0 of 4 removed) |
| `leakage` | `evals/datasets/workflow_cases.jsonl`, 8 workflows | Adversarial PII in notes, comments and model output; third-party contacts; a prompt-injection attempt asking the model to print identifiers; a prompt-building regression. Every prompt, stored row and API response is scanned | 0 leaks | 0 of 8 leaked, 56 prompts scanned |
| `decisions` | `evals/datasets/decision_cases.jsonl`, 10 scenarios | Outcome accuracy; citations resolve to evidence; required evidence cited; guardrail and fallback rates | accuracy ≥ 0.9, citations 100% valid | 1.0 |

With redaction disabled on purpose, the leakage eval reports 6 of 8 cases leaking, so it does catch leaks. In mock mode the `decisions` suite checks the pipeline. `--live` runs the same suites against the real model, which is billed.

---

## 10. Key design decisions and trade-offs

* **An explicit state machine instead of LangGraph.** The brief allowed "an equivalent explicit persisted state machine". LangGraph wasn't installed, and its checkpointer and interrupt APIs couldn't be verified offline for a pinned version. The brief said not to invent framework APIs. An explicit machine also keeps persistence, approval and lease semantics fully visible. All wiring lives in `engine.NODES` and `_advance()`, so a graph runtime could replace it without touching the agents.
* **Deterministic rules first, the LLM second.** Eligibility, licensure and coverage are rule questions, so they are computed deterministically and cited as evidence. The LLM's job is to synthesize and explain, and guardrails keep it from producing an outcome the evidence doesn't support. This keeps outcomes auditable and makes the mock mode meaningful.
* **Guardrails win over the model, and say so.** The model may agree with the rules or escalate, nothing else. Overrides are flagged and set confidence to LOW; nothing is silently changed. What gets written (status, reason code, units) is derived from the checks, never from the model.
* **Approval is enforced where the write happens.** Checking only in the orchestrator would trust every caller. The service layer re-verifies the approval in the same transaction.
* **Mock is the default LLM mode.** The project runs and tests offline, and nothing is billed unless someone opts in.
* **SQLite** keeps setup to zero. The atomic parts (leases, budget reservation) rely on its single-writer lock; the Postgres equivalents are noted under Limitations.
* **The model is `claude-opus-5-5` at `medium` effort,** with server-side refusal fallback on. All three are configurable.

**LLM adapter details.** It uses `anthropic==2.1.0`:

* `client.messages.parse(output_format=<Pydantic model>, output_config={"effort": ...})` for structured output;
* `client.messages.count_tokens` for budgeting;
* when `LLM_SERVER_FALLBACKS=true`, `client.beta.messages.parse(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`.

All signatures were checked against the installed package.

---

## 11. Configuration

Set these in the environment or in `.env`; see `.env.example`. Every one has a safe default.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` \| `live` \| `auto` (live if `ANTHROPIC_API_KEY` is set) |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID` | – | Live credentials. The workspace ID is needed only for keys not scoped to a workspace |
| `LLM_MODEL`, `LLM_EFFORT`, `LLM_SERVER_FALLBACKS` | `claude-opus-5-5`, `medium`, `true` | Model settings |
| `TOKEN_BUDGET_PER_RUN` / `_MAX_INPUT_PER_CALL` / `_MAX_OUTPUT_PER_CALL` / `_DAILY` | 40,000 / 12,000 / 8,000 / 500,000 | Token limits |
| `DATABASE_URL` | `sqlite:///./data/pa_orchestrator.db` | Database. Created and seeded on first start; rebuild with `python -m app.seed --reset` |
| `INTERNAL_API_SECRET` | random per process | Root of the agents' credentials. At least 32 characters. Required when `INTERNAL_API_BASE_URL` is set |
| `INTERNAL_API_BASE_URL` | empty (in-process) | Run the internal API as its own service (below) |
| `REVIEWER_TOKEN_SECRET`, `REVIEWER_TOKEN_TTL_SECONDS` | random, 28800 | Reviewer session tokens |
| `RUN_LEASE_SECONDS`, `TOKEN_RESERVATION_EXPIRY_SECONDS` | 30, 900 | Worker lease length; when an unsettled reservation counts as abandoned |

**Two-process deployment** (covered by `tests/test_two_process.py`):

```bash
export INTERNAL_API_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
export INTERNAL_API_BASE_URL=http://127.0.0.1:8001
.venv/bin/uvicorn app.internal_main:internal_app --host 127.0.0.1 --port 8001 &
.venv/bin/uvicorn app.main:app --port 8000
```

**Live LLM:**

* Set `LLM_MODE=live` and `ANTHROPIC_API_KEY`, plus `ANTHROPIC_WORKSPACE_ID` if the API asks for it.
* Smoke test (billed): `RUN_LIVE_LLM_TESTS=1 .venv/bin/python -m pytest tests/test_live_smoke.py -q`.
* Live evals (billed): `.venv/bin/python -m evals.run_evals --live`.

---

## 12. API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/auth/login` | `{reviewer_id, passcode}` → bearer token, required by every `/workflows` endpoint |
| POST | `/api/v1/workflows` | Start a run: `{member_id, request: {request_type, auth_request_number \| cpt_code, icd10_code, units, service_date, notes?}, token_budget?}` |
| GET | `/api/v1/workflows`, `/api/v1/workflows/{id}` | List runs, or get one run with its state, recommendations, approvals, actions, checkpoints and token usage |
| POST | `/api/v1/workflows/{id}/decisions` | `{decision: APPROVE\|REJECT\|REQUEST_REVISION, recommendation_version, comment?}`. The reviewer comes from the token |
| POST | `/api/v1/workflows/{id}/resume` | Resume a `FAILED` run, or a `RUNNING` run with an expired lease |
| GET | `/api/v1/workflows/{id}/audit` | Audit events and a hash-chain integrity check |
| GET | `/api/v1/workflows/{id}/token-usage` | Budget, usage and per-call ledger |
| GET | `/api/v1/demo/scenarios`, `/api/v1/demo/reviewers` | Synthetic fixtures for the UI |
| * | `/internal/v1/...` (internal app only) | The agents' domain API: per-agent credential, grants, `Idempotency-Key` on writes |

**Example:**

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"reviewer_id":"SYN-R02","passcode":"director-demo"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8000/api/v1/workflows -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"member_id":"SYN-M0004","request":{"request_type":"PRIOR_AUTH_REVIEW","auth_request_number":"SYN-PA-0004"}}'
```

---

## 13. Demo scenarios (synthetic)

| Member | Request | What it exercises | Recommended outcome | Recorded on approval |
|---|---|---|---|---|
| SYN-M0001 | SYN-PA-0001 | Clean case | APPROVE | APPROVED, 1 unit |
| SYN-M0002 | SYN-PA-0002 | HMO specialist without a referral | DENY | DENIED, `REF-01` |
| SYN-M0003 | SYN-PA-0003 | Two overlapping coverage rules with different terms (conflict, see G1) | REFER_TO_MEDICAL_DIRECTOR | escalation notice |
| SYN-M0004 | SYN-PA-0004 | Provider's license expired before the service date | DENY | DENIED, `LIC-01` |
| SYN-M0005 | SYN-PA-0005 | Out of network on a Medicaid HMO, also no referral | DENY | DENIED, `NET-01` (first hard failure) |
| SYN-M0006 | SYN-PA-0006 | Coverage ends 2026-10-31, before the 2026-11-16 service | DENY | DENIED, `ELG-01` |
| SYN-M0007 | SYN-PA-0007 | License expiry missing, clinical notes incomplete (missing data) | PEND_FOR_INFORMATION | document request |
| SYN-M0008 | SYN-PA-0008 | Two overlapping active enrollments (conflict / COB, see G1) | REFER_TO_MEDICAL_DIRECTOR | escalation notice |
| SYN-M0009 | coverage inquiry | 30 physical-therapy units against a 20-unit annual limit | APPROVE (partial) | notice: 20 units, `COV-04` (an inquiry records no determination) |
| SYN-M0010 | SYN-PA-0010 | Member below the rule's minimum age | DENY | DENIED, `COV-03` |

---

## 14. Project layout

```
app/
  main.py               public FastAPI app (UI + /api/v1), startup recovery
  internal_main.py      internal FastAPI app (/internal/v1), agents only
  config.py, security.py  settings and secret checks; agent credentials, reviewer tokens, passcode hashing
  privacy.py            PII patterns, redaction, deep scanner (pure)
  models.py, db.py      SQLAlchemy models (17 domain + 8 workflow tables); engine and sessions
  schemas.py            Pydantic contracts for the internal API and tools
  repositories.py       the only module that queries domain tables
  seed.py               synthetic dataset and demo reviewers
  demo.py               command-line demo
  api/                  internal.py (agent API), workflows.py (public API)
  services/             domain.py, action_guard.py, access_policy.py, privacy_service.py, workflow_store.py
  agents/               intake, retrieval, validation, recommendation, actions, tools (ToolClient)
  llm/                  base.py (contract), mock.py, anthropic_provider.py, budget.py
  orchestrator/engine.py  state machine, leases, resume and recovery, decisions
static/index.html       approval UI (sign-in, run, evidence, decide, audit)
tests/                  98 tests (97 offline, 1 opt-in live)
evals/                  harness.py, run_evals.py, datasets/*.jsonl
healthcare_db/          the provided reference files (read-only)
requirements.txt / requirements.lock   direct pins / full pinned environment
```

---

## 15. Limitations and next steps

* **Reviewer login is demo-grade.** Passcodes and signed tokens, with immediate deactivation, but no identity provider, MFA, per-token revocation or rate limiting. **Next:** OIDC.
* **Workflows run synchronously inside the HTTP request.** Leases make several workers safe, but a long LLM call keeps the request open. **Next:** a job queue, with the UI polling the run.
* **The atomic parts rely on SQLite's single-writer lock.** That covers leases and budget reservation. **Next:** on Postgres, use `SELECT … FOR UPDATE` or an advisory lock.
* **Recovery runs only at startup.** A worker that dies while others keep running leaves its runs resumable but not resumed automatically. **Next:** a periodic sweeper.
* **Usage after a crash mid-call is unknown.** If the process dies during an LLM call, recovery charges the reserved worst case. If it dies after the call but before the checkpoint, resuming makes a second, separately budgeted call.
* **The live LLM path is only partly verified.** SDK signatures were checked against the installed package, and the provided key reached authentication but needs `ANTHROPIC_WORKSPACE_ID`. No real generation has been run, so live structured output, and the usage reported when a server-side fallback runs, are unconfirmed until the opt-in smoke test or `--live` evals are run.
* **PII detection is patterns plus the member's own identifiers.** Names of third parties need NER. The eval reports them as out of scope.
* **Gaps G1–G3 in section 2.5:** conflict and supersession modeling, reference concepts not modeled, and thin revision/pend handling.
* **No schema migrations.** Tables are created with `create_all`; rebuild with `python -m app.seed --reset`.
* **The rules are simplified demonstration rules,** not clinical or legal guidance.
