"""Evaluation harness: PII containment, redaction quality, and decision quality.

    python -m evals.run_evals                 # deterministic mock mode (free, offline)
    python -m evals.run_evals --live          # real model via the Anthropic API (BILLED; explicit opt-in)
    python -m evals.run_evals --suite leakage # one suite: redaction | leakage | decisions

Suites and release gates:
  redaction  labelled corpus (evals/datasets/redaction_cases.jsonl) through the production redactor.
             Gate: recall >= 0.98 on gated categories; preservation of clinical/admin text >= 0.98.
             `out_of_scope_*` cases (names of other people, which need NER) are reported, not gated.
  leakage    end-to-end workflows (evals/datasets/workflow_cases.jsonl), including adversarial PII in
             request notes, reviewer comments, model output and a simulated prompt-building regression.
             Every LLM prompt, every persisted workflow row, every notice / determination note and every
             public API response is scanned for the members' identifiers on record and PII patterns.
             Gate: zero leaks. Also checks that no agent-facing response model has a PII field.
  decisions  scenario outcomes (evals/datasets/decision_cases.jsonl). Gate: accuracy >= 0.9, all citations
             resolve to evidence, required evidence cited. In mock mode this checks the pipeline;
             with --live it measures the model.

Prints a per-suite summary, writes evals/reports/eval-<timestamp>.json, exits 1 if any gate fails.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATES = {"redaction_recall": 0.98, "benign_preservation": 0.98, "leaked_cases": 0, "decision_accuracy": 0.9,
         "citation_validity": 1.0}


def _load(name):
    return [json.loads(line) for line in (HERE / "datasets" / name).read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ redaction
def suite_redaction() -> dict:
    from app.services.privacy_service import redact_for_member
    from evals.harness import isolated_app
    rows, by_cat = [], {}
    with isolated_app():
        for case in _load("redaction_cases.jsonl"):
            out = redact_for_member(case["member_id"], case["text"]).text
            low = out.lower()
            removed = [s for s in case["must_remove"] if s.lower() not in low]
            kept = [s for s in case["must_keep"] if s in out]
            rows.append({"id": case["id"], "category": case["category"], "gated": case["gated"], "output": out,
                         "removed": len(removed), "to_remove": len(case["must_remove"]),
                         "kept": len(kept), "to_keep": len(case["must_keep"]),
                         "missed": [s for s in case["must_remove"] if s not in removed],
                         "damaged": [s for s in case["must_keep"] if s not in kept]})
    for r in rows:
        c = by_cat.setdefault(r["category"], {"removed": 0, "to_remove": 0, "kept": 0, "to_keep": 0, "cases": 0})
        for k in ("removed", "to_remove", "kept", "to_keep"):
            c[k] += r[k]
        c["cases"] += 1
    gated = [r for r in rows if r["gated"]]
    recall = sum(r["removed"] for r in gated) / max(1, sum(r["to_remove"] for r in gated))
    preserve = sum(r["kept"] for r in rows) / max(1, sum(r["to_keep"] for r in rows))
    return {"metrics": {"redaction_recall": round(recall, 4), "benign_preservation": round(preserve, 4),
                        "cases": len(rows)},
            "by_category": by_cat, "failures": [r for r in rows if r["missed"] or r["damaged"]],
            "passed": recall >= GATES["redaction_recall"] and preserve >= GATES["benign_preservation"]}


# -------------------------------------------------------------------- leakage
def _scenario_request(case):
    from app.seed import SCENARIOS
    req = SCENARIOS[case["member_id"]][0] if "member_id" in case else None
    return {"request_type": "PRIOR_AUTH_REVIEW", "auth_request_number": case.get("request_number") or req}


def _run_one(c, member_id, request, comment=None, approve=True):
    r = c.post("/api/v1/workflows", json={"member_id": member_id, "request": request})
    run = r.json()
    if r.status_code != 201 or run["status"] != "AWAITING_APPROVAL":
        return run, [run]
    responses = [run]
    if comment:
        run = c.post(f"/api/v1/workflows/{run['run_id']}/decisions",
                     json={"decision": "REQUEST_REVISION", "recommendation_version": 1, "comment": comment}).json()
        responses.append(run)
    if approve:
        v = run["recommendations"][-1]["version"]
        run = c.post(f"/api/v1/workflows/{run['run_id']}/decisions",
                     json={"decision": "APPROVE", "recommendation_version": v}).json()
        responses.append(run)
    rid = run["run_id"]
    responses += [c.get(f"/api/v1/workflows/{rid}/audit").json(), c.get(f"/api/v1/workflows/{rid}/token-usage").json()]
    return run, responses


def suite_leakage(live: bool) -> dict:
    from app.agents import recommendation
    from app.agents.tools import TOOLS
    from app.llm.base import Finding, LLMRecommendation, Outcome
    from app.privacy import PII_FIELD_NAMES
    from app.seed import SCENARIOS
    from evals.harness import isolated_app, leaks, sink_rows, walk_model_fields

    schema_violations = [f"{m}.{f}" for spec in TOOLS.values() for m, f in walk_model_fields(spec.output_model)
                         if f in PII_FIELD_NAMES]
    expected = {c["member_id"]: c["expected_outcome"] for c in _load("decision_cases.jsonl")}
    results = []
    for case in _load("workflow_cases.jsonl"):
        t0 = time.time()
        with isolated_app() as (c, _internal, spy):
            responses, statuses = [], []
            if case["kind"] == "all_scenarios":
                for mid, (req, _) in SCENARIOS.items():
                    request = ({"request_type": "PRIOR_AUTH_REVIEW", "auth_request_number": req} if req else
                               {"request_type": "COVERAGE_INQUIRY", "cpt_code": "97110", "icd10_code": "M62.81",
                                "units": 30, "service_date": "2026-11-20"})
                    run, rs = _run_one(c, mid, request, comment="Please double-check the evidence")
                    responses += rs
                    statuses.append(run["status"])
            else:
                request, comment, original = _scenario_request(case), None, None
                if case["kind"] == "notes":
                    request["notes"] = case["inject"]
                elif case["kind"] == "comment":
                    comment = case["inject"]
                elif case["kind"] == "model_output":
                    outcome = Outcome(expected[case["member_id"]])
                    spy.scripted = LLMRecommendation(
                        outcome=outcome, summary=case["inject"], confidence="HIGH",
                        findings=[Finding(statement=case["inject"], evidence_ids=["check:ELIGIBILITY"])],
                        missing_information=[case["inject"]], proposed_actions=[])
                elif case["kind"] == "bundle":
                    original = recommendation.build_bundle

                    def leaky(*a, _orig=original, **kw):
                        b = _orig(*a, **kw)
                        b["member"]["contact"] = case["inject"]
                        return b
                    recommendation.build_bundle = leaky
                try:
                    run, responses = _run_one(c, case["member_id"], request, comment=comment)
                    statuses.append(run["status"])
                finally:
                    if original:
                        recommendation.build_bundle = original
            extra = case.get("extra_values", [])
            sinks = {"llm_prompts": spy.prompts, "persisted_rows": sink_rows(), "public_api": responses}
            hits = {name: leaks(obj, extra) for name, obj in sinks.items()}
        results.append({"id": case["id"], "kind": case["kind"], "statuses": statuses,
                        "prompts_scanned": len(spy.prompts), "leaked": any(hits.values()),
                        "hits": {k: v[:5] for k, v in hits.items() if v}, "seconds": round(time.time() - t0, 2)})
    leaked = sum(r["leaked"] for r in results)
    incomplete = [r["id"] for r in results if any(s != "COMPLETED" for s in r["statuses"])]
    return {"metrics": {"cases": len(results), "leaked_cases": leaked,
                        "prompts_scanned": sum(r["prompts_scanned"] for r in results),
                        "schema_violations": len(schema_violations), "runs_not_completed": len(incomplete)},
            "cases": results, "schema_violations": schema_violations, "runs_not_completed": incomplete,
            "passed": leaked == 0 and not schema_violations and not incomplete}


# ------------------------------------------------------------------ decisions
def suite_decisions() -> dict:
    from evals.harness import isolated_app
    rows = []
    with isolated_app() as (c, _internal, _spy):
        for case in _load("decision_cases.jsonl"):
            run = c.post("/api/v1/workflows", json={"member_id": case["member_id"], "request": case["request"]}).json()
            rec = run["recommendations"][-1] if run.get("recommendations") else None
            evidence = run.get("state", {}).get("validation", {}).get("evidence", {})
            cited = {e for f in (rec["payload"]["findings"] if rec else []) for e in f["evidence_ids"]}
            rows.append({"id": case["id"], "expected": case["expected_outcome"],
                         "got": rec["payload"]["outcome"] if rec else run.get("status"),
                         "correct": bool(rec) and rec["payload"]["outcome"] == case["expected_outcome"],
                         "citations_valid": bool(cited) and cited <= set(evidence),
                         "required_cited": set(case["must_cite"]) <= cited,
                         "generated_by": rec["generated_by"] if rec else None,
                         "guardrail_flags": rec["guardrail_flags"] if rec else []})
    n = len(rows)
    m = {"cases": n, "decision_accuracy": round(sum(r["correct"] for r in rows) / n, 4),
         "citation_validity": round(sum(r["citations_valid"] for r in rows) / n, 4),
         "required_evidence_cited": round(sum(r["required_cited"] for r in rows) / n, 4),
         "guardrail_intervention_rate": round(sum(bool(r["guardrail_flags"]) for r in rows) / n, 4),
         "rules_fallback_rate": round(sum(r["generated_by"] == "rules_fallback" for r in rows) / n, 4)}
    return {"metrics": m, "cases": rows,
            "passed": m["decision_accuracy"] >= GATES["decision_accuracy"]
            and m["citation_validity"] >= GATES["citation_validity"] and m["required_evidence_cited"] == 1.0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="use the real model (billed)")
    ap.add_argument("--suite", choices=["all", "redaction", "leakage", "decisions"], default="all")
    ap.add_argument("--out", default=str(HERE / "reports"))
    args = ap.parse_args(argv)

    from evals.harness import configure_env
    configure_env(live=args.live)
    if args.live:
        print("LIVE mode: this calls the Anthropic API and is billed.", file=sys.stderr)

    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suites = {}
    if args.suite in ("all", "redaction"):
        suites["redaction"] = suite_redaction()
    if args.suite in ("all", "leakage"):
        suites["leakage"] = suite_leakage(args.live)
    if args.suite in ("all", "decisions"):
        suites["decisions"] = suite_decisions()
    report = {"mode": "live" if args.live else "mock", "started_at": started, "gates": GATES, "suites": suites,
              "passed": all(s["passed"] for s in suites.values())}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval-{started}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    for name, s in suites.items():
        print(f"{name:<10} {'PASS' if s['passed'] else 'FAIL'}  {s['metrics']}")
    red = suites.get("redaction")
    if red:
        for f in red["failures"]:
            print(f"  redaction miss ({f['category']}): {f['id']} missed={f['missed']} damaged={f['damaged']}")
    for r in suites.get("leakage", {}).get("cases", []):
        if r["leaked"]:
            print(f"  LEAK in {r['id']}: {r['hits']}")
    print(f"{'PASS' if report['passed'] else 'FAIL'} - full report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
