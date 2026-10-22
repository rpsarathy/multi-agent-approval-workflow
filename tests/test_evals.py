"""The eval harness runs offline in mock mode and every release gate passes."""
import json

from evals import run_evals


def test_mock_evals_pass_all_gates(tmp_path):
    assert run_evals.main(["--out", str(tmp_path)]) == 0
    report = json.loads(next(tmp_path.glob("eval-*.json")).read_text())
    assert report["mode"] == "mock" and report["passed"]
    assert report["suites"]["leakage"]["metrics"]["leaked_cases"] == 0
    assert report["suites"]["leakage"]["metrics"]["prompts_scanned"] >= 50
    assert report["suites"]["redaction"]["metrics"]["redaction_recall"] >= 0.98
    # out-of-scope cases are reported honestly as misses, not hidden
    assert report["suites"]["redaction"]["by_category"]["out_of_scope_other_person"]["removed"] == 0
    assert not list(tmp_path.glob("*.md"))     # JSON report only
