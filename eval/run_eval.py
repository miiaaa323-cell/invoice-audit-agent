"""Evaluate complete PDF workflows in an isolated database.

Synthetic replay fixtures exercise orchestration and rules. They do not measure a
live model's extraction ability; cache provenance is included in every report.
"""

import argparse
import json
import math
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import ROOT, Settings, load_policy
from app.graph.builder import build_graph
from app.repo.repositories import Repository
from app.services.explanation import ExplanationService
from app.services.extraction import ExtractionService
from app.services.llm_client import LLMClient
from app.services.money import normalize_text

DECIMAL_FIELDS = {
    "quantity",
    "unit_price_excl_tax",
    "amount_excl_tax",
    "tax_rate",
    "tax_amount",
    "total_excl_tax",
    "total_tax",
    "total_incl_tax",
}
RULE_CODES = (
    "MISSING_FIELD",
    "LOW_CONFIDENCE",
    "ARITHMETIC_ERROR",
    "DUPLICATE_INVOICE",
    "BUYER_MISMATCH",
    "VENDOR_NOT_APPROVED",
    "VENDOR_PO_MISMATCH",
    "PO_NOT_FOUND",
    "GR_NOT_FOUND",
    "GR_ALREADY_INVOICED",
    "LINE_NOT_ON_PO",
    "QTY_MISMATCH",
    "PRICE_MISMATCH",
    "TAX_RATE_MISMATCH",
    "AMOUNT_OVER_TOLERANCE",
)


def flatten_fields(value: Any, prefix: str = "") -> dict[str, Any]:
    """Represent invoice leaves by the same paths used for field corrections."""
    if isinstance(value, dict):
        return {
            path: leaf
            for key, item in value.items()
            for path, leaf in flatten_fields(item, f"{prefix}.{key}".strip(".")).items()
        }
    if isinstance(value, list) and value:
        return {
            path: leaf
            for index, item in enumerate(value)
            for path, leaf in flatten_fields(item, f"{prefix}.{index}").items()
        }
    return {prefix: value}


