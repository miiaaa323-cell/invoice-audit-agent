from copy import deepcopy
from decimal import Decimal

import pytest

from app.config import Policy
from app.repo.repositories import Repository
from eval.run_eval import (
    evaluate_case,
    extraction_score,
    fields_equal,
    flatten_fields,
    quality_gate_failures,
    render_report,
    summarize,
)


def make_case(**updates):
    case = {
        "invoice_file": "invoices/example.pdf",
        "run_id": "run-1",
        "expected_codes": [],
        "actual_codes": [],
        "expected_route": "AUTO_APPROVE",
        "actual_route": "AUTO_APPROVE",
        "expected_variance": None,
        "variance_correct": True,
        "field_matches": 10,
        "field_count": 10,
        "seconds": 0.1,
        "status": "APPROVED",
        "error": None,
    }
    case.update(updates)
    return case


def problem_case(*, detected=True):
    return make_case(
        expected_codes=["PRICE_MISMATCH", "AMOUNT_OVER_TOLERANCE"],
        actual_codes=["PRICE_MISMATCH", "AMOUNT_OVER_TOLERANCE"] if detected else [],
        expected_route="AP_REVIEW",
        actual_route="AP_REVIEW",
        status="REJECTED",
    )


@pytest.mark.parametrize(
    "path,actual,truth,equal",
    [
        ("total_tax", "1.0", "1.00", True),
        ("lines.0.unit_price_excl_tax", ".0001", "0.0002", False),
        ("lines.0.quantity", Decimal("2"), "2.0", True),
        ("buyer_name", "　ＡＣＭＥ　", "ACME", True),
        ("total_tax", "NaN", "0", False),
        ("total_tax", None, "0", False),
        ("invoice_number", None, None, True),
    ],
)
def test_metric_value_comparison(path, actual, truth, equal):
    assert fields_equal(path, actual, truth) is equal


def test_flatten_and_score_count_all_invoice_leaves():
    truth = {
        "invoice_number": "INV-1",
        "total_tax": "1.00",
        "lines": [{"sku": "A", "unit_price_excl_tax": "1.1000"}, {"sku": "B"}],
    }
    actual = deepcopy(truth)
    actual["lines"][0]["unit_price_excl_tax"] = "1.1"
    assert flatten_fields(truth)["lines.1.sku"] == "B"
    assert extraction_score(actual, truth) == (5, 5)
    actual["lines"].pop()
    actual["unexpected"] = "extra"
    assert extraction_score(actual, truth) == (4, 6)


def test_problem_detection_requires_primary_and_derived_expected_codes():
    case = problem_case()
    case["actual_codes"] = ["AMOUNT_OVER_TOLERANCE"]
    metrics = summarize([case], mode="replay", sources={"synthetic": 1})
    assert metrics["problems_detected"] == 0
    assert metrics["per_code"]["PRICE_MISMATCH"]["missed"] == 1
    assert metrics["per_code"]["AMOUNT_OVER_TOLERANCE"]["detected"] == 1


def test_review_findings_are_counted_per_code_without_a_blocking_false_positive():
    case = make_case(actual_codes=["LOW_CONFIDENCE"], blocking_codes=[])
    metrics = summarize([case], mode="replay", sources={"synthetic": 1})
    assert metrics["false_positives"] == 0
    assert metrics["per_code"]["LOW_CONFIDENCE"]["unexpected"] == 1


def test_summary_includes_routes_variance_straight_through_and_mean_time():
    cases = [
        make_case(seconds=0.1),
        make_case(
            expected_route="LEVEL1_APPROVAL",
            actual_route="LEVEL1_APPROVAL",
            seconds=0.3,
            expected_variance="-.02",
        ),
        problem_case(),
    ]
    metrics = summarize(cases, mode="replay", sources={"synthetic": 4})
    assert metrics["problems_detected"] == 1
    assert metrics["false_positives"] == 0
    assert metrics["field_matches"] == metrics["field_count"] == 30
    assert metrics["routes_correct"] == 3
    assert metrics["variances_correct"] == metrics["variance_count"] == 1
    assert metrics["auto_approved"] == 1
    assert metrics["mean_seconds"] == pytest.approx(1 / 6)
    assert quality_gate_failures(metrics) == []


@pytest.mark.parametrize("detected,pass_gate", [(35, False), (36, True), (40, True)])
def test_ci_detection_threshold_36_of_40(detected, pass_gate):
    cases = [problem_case(detected=index < detected) for index in range(40)]
    metrics = summarize(cases, mode="replay", sources={})
    assert bool(quality_gate_failures(metrics)) is not pass_gate


@pytest.mark.parametrize("false_positives,pass_gate", [(8, True), (9, False)])
def test_ci_false_positive_threshold(false_positives, pass_gate):
    cases = [make_case(actual_codes=["BUYER_MISMATCH"]) for _ in range(false_positives)]
    metrics = summarize(cases, mode="replay", sources={})
    assert bool(quality_gate_failures(metrics)) is not pass_gate


def test_report_identifies_synthetic_fixtures_and_every_requested_metric():
    metrics = summarize([make_case()], mode="replay", sources={"synthetic": 1})
    report = render_report(metrics)
    assert "不能视为 Claude 的真实抽取能力" in report
    for text in (
        "问题票检出",
        "误报",
        "首次字段抽取准确率",
        "路由准确率",
        "尾差记账正确数",
        "直通率",
        "平均处理耗时",
        "PRICE_MISMATCH",
        "漏报",
    ):
        assert text in report
    assert "CI 门禁：通过" in report


def test_case_uses_initial_extraction_before_human_truth_correction(tmp_path):
    truth = {"invoice_number": "INV-1", "total_incl_tax": "100.00"}
    label = {
        "invoice_file": "invoice.pdf",
        "truth": truth,
        "expected_codes": [],
        "expected_route": "FIELD_VERIFY",
        "expected_variance": None,
    }

    class Service:
        policy = Policy()

        def submit(self, _):
            self.result = {
                "run_id": "run-1",
                "version": 0,
                "status": "PROCESSING",
                "route": None,
                "invoice": {**truth, "total_incl_tax": "999.00"},
                "findings": [],
                "pending": {
                    "type": "field_verify",
                    "fields": ["total_incl_tax"],
                    "required_role": "ap_clerk",
                },
            }
            return self.result

        def resume(self, run_id, body, role):
            assert body["action"] == "confirm_fields"
            assert body["corrected_fields"] == {"total_incl_tax": "100.00"}
            assert role == "ap_clerk"
            self.result["invoice"]["total_incl_tax"] = "100.00"
            self.result.update(status="APPROVED", pending=None)
            return self.result

    repo = Repository(tmp_path / "eval.sqlite")
    case = evaluate_case(Service(), repo, label, tmp_path)
    assert case["field_matches"] == 1
    assert case["field_count"] == 2
    assert case["actual_route"] == "FIELD_VERIFY"
    assert case["variance_correct"]
