"""Validation agent: deterministic policy checks + missing/conflicting data detection.

Works only on the retrieved state (no tools, no LLM). Every check and issue cites
evidence IDs from the evidence catalog it builds, so the recommendation can be
traced back to specific records.

Check statuses: PASS | FAIL | UNKNOWN (data missing) | NOT_APPLICABLE.
`hard` checks are failures that more information cannot cure (eligibility,
licensure, network, ...). Soft checks (criteria, units, documentation) may change
once missing information arrives.
"""
from datetime import date

NETWORK_RESTRICTED = ("HMO", "EPO")


def _d(x: str | None) -> date | None:
    return date.fromisoformat(x) if x else None


def run(intake: dict, profile: dict, policy: dict) -> dict:
    evidence: dict[str, str] = {}
    checks: list[dict] = []
    issues: list[dict] = []

    def ev(eid: str, text: str) -> str:
        evidence[eid] = text
        return eid

    def check(name, status, detail, ids, hard):
        checks.append({"check": name, "status": status, "detail": detail, "evidence_ids": ids, "hard": hard})

    def issue(kind, code, message, ids):
        issues.append({"kind": kind, "code": code, "message": message, "evidence_ids": ids})

    svc = policy["service"]
    dos = _d(svc["service_date"])
    age = policy["member_age"]          # computed server-side; no date of birth is available to agents
    mem_id = ev(f"member:{profile['member_id']}",
                f"Member {profile['member_id']} age {age} on service date, state {profile['state_code']} "
                f"(synthetic={profile['is_synthetic']})")
    svc_id = ev("service:requested", f"CPT {svc['cpt_code']} dx {svc['icd10_code']} units {svc['units']} "
                                     f"on {svc['service_date']} in {svc['service_state']}")

    for note in policy.get("retrieval_notes", []):
        issue("CONFLICT" if "not on the member's profile" in note else "MISSING", "RETRIEVAL", note, [mem_id])

    # ---- request status -------------------------------------------------
    ar = policy.get("auth_request")
    if ar:
        ar_id = ev(f"auth_request:{ar['request_number']}",
                   f"Request {ar['request_number']} status {ar['status']}, referral_on_file={ar['referral_on_file']}")
        if ar["status"] != "PENDING":
            issue("CONFLICT", "ALREADY_DECIDED", f"Request is already {ar['status']}; a new determination would conflict",
                  [ar_id])

    # ---- enrollment / eligibility ------------------------------------------
    enr = policy.get("enrollment")
    if enr is None:
        check("ELIGIBILITY", "FAIL", "No enrollment found for the member", [mem_id], True)
        return _result(evidence, checks, issues)
    plan = enr["plan"]
    enr_id = ev(f"enrollment:{enr['enrollment_id']}",
                f"Enrollment {enr['enrollment_id']} status {enr['status']} coverage {enr['coverage_start']} to "
                f"{enr['coverage_end'] or 'open'}")
    plan_id = ev(f"plan:{plan['plan_id']}",
                 f"Plan {plan['plan_code']} ({plan['product_name']}, {plan['insurance_category']}/{plan['network_model']}) "
                 f"state {plan['state_code']} active={plan['is_active']} terminates {plan['termination_date'] or 'never'}")

    overlapping = [e for e in profile["enrollments"]
                   if e["status"] == "ACTIVE" and _d(e["coverage_start"]) <= dos
                   and (e["coverage_end"] is None or dos <= _d(e["coverage_end"]))]
    if len(overlapping) > 1:
        ids = [ev(f"enrollment:{e['enrollment_id']}", f"Enrollment {e['enrollment_id']} ACTIVE on plan {e['plan']['plan_code']}")
               for e in overlapping]
        issue("CONFLICT", "MULTIPLE_ACTIVE_ENROLLMENTS",
              f"{len(overlapping)} active enrollments cover the service date; primary payer (COB) is undetermined", ids)

    start, end = _d(enr["coverage_start"]), _d(enr["coverage_end"])
    if enr["status"] == "PENDING_VERIFICATION":
        check("ELIGIBILITY", "UNKNOWN", "Eligibility pending verification", [enr_id], False)
    elif dos < start:
        check("ELIGIBILITY", "FAIL", f"Service date {dos} precedes coverage start {start}", [enr_id, svc_id], True)
    elif (end and dos > end) or enr["status"] == "TERMINATED":
        check("ELIGIBILITY", "FAIL", f"Coverage ended {end}; service date {dos}", [enr_id, svc_id], True)
    elif plan["termination_date"] and dos > _d(plan["termination_date"]):
        check("ELIGIBILITY", "FAIL", f"Plan terminated {plan['termination_date']}", [plan_id, svc_id], True)
    else:
        check("ELIGIBILITY", "PASS", f"Enrollment active on {dos}", [enr_id, plan_id], True)

    if profile["state_code"] != plan["state_code"]:
        issue("WARNING", "STATE_MISMATCH", f"Member state {profile['state_code']} differs from plan state {plan['state_code']}",
              [mem_id, plan_id])

    # ---- provider ------------------------------------------------------------
    prov = policy.get("provider")
    if prov is None:
        for name in ("PROVIDER_EXCLUSION", "PROVIDER_LICENSE", "NETWORK", "REFERRAL"):
            check(name, "NOT_APPLICABLE", "No rendering provider on a coverage inquiry", [svc_id], True)
    else:
        prov_id = ev(f"provider:{prov['provider_id']}",
                     f"{prov['display_name']} {prov['credential']} {prov['specialty']} ({prov['specialty_group']}), "
                     f"OIG excluded={prov['oig_excluded']}")
        check("PROVIDER_EXCLUSION", "FAIL" if prov["oig_excluded"] else "PASS",
              "Provider is on the exclusion list" if prov["oig_excluded"] else "Provider not excluded", [prov_id], True)

        lic = prov["license_in_state"]
        if lic is None:
            check("PROVIDER_LICENSE", "FAIL", f"No license in service state {svc['service_state']}", [prov_id], True)
        else:
            lic_id = ev(f"license:{lic['license_id']}",
                        f"License {lic['license_type']} in {lic['state_code']} status {lic['status']} expires "
                        f"{lic['expiry_date'] or 'UNKNOWN'}")
            if lic["status"] in ("SUSPENDED", "REVOKED"):
                check("PROVIDER_LICENSE", "FAIL", f"License {lic['status']}", [lic_id], True)
            elif lic["expiry_date"] is None:
                check("PROVIDER_LICENSE", "UNKNOWN", "License expiry date not on file", [lic_id], False)
                issue("MISSING", "LICENSE_EXPIRY", "Provider license expiry date is missing; credentialing must verify", [lic_id])
            elif _d(lic["expiry_date"]) < dos or lic["status"] == "EXPIRED":
                check("PROVIDER_LICENSE", "FAIL", f"License not valid on {dos} (expires {lic['expiry_date']}, "
                                                  f"status {lic['status']})", [lic_id, svc_id], True)
            else:
                check("PROVIDER_LICENSE", "PASS", f"License {lic['status']} through {lic['expiry_date']}", [lic_id], True)

        net = prov["network_for_plan"]
        in_net = bool(net and net["network_status"] == "IN_NETWORK" and _d(net["effective_date"]) <= dos
                      and (net["end_date"] is None or dos <= _d(net["end_date"])))
        net_ids = [prov_id, plan_id] + ([ev(f"network:{net['participation_id']}",
                                            f"Participation {net['network_status']} {net['effective_date']} to "
                                            f"{net['end_date'] or 'open'}")] if net else [])
        if in_net:
            check("NETWORK", "PASS", "Provider in network on service date", net_ids, True)
        elif plan["network_model"] in NETWORK_RESTRICTED and not plan["oon_coverage"]:
            check("NETWORK", "FAIL", f"Provider out of network for {plan['network_model']} plan without OON benefit",
                  net_ids, True)
        else:
            check("NETWORK", "PASS", f"Out of network, but {plan['network_model']} plan has OON coverage", net_ids, True)

        if plan["requires_referral"] and prov["specialty_group"] != "Primary Care":
            has_ref = bool(ar and (ar["referral_on_file"] or any(d["document_type"] == "REFERRAL" for d in ar["documents"])))
            check("REFERRAL", "PASS" if has_ref else "FAIL",
                  "PCP referral on file" if has_ref else "Plan requires a PCP referral for specialist services; none on file",
                  [plan_id, prov_id] + ([f"auth_request:{ar['request_number']}"] if ar else []), True)
            if enr["pcp_provider_id"] is None:
                issue("MISSING", "PCP", "Referral-based plan but no PCP assigned on the enrollment", [enr_id])
        else:
            check("REFERRAL", "NOT_APPLICABLE", "No referral requirement", [plan_id], True)

    # ---- policy rules --------------------------------------------------------
    pr = policy.get("policy_rules")
    if pr is None:
        check("COVERAGE", "UNKNOWN", "No policy rules found for this service", [svc_id], False)
        return _result(evidence, checks, issues)
    proc = pr["procedure"]
    proc_id = ev(f"procedure:{proc['cpt_code']}", f"{proc['cpt_code']} {proc['description']}; default PA "
                                                 f"{proc['requires_prior_auth']}, default doc {proc['default_required_doc']}")
    rules = pr["coverage_rules"]
    rule_ids = [ev(f"coverage_rule:{r['coverage_rule_id']}",
                   f"Rule {r['coverage_rule_id']} ({r['citation']}): covered={r['is_covered']} PA={r['requires_prior_auth']} "
                   f"doc={r['required_document_type']} age {r['min_age']}-{r['max_age']} max_units={r['max_units_per_year']} "
                   f"effective {r['effective_date']} to {r['end_date'] or 'open'}") for r in rules]
    if len(rules) > 1:
        terms = {(r["is_covered"], r["requires_prior_auth"], r["required_document_type"], r["min_age"], r["max_age"],
                  r["max_units_per_year"]) for r in rules}
        if len(terms) > 1:
            issue("CONFLICT", "OVERLAPPING_POLICY_RULES",
                  f"{len(rules)} coverage rules are effective on {dos} with different terms", rule_ids)
    if rules:
        rule, rule_ref = rules[-1], rule_ids[-1]       # most recent effective rule
        covered, pa, req_doc = rule["is_covered"], rule["requires_prior_auth"], rule["required_document_type"]
        min_age, max_age, max_units = rule["min_age"], rule["max_age"], rule["max_units_per_year"]
    else:
        issue("WARNING", "NO_COVERAGE_RULE", "No state coverage rule; procedure defaults applied", [proc_id])
        rule_ref = proc_id
        covered, pa, req_doc = True, proc["requires_prior_auth"], proc["default_required_doc"]
        min_age = max_age = max_units = None

    check("COVERAGE", "PASS" if covered else "FAIL", "Service covered" if covered else "Service excluded under policy",
          [rule_ref], True)

    if min_age is None and max_age is None:
        check("AGE", "NOT_APPLICABLE", "No age restriction", [rule_ref], True)
    elif (min_age is not None and age < min_age) or (max_age is not None and age > max_age):
        check("AGE", "FAIL", f"Member age {age} outside covered range {min_age}-{max_age}", [rule_ref, mem_id], True)
    else:
        check("AGE", "PASS", f"Member age {age} within {min_age}-{max_age}", [rule_ref, mem_id], True)

    if not pa:
        check("DOCUMENTATION", "NOT_APPLICABLE", "Prior authorization not required for this service", [rule_ref], False)
    elif not req_doc:
        check("DOCUMENTATION", "PASS", "No specific document required", [rule_ref], False)
    elif ar is None:
        check("DOCUMENTATION", "UNKNOWN", f"{req_doc} would be required; no submission to inspect", [rule_ref], False)
    else:
        docs = [d for d in ar["documents"] if d["document_type"] == req_doc]
        doc_ids = [ev(f"document:{d['document_id']}", f"{d['document_type']} received {d['received_at']} "
                                                       f"complete={d['is_complete']}") for d in docs]
        if any(d["is_complete"] for d in docs):
            check("DOCUMENTATION", "PASS", f"{req_doc} received and complete", [rule_ref] + doc_ids, False)
        else:
            what = "incomplete" if docs else "not received"
            check("DOCUMENTATION", "UNKNOWN", f"Required {req_doc} {what}", [rule_ref] + doc_ids, False)
            issue("MISSING", "REQUIRED_DOCUMENT", f"Required document {req_doc} is {what}", [rule_ref] + doc_ids)

    crit = pr["clinical_criteria"]
    if not crit:
        check("MEDICAL_NECESSITY", "NOT_APPLICABLE", "No diagnosis criteria defined for this service", [proc_id], False)
    else:
        crit_ids = [ev(f"criterion:{c['criteria_id']}", f"{c['icd10_code']} {c['diagnosis_description']} ({c['criteria_source']})")
                    for c in crit]
        if svc["icd10_code"] in {c["icd10_code"] for c in crit}:
            check("MEDICAL_NECESSITY", "PASS", f"Diagnosis {svc['icd10_code']} meets criteria", crit_ids + [svc_id], False)
        else:
            check("MEDICAL_NECESSITY", "FAIL", f"Diagnosis {svc['icd10_code']} not among criteria diagnoses "
                                               f"{sorted(c['icd10_code'] for c in crit)}", crit_ids + [svc_id], False)

    if max_units is None:
        check("UNITS", "NOT_APPLICABLE", "No unit limit", [rule_ref], False)
    elif svc["units"] > max_units:
        check("UNITS", "FAIL", f"{svc['units']} units requested exceeds annual limit {max_units}", [rule_ref, svc_id], False)
    else:
        check("UNITS", "PASS", f"{svc['units']} units within annual limit {max_units}", [rule_ref, svc_id], False)

    return _result(evidence, checks, issues)


def _result(evidence, checks, issues) -> dict:
    for c in checks:
        evidence[f"check:{c['check']}"] = f"{c['check']} {c['status']}: {c['detail']}"
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE")}
    return {"checks": checks, "data_issues": issues, "evidence": evidence, "summary": counts}