def fields_equal(path: str, actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is expected
    if path.rsplit(".", 1)[-1] in DECIMAL_FIELDS:
        try:
            actual_decimal, expected_decimal = Decimal(str(actual)), Decimal(str(expected))
            return (
                actual_decimal.is_finite()
                and expected_decimal.is_finite()
                and (actual_decimal == expected_decimal)
            )
        except (InvalidOperation, ValueError):
            return False
    if isinstance(actual, str) and isinstance(expected, str):
        return normalize_text(actual) == normalize_text(expected)
    return actual == expected


def extraction_score(actual: dict | None, truth: dict) -> tuple[int, int]:
    actual_fields = flatten_fields(actual or {})
    expected_fields = flatten_fields(truth)
    paths = set(actual_fields) | set(expected_fields)
    matched = sum(
        path in actual_fields
        and path in expected_fields
        and fields_equal(path, actual_fields[path], expected_fields[path])
        for path in paths
    )
    return matched, len(paths)


class EvaluationLLMClient(LLMClient):
    """Track sources actually consumed, including mixed recorded/synthetic caches."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sources: Counter = Counter()

    def complete(self, task, payload, schema):
        response = super().complete(task, payload, schema)
        source = "live"
        if self.mode == "replay":
            request_hash = self.request_hash(self.request(task, payload, schema))
            entry = json.loads(
                (self.cache_dir / f"{request_hash}.json").read_text(encoding="utf-8")
            )
            source = entry.get("source", "unknown")
        elif self.mode == "record":
            source = "recorded"
        self.sources[source] += 1
        return response


@contextmanager
def evaluation_service(settings: Settings, runtime_dir: Path):
    from app.services.audit import AuditService

    isolated = settings.model_copy(
        update={
            "data_dir": runtime_dir,
            "db_path": runtime_dir / "app.sqlite",
            "checkpoint_path": runtime_dir / "checkpoints.sqlite",
            "upload_dir": runtime_dir / "uploads",
        }
    )
    policy = load_policy(isolated.policy_path)
    repo = Repository(isolated.db_path)
    repo.seed(isolated.seed_dir)
    client = EvaluationLLMClient(
        mode=isolated.llm_mode,
        cache_dir=isolated.cache_dir,
        model=isolated.llm_model,
        api_key=isolated.anthropic_api_key,
    )
    try:
        with SqliteSaver.from_conn_string(str(isolated.checkpoint_path)) as checkpointer:
            graph = build_graph(
                checkpointer, repo, ExtractionService(client), ExplanationService(client), policy
            )
            yield AuditService(graph, repo, isolated, policy), repo, client
    finally:
        repo.db.close()


def _first_route(result: dict) -> str | None:
    if result.get("route"):
        return result["route"]
    pending = result.get("pending") or {}
    return {
        "field_verify": "FIELD_VERIFY",
        "ap_review": "AP_REVIEW",
        "level1_approval": "LEVEL1_APPROVAL",
    }.get(pending.get("type"))


def _findings(result: dict) -> set[str]:
    return {finding["code"] for finding in result.get("findings", [])}


def _blocking_findings(result: dict) -> set[str]:
    return {
        finding["code"]
        for finding in result.get("findings", [])
        if finding.get("severity") == "blocking"
    }


def evaluate_case(service, repo: Repository, label: dict, data_dir: Path) -> dict:
    """Measure initial extraction, then simulate human actions from label truth."""
    started = perf_counter()
    result = service.submit(data_dir / label["invoice_file"])
    elapsed = perf_counter() - started
    initial_invoice = deepcopy(result.get("invoice"))
    observed_route = _first_route(result)
    observed_codes = _findings(result)
    blocking_codes = _blocking_findings(result)
    truth_fields = flatten_fields(label["truth"])
    for _ in range(10):
        pending = result.get("pending")
        if not pending:
            break
        kind = pending["type"]
        if kind == "field_verify":
            body = {
                "action": "confirm_fields",
                "corrected_fields": {path: truth_fields[path] for path in pending["fields"]},
            }
        elif kind == "ap_review":
            body = {"action": "reject", "note": "评测复核：发票存在需处理的差异。"}
        elif kind in {"level1_approval", "level2_approval"}:
            body = {"action": "approve"}
        else:
            raise RuntimeError(f"Unexpected evaluation interrupt: {kind}")
        body["version"] = result.get("version")
        started = perf_counter()
        result = service.resume(result["run_id"], body, pending["required_role"])
        elapsed += perf_counter() - started
        observed_codes |= _findings(result)
        blocking_codes |= _blocking_findings(result)
    else:
        raise RuntimeError("Evaluation exceeded ten human actions for one invoice")
    matched, field_count = extraction_score(initial_invoice, label["truth"])
    with repo.db.connection() as connection:
        variance_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT invoice_number, amount, account FROM variance_entries WHERE run_id = ?",
                (result["run_id"],),
            ).fetchall()
        ]
    expected_variance = label.get("expected_variance")
    variance_correct = (
        not variance_rows
        if expected_variance is None
        else (
            len(variance_rows) == 1
            and fields_equal("amount_excl_tax", variance_rows[0]["amount"], expected_variance)
            and variance_rows[0]["invoice_number"] == label["truth"]["invoice_number"]
            and variance_rows[0]["account"] == service.policy.variance_account
        )
    )
    return {
        "invoice_file": label["invoice_file"],
        "run_id": result["run_id"],
        "expected_codes": label["expected_codes"],
        "actual_codes": sorted(observed_codes),
        "blocking_codes": sorted(blocking_codes),
        "expected_route": label["expected_route"],
        "actual_route": observed_route,
        "expected_variance": expected_variance,
        "variance_correct": bool(variance_correct),
        "field_matches": matched,
        "field_count": field_count,
        "seconds": elapsed,
        "status": result["status"],
        "error": None,
    }


def summarize(cases: list[dict], *, mode: str, sources: dict) -> dict:
    problems = [case for case in cases if case["expected_codes"]]
    normals = [case for case in cases if not case["expected_codes"]]
    variance_cases = [case for case in cases if case["expected_variance"] is not None]
    per_code = {}
    all_codes = set(RULE_CODES) | {code for case in cases for code in case["actual_codes"]}
    for code in sorted(all_codes):
        expected = sum(code in case["expected_codes"] for case in cases)
        detected = sum(
            code in case["expected_codes"] and code in case["actual_codes"] for case in cases
        )
        per_code[code] = {
            "expected": expected,
            "detected": detected,
            "missed": expected - detected,
            "unexpected": sum(
                code not in case["expected_codes"] and code in case["actual_codes"]
                for case in cases
            ),
        }
    return {
        "mode": mode,
        "sources": dict(sources),
        "sample_count": len(cases),
        "problem_count": len(problems),
        "normal_count": len(normals),
        "problems_detected": sum(
            set(case["expected_codes"]) <= set(case["actual_codes"]) for case in problems
        ),
        "false_positives": sum(
            bool(case.get("blocking_codes", case["actual_codes"])) for case in normals
        ),
        "field_matches": sum(case["field_matches"] for case in cases),
        "field_count": sum(case["field_count"] for case in cases),
        "routes_correct": sum(case["actual_route"] == case["expected_route"] for case in cases),
        "variance_count": len(variance_cases),
        "variances_correct": sum(case["variance_correct"] for case in variance_cases),
        "variance_mismatches": sum(not case["variance_correct"] for case in cases),
        "auto_approved": sum(case["actual_route"] == "AUTO_APPROVE" for case in cases),
        "mean_seconds": sum(case["seconds"] for case in cases) / len(cases) if cases else 0,
        "outcome_mismatches": sum(
            case["status"] != ("REJECTED" if case["expected_codes"] else "APPROVED")
            for case in cases
        ),
        "errors": [case for case in cases if case.get("error")],
        "per_code": per_code,
        "cases": cases,
    }


def quality_gate_failures(metrics: dict) -> list[str]:
    failures = []
    if metrics["sample_count"] == 0:
        failures.append("评测集为空")
    required = math.ceil(metrics["problem_count"] * 0.9)
    if metrics["problems_detected"] < required:
        failures.append(f"问题票检出少于 {required}/{metrics['problem_count']}")
    if metrics["false_positives"] > 8:
        failures.append("正常票误报超过 8 张")
    if metrics["routes_correct"] != metrics["sample_count"]:
        failures.append("存在路由与标签不符")
    if metrics["variance_mismatches"]:
        failures.append("存在尾差台账不符")
    if metrics["outcome_mismatches"]:
        failures.append("存在最终状态与标签不符")
    if metrics["errors"]:
        failures.append("存在处理错误")
    return failures


def _ratio(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator} ({numerator / denominator:.2%})" if denominator else "0/0"


def render_report(metrics: dict) -> str:
    sources = ", ".join(
        f"{source}: {count}" for source, count in sorted(metrics["sources"].items())
    )
    lines = [
        "# 发票审核评测报告",
        "",
        f"运行模式：`{metrics['mode']}`；样本：{metrics['sample_count']} 张。",
        f"实际响应来源：{sources or '无成功的 LLM 响应'}。",
        "",
    ]
    if metrics["sources"].get("synthetic"):
        lines += [
            "本次使用了由合成发票真值构造的 replay 夹具。抽取准确率反映夹具与真值的一致性，",
            "不能视为 Claude 的真实抽取能力，也不能用作 live 实测成绩。规则、PDF 读取、路由、",
            "人工恢复、数据库入账和尾差台账均通过完整流程运行。",
            "",
        ]
    elif metrics["mode"] == "replay":
        lines += ["本次重放已记录的模型响应；响应来源见上方，未在本次执行中调用实时模型。", ""]
    else:
        lines += ["本次调用实时模型；抽取指标使用人工修正前的首次响应。", ""]
    lines += [
        "| 指标 | 结果 |",
        "| --- | --- |",
        f"| 问题票检出 | {_ratio(metrics['problems_detected'], metrics['problem_count'])} |",
        f"| 正常票 blocking 误报 | {metrics['false_positives']}/{metrics['normal_count']} |",
        f"| 首次字段抽取准确率 | {_ratio(metrics['field_matches'], metrics['field_count'])} |",
        f"| 首次路由准确率 | {_ratio(metrics['routes_correct'], metrics['sample_count'])} |",
        f"| 尾差记账正确数 | {_ratio(metrics['variances_correct'], metrics['variance_count'])} |",
        f"| 直通率 | {_ratio(metrics['auto_approved'], metrics['sample_count'])} |",
        f"| 平均处理耗时（不含人工等待） | {metrics['mean_seconds'] * 1000:.2f} ms |",
        f"| 处理错误 | {len(metrics['errors'])} |",
        "",
        "问题票检出要求该票标签中的全部异常码均被检出；误报只统计正常票的 blocking 异常。",
        "字段按头字段和所有明细叶子字段计数，金额使用 Decimal 比较，文本使用 NFKC 与去首尾空格比较。",
        "路由取首次待办或直通路线，包含 FIELD_VERIFY。耗时累计每次提交与恢复调用，不含人工思考或等待。",
        "",
        "| 异常码 | 应检出 | 检出 | 漏报 | 标签外检出 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for code, counts in metrics["per_code"].items():
        lines.append(
            f"| {code} | {counts['expected']} | {counts['detected']} | "
            f"{counts['missed']} | {counts['unexpected']} |"
        )
    failures = quality_gate_failures(metrics)
    lines += ["", f"CI 门禁：{'未通过（' + '；'.join(failures) + '）' if failures else '通过'}。"]
    if metrics["errors"]:
        lines += ["", "处理错误：", ""]
        lines.extend(f"- `{case['invoice_file']}`：{case['error']}" for case in metrics["errors"])
    return "\n".join(lines) + "\n"


def run_evaluation(
    data_dir: Path | str = ROOT / "data",
    cache_dir: Path | str = ROOT / "fixtures/llm_cache",
    mode: str = "replay",
    report_path: Path | str | None = None,
    settings: Settings | None = None,
) -> dict:
    data_dir = Path(data_dir)
    labels = json.loads((data_dir / "labels.json").read_text(encoding="utf-8"))
    labels = sorted(labels, key=lambda label: label["invoice_file"])
    settings = settings or Settings.from_env()
    settings = settings.model_copy(
        update={"llm_mode": mode, "cache_dir": Path(cache_dir), "seed_dir": data_dir / "seed"}
    )
    cases = []
    with TemporaryDirectory(prefix="invoice-audit-eval-") as directory:
        with evaluation_service(settings, Path(directory)) as (service, repo, client):
            for label in labels:
                started = perf_counter()
                try:
                    case = evaluate_case(service, repo, label, data_dir)
                except Exception as exc:
                    _, field_count = extraction_score(None, label["truth"])
                    case = {
                        "invoice_file": label["invoice_file"],
                        "run_id": None,
                        "expected_codes": label["expected_codes"],
                        "actual_codes": [],
                        "blocking_codes": [],
                        "expected_route": label["expected_route"],
                        "actual_route": None,
                        "expected_variance": label.get("expected_variance"),
                        "variance_correct": False,
                        "field_matches": 0,
                        "field_count": field_count,
                        "seconds": perf_counter() - started,
                        "status": "ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                cases.append(case)
            metrics = summarize(cases, mode=mode, sources=client.sources)
    if report_path is not None:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_report(metrics), encoding="utf-8")
    return metrics


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("replay", "record", "live"), default="replay")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "fixtures/llm_cache")
    parser.add_argument("--report", type=Path, default=ROOT / "eval/report.md")
    args = parser.parse_args(argv)
    metrics = run_evaluation(args.data_dir, args.cache_dir, args.mode, args.report)
    print(render_report(metrics), end="")
    return int(bool(quality_gate_failures(metrics)))


if __name__ == "__main__":
    raise SystemExit(main())
