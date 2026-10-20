# -*- coding: utf-8 -*-
"""Tests for the deterministic parts of share_prices (no network).

The Yahoo fetch itself is a live call and is exercised manually; the pure
helpers here are what can regress silently, so they are pinned.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import share_prices as sp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


print("=" * 80)
print("1. FY-end detection")
print("=" * 80)
check("31 March 2026", sp.detect_fy_end_month_day("financial year ended 31 March 2026") == (3, 31))
check("30 September 2025", sp.detect_fy_end_month_day("for the year ended 30 September 2025") == (9, 30))
check("30 June 2026 (year ended)", sp.detect_fy_end_month_day("year ended 30 June 2026") == (6, 30))
check("31 December 2025", sp.detect_fy_end_month_day("financial year ended 31 December 2025") == (12, 31))
check("no FY-end -> None", sp.detect_fy_end_month_day("nothing here") is None)

print()
print("=" * 80)
print("2. FY-end date clamping")
print("=" * 80)
check("31 Jun clamps to 30 Jun", sp._fy_end_date(2025, 6, 31) == __import__("datetime").date(2025, 6, 30))
check("29 Feb 2024 valid (leap)", sp._fy_end_date(2024, 2, 29) == __import__("datetime").date(2024, 2, 29))
check("31 Feb 2023 clamps to 28", sp._fy_end_date(2023, 2, 31) == __import__("datetime").date(2023, 2, 28))

print()
print("=" * 80)
print("3. simple TSR")
print("=" * 80)
closes = {2021: 100.0, 2022: 206.0, 2023: 175.66, 2024: None, 2025: 197.06}
tsr = sp.compute_simple_tsr(closes)
check("2022 TSR = 106%", tsr.get(2022) == 106.0, str(tsr))
check("2023 TSR = -14.73%", tsr.get(2023) == -14.73, str(tsr))
check("2024 (missing close) -> None", tsr.get(2024) is None, str(tsr))
check("2025 (prev close None) -> None", tsr.get(2025) is None, str(tsr))

print()
print("=" * 80)
print("3b. simple TSR including dividends paid")
print("=" * 80)
# A total shareholder return includes the dividends received over the year.
# Issuers define TSR that way themselves - AMP's 2025 annual report (p.173)
# defines it as "the changes in market value of AMP shares, plus the value of
# any dividends paid and capital returns".
closes = {2021: 1.01, 2022: 1.31, 2023: 0.93, 2024: 1.59, 2025: 1.82}
paid = {2022: 0.0, 2023: 0.05, 2024: 0.04, 2025: 0.03}
tsr = sp.compute_simple_tsr(closes, paid)
check("2022 price return, no dividend", tsr.get(2022) == 29.70, str(tsr))
check("2023 includes 5c dividend", tsr.get(2023) == -25.19, str(tsr))
check("2024 includes 4c dividend", tsr.get(2024) == 75.27, str(tsr))
check("2025 includes 3c dividend", tsr.get(2025) == 16.35, str(tsr))
check("no dividends -> price return",
      sp.compute_simple_tsr(closes) == sp.compute_simple_tsr(closes, None))

print()
print("=" * 80)
print("4. symbol mapping")
print("=" * 80)
check("MQG -> MQG.AX", sp.yahoo_symbol("MQG") == "MQG.AX")
check("rio lower -> RIO.AX", sp.yahoo_symbol("rio") == "RIO.AX")

print()
print("=" * 80)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("share_prices OK.")