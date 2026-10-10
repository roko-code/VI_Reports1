# -*- coding: utf-8 -*-
"""Regression test for the FY2026 scale-slip reconciliation defect.

Macquarie FY2026's prompt_3 returned the correct figure `$26,526,819` in its
own `disclosed_realised_quote` and cross-check (`cross_check_calculated_m` =
26.53), but recorded `ceo_realised_rem` = 0.021 - a scale-slip. The pre-fix
validator accepted it, and the compiled JSON then dropped it as implausibly
low, leaving FY2026 absent from the report. `_reconcile_ceo_value` must repair
the primary field from the model's own cross-check in that case.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from performance_table import _reconcile_ceo_value  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


print("=" * 88)
print("1. a scale-slip primary figure is repaired from the cross-check")
print("=" * 88)
slip = {
    "ceo_name": "Shemara R Wikramanayake",
    "ceo_realised_rem": 0.021,
    "disclosed_realised_rem": 0.021,
    "cross_check_calculated_m": 26.53,
    "ceo_served_part_year": False,
}
repaired = _reconcile_ceo_value(slip)
check("primary repaired to the cross-check value",
      repaired["ceo_realised_rem"] == 26.53,
      str(repaired))
check("disclosed field repaired too",
      repaired["disclosed_realised_rem"] == 26.53)
check("repair is flagged",
      repaired.get("reconciled_from_cross_check") is True)

print()
print("=" * 88)
print("2. a legitimate figure is left untouched")
print("=" * 88)
normal = {
    "ceo_name": "S.R. Wikramanayake",
    "ceo_realised_rem": 25.82,
    "disclosed_realised_rem": 25.82,
    "cross_check_calculated_m": 26.0,
    "ceo_served_part_year": False,
}
keep = _reconcile_ceo_value(normal)
check("a plausible primary is untouched", keep["ceo_realised_rem"] == 25.82)

print()
print("=" * 88)
print("3. a part-year figure is never inflated by a full-year cross-check")
print("=" * 88)
part_year = {
    "ceo_name": "Andrew Irvine",
    "ceo_realised_rem": 1.74,
    "disclosed_realised_rem": 1.74,
    "cross_check_calculated_m": 7.56,
    "ceo_served_part_year": True,
}
keep_py = _reconcile_ceo_value(part_year)
check("part-year 1.74 is not inflated to 7.56",
      keep_py["ceo_realised_rem"] == 1.74)

part_year_small = dict(part_year, ceo_realised_rem=0.5, disclosed_realised_rem=0.5)
keep_pys = _reconcile_ceo_value(part_year_small)
check("small part-year 0.5 is not inflated", keep_pys["ceo_realised_rem"] == 0.5)

print()
print("=" * 88)
print("4. no cross-check -> no repair")
print("=" * 88)
no_cc = {"ceo_realised_rem": 0.021, "ceo_served_part_year": False}
keep_nc = _reconcile_ceo_value(no_cc)
check("without a cross-check the primary is untouched",
      keep_nc["ceo_realised_rem"] == 0.021)

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Scale-slip reconciliation regression tests passed.")