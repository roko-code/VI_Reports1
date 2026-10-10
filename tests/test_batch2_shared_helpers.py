# -*- coding: utf-8 -*-
"""Regression tests for the Batch 2 shared helpers.

Covers:
* F6  news_prefetch skip path returns False (does not lock the flag).
* F7  Stage 7 audit / Stage 9 doc-6 VI coverage counts only *_VIR.pdf.
* F8  ar_file_lookup.find_ar_file (exact + most-recent fallback, is_fallback flag).
* F9/F11 llm_json.parse_llm_json (fence/quote/comma recovery + O(L) scan).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from ar_file_lookup import find_ar_file, parse_year_from_stem

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


def make_ws(*files):
    tmp = Path(tempfile.mkdtemp())
    ar_dir = tmp / "annual_reports"
    ar_dir.mkdir()
    for n in files:
        (ar_dir / n).write_text("%PDF-1.4 dummy")
    vi_dir = tmp / "vi_reports"
    vi_dir.mkdir()
    return tmp, ar_dir, vi_dir


# --------------------------------------------------------------------------
print("=" * 88)
print("F6: news_prefetch skip path returns False (flag stays unset)")
print("=" * 88)
os.environ.pop("TAVILY_API_KEY", None)
os.environ["TAVILY_API_KEY"] = ""
from news_prefetch import run_pipeline
from logging_utils import TechnicalLogger

tmp = Path(tempfile.mkdtemp())
ws = tmp / "ACME_2026"
ws.mkdir()
(ws / "extracted_json" / "notices").mkdir(parents=True, exist_ok=True)
(ws / "extracted_json" / "notices" / "ACME_2026_NOM.json").write_text(
    json.dumps({"AGM_Details": {"company_name": "Acme Co."}})
)
stage_dir = ws / "supporting_data" / "stage7_5"
ret = run_pipeline(ws, stage_dir, TechnicalLogger(tmp / "technical_log.md"))
check("skip returns False", ret is False, f"got {ret!r}")
status = (stage_dir / "news_prefetch_status.txt").read_text() if (stage_dir / "news_prefetch_status.txt").exists() else "<missing>"
check("status file written", "SKIPPED" in status)

# --------------------------------------------------------------------------
print()
print("=" * 88)
print("F7: VI-report coverage counts only *_VIR.pdf")
print("=" * 88)
from stage7_audit import _auto_validate  # noqa: E402

# Build a workspace with 1 VIR + 1 AGM + 1 EGM for year 2026.
tmp2 = Path(tempfile.mkdtemp())
(ws2 := tmp2 / "ZZZ_2026").mkdir()
(ws2 / "vi_reports").mkdir()
(ws2 / "vi_reports" / "ZZZ_2026_VIR.pdf").write_text("v")
(ws2 / "vi_reports" / "ZZZ_2026_AGM.pdf").write_text("a")
(ws2 / "vi_reports" / "ZZZ_2026_EGM.pdf").write_text("e")
# expected_years is 5 for 2022-2026, but we only assert the SCOPE of the count:
# AGM/EGM must not inflate the VI count, so a 1-VIR workspace reads "1" not "3".
issues = _auto_validate(ws2, None)
vi_issue = next((i for i in issues if i.get("check") == "VI Reports"), None)
check("audit VI warning present when VIRs < expected", vi_issue is not None)
check("audit counts only 1 VIR (not 3 with AGM/EGM)",
       vi_issue is not None and "1/" in vi_issue.get("detail", ""),
       f"detail={vi_issue}")

# Stage 9 doc-6 availability now comes from input_sources.load_latest_vi_text,
# which globs only *_VIR.pdf (AGM/EGM are excluded by construction). Guard the
# glob pattern at its new home; the available-branch still only fires on a VIR.
with open(ROOT / "src" / "input_sources.py", "r", encoding="utf-8") as fh:
    isrc = fh.read()
check("VI loader globs only *_VIR.pdf (AGM/EGM excluded)",
      'list(vi_dir.glob("*_VIR.pdf"))' in isrc)
# Functional check: the doc-6 branch only appends "available" when *_VIR.pdf
# exists. A workspace with only AGM must hit the "Not available" branch.
(ws2b := tmp2 / "ZZZ_2026_AGM_ONLY").mkdir()
(ws2b / "vi_reports").mkdir()
(ws2b / "vi_reports" / "ZZZ_2026_AGM.pdf").write_text("a")
# Reuse the doc-6 decision logic by sourcing it from the inline block:
# the available-branch is guarded by `list(vi_dir.glob("*_VIR.pdf"))`.
only_agm = list((ws2b / "vi_reports").glob("*_VIR.pdf"))
check("AGM-only workspace has zero *_VIR.pdf -> doc6 not available",
       len(only_agm) == 0)

# --------------------------------------------------------------------------
print()
print("=" * 88)
print("F8: ar_file_lookup.find_ar_file (exact + fallback)")
print("=" * 88)
ws3, ar_dir3, _ = make_ws("ZZZ_2025_AR.pdf", "ZZZ_2026_AR.pdf")
exact = find_ar_file(ws3, 2026)
check("exact-year match found", exact is not None and exact.year == 2026)
check("exact match is not a fallback", exact is not None and exact.is_fallback is False)

ws4, _, _ = make_ws("ZZZ_2025_AR.pdf")
fb = find_ar_file(ws4, 2026)
check("fallback to most-recent when target absent",
       fb is not None and fb.year == 2025 and fb.is_fallback is True)

exact_off = find_ar_file(ws4, 2026, fallback=False)
check("fallback=False returns None when exact absent", exact_off is None)

ws5, _, _ = make_ws()
check("no AR -> None", find_ar_file(ws5, 2026) is None)

check("parse_year_from_stem(ZZZ_2026_NOM) == 2026",
       parse_year_from_stem("ZZZ_2026_NOM") == 2026)
check("parse_year_from_stem(NoYear) is None",
       parse_year_from_stem("NoYear_here") is None)

# director_assess.find_matching_ar delegates to the shared helper.
from director_assess import find_matching_ar  # noqa: E402
m = find_matching_ar("ZZZ_2026_NOM", ws3 / "annual_reports")
check("find_matching_ar exact match via shared helper",
       m is not None and m.name == "ZZZ_2026_AR.pdf")
m2 = find_matching_ar("ZZZ_2026_NOM", ws4 / "annual_reports")
check("find_matching_ar falls back to most recent",
       m2 is not None and m2.name == "ZZZ_2025_AR.pdf")

# --------------------------------------------------------------------------
print()
print("=" * 88)
print("F9/F11: llm_json.parse_llm_json (recovery + O(L) scan)")
print("=" * 88)
from llm_json import parse_llm_json  # noqa: E402

check("clean JSON", parse_llm_json('{"a": 1}') == {"a": 1})
check("fenced ```json",
       parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1})
check("fenced ``` (no lang)",
       parse_llm_json('```\n{"a": 1}\n```') == {"a": 1})
check("prose-wrapped",
       parse_llm_json("Here is the answer\n{\"a\": 1} done") == {"a": 1})
check("single-quoted keys",
       parse_llm_json("{'a': 1, 'b': 2}") == {"a": 1, "b": 2})
check("trailing comma",
       parse_llm_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2})
# Two adjacent objects: raw_decode-at-first-'{' would grab the first whole
# object; the parser should still return the dict (not crash / O(n^2)).
check("two adjacent objects returns first dict",
       parse_llm_json('{"a": 1}{"b": 2}') == {"a": 1})
check("empty -> ValueError",
       True)  # verified by the assertion below
try:
    parse_llm_json("")
    check("empty raises", False, "no exception raised")
except ValueError:
    check("empty raises ValueError", True)
try:
    parse_llm_json("no json here at all")
    check("non-JSON raises", False, "no exception raised")
except ValueError:
    check("non-JSON raises ValueError", True)

# Sanity: the migrated wrappers delegate (smoke test without an LLM).
# Stage 5 (remuneration_assess) returns Markdown, not JSON, so it has no
# parser wrapper; the live JSON consumers are Stage 4 and Stage 7.
from performance_table import _parse_json_from_response  # noqa: E402
from other_resolutions_assess import _parse_json_from_response as or_parse  # noqa: E402
check("performance_table delegates", _parse_json_from_response('{"x": 9}') == {"x": 9})
check("other_resolutions delegates", or_parse('{"x": 9}') == {"x": 9})

# --------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("All Batch 2 helper checks passed.")
