# -*- coding: utf-8 -*-
"""Tests for the deterministic EPS extractor.

The live defect this pins: AMP FY2024 was reported as an EPS of 9.3c, which
is FY2023's number - the FY2024 AR prints the current year and the
comparative side by side and the value was read from the wrong column.  FY2024
is 5.7c reported and 9.0c underlying.

The extractor's job is to use the column header to decide which number
belongs to which year, so the tests use the real header shapes AMP uses:
two-digit years, footnote markers glued on by the PDF text extraction, and
half-year columns.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import eps_extraction as ee  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


def page(text, number=1):
    return {"page_number": number, "text": text}


print("=" * 80)
print("1. Header column parsing")
print("=" * 80)
check("FY23 FY22",
      ee.header_columns("Key performance metrics FY23 FY22") == [(2023, False), (2022, False)])
check("FY 23 2H 23 1H 23 FY 22",
      ee.header_columns("FY 23 2H 23 1H 23 FY 22")
      == [(2023, False), (2023, True), (2023, True), (2022, False)])
check("footnote glued onto 1H 24 and FY 23",
      ee.header_columns("FY 24 2H 24 1H 241 FY 231")
      == [(2024, False), (2024, True), (2024, True), (2023, False)])
check("four digit years",
      ee.header_columns("2017 2018 2019 2020 2021")
      == [(2017, False), (2018, False), (2019, False), (2020, False), (2021, False)])
check("no year tokens -> no columns", ee.header_columns("Profit and loss") == [])
check("glued FY 24 1 in a %FY column", ee.header_columns("Profit ($m) FY 25 FY 241 %FY")
      == [(2025, False), (2024, False)])

print()
print("=" * 80)
print("2. EPS rows in a two-column table")
print("=" * 80)
text = (
    "Key performance metrics FY 25 FY 24\n"
    "Earnings\n"
    "EPS – statutory (cps) 5.3 5.7\n"
    "EPS – underlying (cps) 11.3 9.0\n"
)
res = ee.extract_eps([page(text, 39)], 2025)
check("FY2025 reported EPS 5.3", res.get("eps") == 5.3, str(res))
check("FY2025 underlying EPS 11.3", res.get("underlying_eps") == 11.3, str(res))
check("source page recorded", res.get("eps_source_page") == 39, str(res))

res = ee.extract_eps([page(text, 39)], 2024)
check("FY2024 reported EPS 5.7 (comparative column)", res.get("eps") == 5.7, str(res))
check("FY2024 underlying EPS 9.0 (comparative column)", res.get("underlying_eps") == 9.0, str(res))

print()
print("=" * 80)
print("3. Footnote marker between the label and the first value")
print("=" * 80)
text = (
    "FY 24 2H 24 1H 241 FY 231\n"
    "EPS – underlying (cps) 2 9.0 4.5 4.4 7.2\n"
    "EPS – statutory (cps) 5.7 1.8 3.8 9.3\n"
)
res = ee.extract_eps([page(text, 19)], 2024)
check("FY2024 reported EPS 5.7", res.get("eps") == 5.7, str(res))
check("FY2024 underlying EPS 9.0", res.get("underlying_eps") == 9.0, str(res))

res = ee.extract_eps([page(text, 19)], 2023)
check("FY2023 reported EPS 9.3", res.get("eps") == 9.3, str(res))
check("FY2023 underlying EPS 7.2 (restated in the FY2024 AR)", res.get("underlying_eps") == 7.2,
      str(res))

print()
print("=" * 80)
print("4. Negative EPS in parentheses")
print("=" * 80)
text = (
    "FY 21 2H 21 1H 21 FY 20 %\n"
    "EPS – underlying (cps) 10.7 5.4 5.3 8.6\n"
    "EPS – actual (cps) (7.6) (12.2) 4.3 5.2\n"
)
res = ee.extract_eps([page(text, 17)], 2021)
check("FY2021 reported EPS -7.6", res.get("eps") == -7.6, str(res))
check("FY2021 underlying EPS 10.7", res.get("underlying_eps") == 10.7, str(res))

print()
print("=" * 80)
print("5. Prose form")
print("=" * 80)
text = (
    "Basic (loss)/ earnings per share for the year ended 31 December 2021 on a "
    "statutory basis was (7.6) cents per share (2020: 5.2 cents per share). "
    "On an underlying basis, the earnings per share was 10.7 cents per share "
    "(2020: 8.6 cents per share)."
)
res = ee.extract_eps([page(text, 34)], 2021)
check("prose reported EPS -7.6", res.get("eps") == -7.6, str(res))
check("prose underlying EPS 10.7", res.get("underlying_eps") == 10.7, str(res))

print()
print("=" * 80)
print("6. Rows that must not be read as EPS")
print("=" * 80)
res = ee.extract_eps([page("EPS Earnings Per Share", 44)], 2025)
check("glossary heading ignored", not res, str(res))
res = ee.extract_eps([page("EPS Performance", 48)], 2025)
check("EPS as a word in a table header ignored", not res, str(res))
res = ee.extract_eps([page("EPS 1 January 2023 to Adjusted Earnings Per Share 13% 1 100% 0%", 61)], 2025)
check("LTI hurdle row ignored", not res, str(res))
res = ee.extract_eps([page("EPS – other financial 35%", 55)], 2025)
check("weighting row ignored", not res, str(res))

print()
print("=" * 80)
print("7. Year isolation")
print("=" * 80)
text = (
    "FY 23 2H 23 1H 23 FY 22\n"
    "EPS – actual (cps) 9.3 0.1 8.8 12.0\n"
    "EPS – underlying (cps) 1 6.8 3.0 3.8 5.7\n"
)
res = ee.extract_eps([page(text, 21)], 2022)
check("FY2022 reported EPS 12.0", res.get("eps") == 12.0, str(res))
check("FY2022 underlying EPS 5.7", res.get("underlying_eps") == 5.7, str(res))
res = ee.extract_eps([page(text, 21)], 2023)
check("FY2023 reported EPS 9.3", res.get("eps") == 9.3, str(res))
check("FY2023 underlying EPS 6.8", res.get("underlying_eps") == 6.8, str(res))

print()
print("=" * 80)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("eps_extraction OK.")
