# -*- coding: utf-8 -*-
"""Tests for the deterministic parts of share_prices (no network).

The Yahoo fetch itself is a live call and is exercised manually; the pure
helpers here are what can regress silently, so they are pinned.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
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