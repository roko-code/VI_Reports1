# -*- coding: utf-8 -*-
"""Regression tests for the review-fix Batch 1 findings.

Pins three defects found by code review of the uncommitted changeset:

  1. F2 - board_questions._load_other_resolutions read Stage 7 JSON keys
     (res_number/res_title/assessment_result/category) that the strict Stage 7
     schema no longer emits; every in-scope resolution rendered as
     "Resolution ? : N/A".
  2. F3 - text_summaries._build_summary_payload had the same key mismatch in
     its "Other Resolutions Assessment" section.
  3. F5 - performance_table._validate_ceo_pay_semantics rejected every
     ceo_realised_rem below 0.8m with no part-year exception, contradicting
     the prompt (a part-year total is expected to be smaller) and
     currency_anchor's 0.3m band floor.
  4. F4 - report_rendering._populate_voting_table applied one global F/U
     remuneration signal to every voting row; the vote is now
     resolution-specific (source-level marker pin, as in test_news_wiring).
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from board_questions import _load_other_resolutions  # noqa: E402
from text_summaries import _build_summary_payload  # noqa: E402
from performance_table import _validate_ceo_pay_semantics  # noqa: E402
from logging_utils import TechnicalLogger  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


# Stage 7 output shaped exactly like its strict schema (additionalProperties:
# false): in_scope_resolutions carry resolution_number + ai_result; titles and
# categories live on resolution_categorisation.
STAGE7_JSON = {
    "header": {"documents_supplied": [1, 2]},
    "resolution_categorisation": [
        {"resolution_number": "2.1", "title": "Adoption of the Remuneration Report", "category": "remuneration"},
        {"resolution_number": "5.1", "title": "Constitutional amendment", "category": "governance"},
        {"resolution_number": "3.1", "title": "Auditor rotation policy", "category": "governance"},
    ],
    "in_scope_resolutions": [
        {
            "resolution_number": "5.1",
            "rules_applied": ["GOV-01"],
            "ai_result": "F",
            "confidence": "High",
            "evidence_comparison_action": "compared against AR p.44",
            "missing_document_note": None,
        },
        {
            "resolution_number": "3.1",
            "rules_applied": ["GOV-10"],
            "ai_result": "U",
            "confidence": "Medium",
            "evidence_comparison_action": "no prior-year disclosure found",
            "missing_document_note": "prior auditor rotation policy not supplied",
        },
    ],
}

tmp = Path(tempfile.mkdtemp(prefix="batch1_"))
reports_dir = tmp / "reports"
reports_dir.mkdir()
(reports_dir / "SUN_2026_Other_Resolutions_Assessment.json").write_text(
    json.dumps(STAGE7_JSON), encoding="utf-8"
)

print("F2: Stage 9 other-resolutions rendering")
doc4 = _load_other_resolutions(tmp)
check("renders resolution numbers", "Resolution 5.1" in doc4 and "Resolution 3.1" in doc4, doc4[:400])
check("renders ai_result values", "Result: F" in doc4 and "Result: U" in doc4, doc4[:400])
check("pulls titles from resolution_categorisation", "Constitutional amendment" in doc4)
check("no placeholder rows", "Resolution ?" not in doc4 and ": N/A" not in doc4, doc4[:400])

print("F3: Stage 8 other-resolutions section")
lg = TechnicalLogger(tmp / "technical_log.md")
payload = _build_summary_payload(tmp, "SUN", 2026, "Suncorp Group Limited", lg)
check("renders resolution numbers", "Resolution 5.1" in payload and "Resolution 3.1" in payload, payload[-1200:])
check("renders ai_result values", "Result: F" in payload and "Result: U" in payload, payload[-1200:])
check("no placeholder rows", "Resolution ?" not in payload)

print("F5: part-year CEO plausibility exemption")
base = {
    "disclosed_realised_rem": 0.5,
    "ceo_realised_rem": 0.5,
    "basis": "disclosed_realised",
    "source_method": "disclosed",
    "ceo_realised_rem_page": 67,
}
problems_part_year = _validate_ceo_pay_semantics({**base, "ceo_served_part_year": True})
check("part-year 0.5m accepted with flag", problems_part_year == [], "; ".join(problems_part_year))
problems_full_year = _validate_ceo_pay_semantics({**base, "ceo_served_part_year": False})
check(
    "full-year 0.5m still rejected",
    any("implausibly low" in p for p in problems_full_year),
    "; ".join(problems_full_year),
)
problems_normal = _validate_ceo_pay_semantics(
    {**base, "disclosed_realised_rem": 7.783, "ceo_realised_rem": 7.783, "ceo_served_part_year": False}
)
check("normal full-year figure accepted", problems_normal == [], "; ".join(problems_normal))

print("F4: resolution-specific voting table logic (source pin)")
rendering_src = (ROOT / "src" / "report_rendering.py").read_text(encoding="utf-8")
check("remuneration signal scoped to remuneration rows", 'elif "remuneration" in res_text:' in rendering_src)
check(
    "fallback default is resolution-specific FOR",
    "derives from the director assessment" in rendering_src or "default to FOR" in rendering_src,
)

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Review-fix Batch 1 regression tests passed.")
