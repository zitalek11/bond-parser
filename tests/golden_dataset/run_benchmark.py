#!/usr/bin/env python3
"""Golden Dataset benchmark for Qwen 2.5 bond parsing quality.

Usage (from bond_parser/):
  python -m tests.golden_dataset.run_benchmark
  python -m tests.golden_dataset.run_benchmark --mock          # offline smoke
  python -m tests.golden_dataset.run_benchmark --case floater_cbr_lag7
  python tests/golden_dataset/run_benchmark.py --self-check    # expected vs expected = 100%
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.bond import BondDocumentSchema  # noqa: E402
from app.services.extractor import stitch_broken_markdown_tables  # noqa: E402
from app.services.llm_parser import LLMBondParser, MockLLMBondParser  # noqa: E402
from app.services.validator import BondDataValidator  # noqa: E402

CASES_DIR = Path(__file__).resolve().parent / "cases"

CRITICAL_COMPARE_KEYS = (
    "reg_number",
    "isin",
    "issuer_inn",
    "nominal_value",
    "currency",
    "maturity_date",
    "coupon_type",
    "benchmark",
    "spread_percent",
    "observation_shift_days",
    "averaging_type",
    "cap_rate",
    "floor_rate",
    "coupons_count",
    "amortization_sum",
    "amortization_coupon_numbers",
)


@dataclass
class FieldScore:
    name: str
    expected: Any
    actual: Any
    ok: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    title: str
    elapsed_sec: float
    validator_status: str
    field_scores: list[FieldScore] = field(default_factory=list)
    diffs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def accuracy_pct(self) -> float:
        if not self.field_scores:
            return 0.0
        return 100.0 * sum(1 for f in self.field_scores if f.ok) / len(self.field_scores)


def _load_cases(
    case_filter: str | None = None,
    *,
    cases_dir: Path | None = None,
) -> list[dict[str, Any]]:
    directory = cases_dir or CASES_DIR
    files = sorted(p for p in directory.glob("*.json") if p.name != "manifest.json")
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    if case_filter:
        cases = [c for c in cases if c["id"] == case_filter]
    if not cases:
        raise SystemExit(f"No golden cases found in {directory}")
    return cases


def _as_date(value: Any) -> Any:
    if isinstance(value, str) and len(value) == 10 and value[4] == "-":
        return date.fromisoformat(value)
    return value


def _float_eq(a: Any, b: Any, tol: float = 0.01) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _extract_actual_map(doc: BondDocumentSchema) -> dict[str, Any]:
    formula = doc.floater_formula
    return {
        "reg_number": doc.reg_number,
        "isin": doc.isin,
        "issuer_inn": doc.issuer_inn,
        "nominal_value": doc.nominal_value,
        "currency": doc.currency,
        "maturity_date": doc.maturity_date.isoformat() if doc.maturity_date else None,
        "coupon_type": str(doc.coupon_type),
        "benchmark": formula.benchmark if formula else None,
        "spread_percent": formula.spread_percent if formula else None,
        "observation_shift_days": formula.observation_shift_days if formula else None,
        "averaging_type": formula.averaging_type if formula else None,
        "cap_rate": formula.cap_rate if formula else None,
        "floor_rate": formula.floor_rate if formula else None,
        "coupons_count": len(doc.coupons),
        "amortization_sum": round(sum(a.amortization_percent for a in doc.amortization), 4)
        if doc.amortization
        else 0.0,
        "amortization_coupon_numbers": sorted(
            a.coupon_number for a in doc.amortization if a.coupon_number is not None
        ),
    }


def _extract_expected_map(expected: dict[str, Any]) -> dict[str, Any]:
    formula = expected.get("floater_formula") or {}
    amort = expected.get("amortization") or []
    coupons = expected.get("coupons") or []
    maturity = expected.get("maturity_date")
    if hasattr(maturity, "isoformat"):
        maturity = maturity.isoformat()
    return {
        "reg_number": expected.get("reg_number"),
        "isin": expected.get("isin"),
        "issuer_inn": expected.get("issuer_inn"),
        "nominal_value": expected.get("nominal_value"),
        "currency": expected.get("currency"),
        "maturity_date": maturity,
        "coupon_type": expected.get("coupon_type"),
        "benchmark": formula.get("benchmark") if formula else None,
        "spread_percent": formula.get("spread_percent") if formula else None,
        "observation_shift_days": formula.get("observation_shift_days") if formula else None,
        "averaging_type": formula.get("averaging_type") if formula else None,
        "cap_rate": formula.get("cap_rate") if formula else None,
        "floor_rate": formula.get("floor_rate") if formula else None,
        "coupons_count": len(coupons),
        "amortization_sum": round(
            sum(float(a.get("amortization_percent") or a.get("percent") or 0) for a in amort),
            4,
        )
        if amort
        else 0.0,
        "amortization_coupon_numbers": sorted(
            int(a["coupon_number"]) for a in amort if a.get("coupon_number") is not None
        ),
    }


def compare_critical(
    expected: dict[str, Any],
    actual_doc: BondDocumentSchema,
    critical_fields: list[str] | None = None,
) -> list[FieldScore]:
    exp_map = _extract_expected_map(expected)
    act_map = _extract_actual_map(actual_doc)
    keys = critical_fields or list(CRITICAL_COMPARE_KEYS)
    # Keep only keys that are meaningful for this case (present in expected map / listed)
    scores: list[FieldScore] = []
    for key in keys:
        if key not in exp_map and key not in CRITICAL_COMPARE_KEYS:
            continue
        if key not in exp_map:
            continue
        # Skip floater-only fields for FIXED when expected is null
        if key in {
            "benchmark",
            "spread_percent",
            "observation_shift_days",
            "averaging_type",
            "cap_rate",
            "floor_rate",
        } and exp_map.get("coupon_type") == "FIXED":
            if exp_map.get(key) is None and act_map.get(key) is None:
                continue

        exp_v = exp_map.get(key)
        act_v = act_map.get(key)
        if key in {"nominal_value", "spread_percent", "amortization_sum", "cap_rate", "floor_rate"}:
            ok = _float_eq(exp_v, act_v)
        else:
            ok = exp_v == act_v
        scores.append(
            FieldScore(
                name=key,
                expected=exp_v,
                actual=act_v,
                ok=ok,
                detail="" if ok else f"{exp_v!r} ≠ {act_v!r}",
            )
        )
    return scores


async def run_case(
    case: dict[str, Any],
    *,
    parser: LLMBondParser | MockLLMBondParser,
    validator: BondDataValidator,
    self_check: bool = False,
) -> CaseResult:
    case_id = case["id"]
    title = case.get("title", case_id)
    markdown = case["input_markdown"]
    if case.get("preprocess") == "stitch_tables":
        markdown = stitch_broken_markdown_tables(markdown)

    started = time.perf_counter()
    try:
        if self_check:
            predicted = BondDocumentSchema.model_validate(case["expected"])
        else:
            predicted = await parser.parse(markdown)
        elapsed = time.perf_counter() - started
        result = validator.validate(predicted, source_text=markdown)
        if result.resolved_document is not None:
            predicted = result.resolved_document
        scores = compare_critical(
            case["expected"], predicted, case.get("critical_fields")
        )
        diffs = [f"{s.name}: {s.detail}" for s in scores if not s.ok]
        return CaseResult(
            case_id=case_id,
            title=title,
            elapsed_sec=elapsed,
            validator_status=result.status,
            field_scores=scores,
            diffs=diffs,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        return CaseResult(
            case_id=case_id,
            title=title,
            elapsed_sec=elapsed,
            validator_status="ERROR",
            error=str(exc),
            diffs=[str(exc)],
        )


def _print_report(results: list[CaseResult]) -> float:
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box

        console = Console()
        table = Table(title="Golden Dataset — Qwen 2.5 Bond Parsing", box=box.SIMPLE_HEAVY)
        table.add_column("Case", style="cyan", no_wrap=True)
        table.add_column("Time, s", justify="right")
        table.add_column("Field Acc %", justify="right")
        table.add_column("Validator")
        table.add_column("Diffs")

        total_ok = 0
        total_fields = 0
        for r in results:
            total_ok += sum(1 for f in r.field_scores if f.ok)
            total_fields += len(r.field_scores)
            status_style = (
                "green"
                if r.validator_status == "APPROVED"
                else ("yellow" if r.validator_status == "NEEDS_REVIEW" else "red")
            )
            diffs = "; ".join(r.diffs[:3]) if r.diffs else "—"
            if len(r.diffs) > 3:
                diffs += f" (+{len(r.diffs) - 3})"
            table.add_row(
                r.case_id,
                f"{r.elapsed_sec:.2f}",
                f"{r.accuracy_pct:.1f}",
                f"[{status_style}]{r.validator_status}[/]",
                diffs[:120],
            )

        console.print(table)
        overall = 100.0 * total_ok / total_fields if total_fields else 0.0
        console.print(
            f"\n[bold]Field-Level Accuracy:[/bold] {overall:.2f}%  "
            f"({total_ok}/{total_fields} critical fields)"
        )
        for r in results:
            if r.error:
                console.print(f"[red]ERROR {r.case_id}:[/red] {r.error}")
        return overall
    except ImportError:
        # Fallback without rich
        rows = []
        total_ok = total_fields = 0
        for r in results:
            total_ok += sum(1 for f in r.field_scores if f.ok)
            total_fields += len(r.field_scores)
            rows.append(
                f"{r.case_id:32} {r.elapsed_sec:6.2f}s  {r.accuracy_pct:6.1f}%  "
                f"{r.validator_status:12}  {'; '.join(r.diffs[:2]) or '—'}"
            )
        print("\n".join(rows))
        overall = 100.0 * total_ok / total_fields if total_fields else 0.0
        print(f"\nField-Level Accuracy: {overall:.2f}% ({total_ok}/{total_fields})")
        return overall


def _write_report(
    results: list[CaseResult],
    *,
    overall: float,
    cases: list[dict[str, Any]],
    report_path: Path,
    engine: str,
) -> None:
    total_ok = sum(1 for r in results for f in r.field_scores if f.ok)
    total_fields = sum(len(r.field_scores) for r in results)
    doc_exact = sum(1 for r in results if r.field_scores and all(f.ok for f in r.field_scores) and not r.error)
    by_source: dict[str, dict[str, float | int]] = {}
    for case, result in zip(cases, results):
        src = str(case.get("source") or "unknown")
        bucket = by_source.setdefault(src, {"cases": 0, "ok_fields": 0, "fields": 0, "doc_exact": 0})
        bucket["cases"] = int(bucket["cases"]) + 1
        bucket["ok_fields"] = int(bucket["ok_fields"]) + sum(1 for f in result.field_scores if f.ok)
        bucket["fields"] = int(bucket["fields"]) + len(result.field_scores)
        if result.field_scores and all(f.ok for f in result.field_scores) and not result.error:
            bucket["doc_exact"] = int(bucket["doc_exact"]) + 1
    for src, bucket in by_source.items():
        fields = int(bucket["fields"]) or 1
        cases_n = int(bucket["cases"]) or 1
        bucket["field_accuracy_pct"] = round(100.0 * int(bucket["ok_fields"]) / fields, 2)
        bucket["doc_exact_pct"] = round(100.0 * int(bucket["doc_exact"]) / cases_n, 2)

    payload = {
        "engine": engine,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": len(results),
        "field_accuracy_pct": round(overall, 2),
        "fields_ok": total_ok,
        "fields_total": total_fields,
        "doc_exact_pct": round(100.0 * doc_exact / len(results), 2) if results else 0.0,
        "doc_exact": doc_exact,
        "by_source": by_source,
        "results": [
            {
                "id": r.case_id,
                "accuracy_pct": round(r.accuracy_pct, 2),
                "validator_status": r.validator_status,
                "diffs": r.diffs,
                "error": r.error,
            }
            for r in results
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report → {report_path}")


async def _amain(args: argparse.Namespace) -> int:
    cases_dir = Path(args.cases_dir) if args.cases_dir else CASES_DIR
    if not cases_dir.is_absolute():
        cases_dir = ROOT / cases_dir
    cases = _load_cases(args.case, cases_dir=cases_dir)
    validator = BondDataValidator(require_quotes=False)

    if args.self_check:
        parser: LLMBondParser | MockLLMBondParser = MockLLMBondParser()
        engine = "self-check"
    elif args.mock:
        parser = MockLLMBondParser()
        engine = "mock"
    else:
        try:
            parser = LLMBondParser()
            engine = "llm"
        except Exception as exc:  # noqa: BLE001
            print(f"LLM unavailable ({exc}), falling back to mock")
            parser = MockLLMBondParser()
            engine = "mock-fallback"

    results: list[CaseResult] = []
    for case in cases:
        print(f"→ running {case['id']} ...", flush=True)
        results.append(
            await run_case(
                case, parser=parser, validator=validator, self_check=args.self_check
            )
        )

    overall = _print_report(results)
    if args.report:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        _write_report(results, overall=overall, cases=cases, report_path=report_path, engine=engine)

    if args.min_accuracy is not None and overall < args.min_accuracy:
        print(f"FAIL: accuracy {overall:.2f}% < required {args.min_accuracy:.2f}%")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Golden Dataset benchmark for bond LLM parsing")
    parser.add_argument("--case", help="Run a single case by id")
    parser.add_argument(
        "--cases-dir",
        default=None,
        help="Directory with case JSON files (default: tests/golden_dataset/cases)",
    )
    parser.add_argument("--mock", action="store_true", help="Use MockLLMBondParser (offline)")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Score expected vs expected (framework sanity, should be 100%)",
    )
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="Exit with code 1 if Field-Level Accuracy is below this %%",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Write JSON report path (e.g. storage/eval50/report.json)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
