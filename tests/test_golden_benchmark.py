from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.bond import BondDocumentSchema
from app.services.extractor import stitch_broken_markdown_tables
from app.services.validator import BondDataValidator
from tests.golden_dataset.run_benchmark import compare_critical, _load_cases


CASES_DIR = Path(__file__).resolve().parent / "golden_dataset" / "cases"


def test_all_golden_cases_present() -> None:
    ids = {p.stem for p in CASES_DIR.glob("*.json")}
    assert ids == {
        "fixed_amortization_coupons",
        "floater_cbr_lag7",
        "floater_ruonia_average",
        "floater_ruonia_compounded",
        "broken_multipage_table",
    }


@pytest.mark.parametrize("path", sorted(CASES_DIR.glob("*.json")))
def test_expected_schema_and_self_score(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    expected = BondDocumentSchema.model_validate(case["expected"])
    # quotes may be incomplete in golden — disable for structural check
    result = BondDataValidator(require_quotes=False).validate(
        expected, source_text=case["input_markdown"]
    )
    # After resolving amortization dates, FIXED amort cases should be clean enough
    # FLOATER with full formula may APPROVE; incomplete would fail — our goldens are complete
    assert result.resolved_document is not None

    scores = compare_critical(case["expected"], result.resolved_document, case.get("critical_fields"))
    assert scores, f"No critical fields scored for {path.name}"
    assert all(s.ok for s in scores), [s.detail for s in scores if not s.ok]


def test_broken_multipage_stitch_keeps_all_coupon_rows() -> None:
    case = json.loads((CASES_DIR / "broken_multipage_table.json").read_text(encoding="utf-8"))
    stitched = stitch_broken_markdown_tables(case["input_markdown"])
    # continuation rows from page 2 must be present once
    assert stitched.count("| 6 | 12.08.2027") == 1
    assert stitched.count("| 10 | 12.08.2029") == 1
    assert stitched.count("| 8 | 25 | 25 |") == 1


def test_load_cases_helper() -> None:
    cases = _load_cases()
    assert len(cases) == 5
