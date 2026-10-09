# -*- coding: utf-8 -*-
"""End-to-end check that the Stage 4d validator now rejects the GBP false-pass.

Exercises the real `_make_ceo_pay_validator` against real cached pages, not the
currency module in isolation. A currency test that passes while the validator
still has a digit-only path is worth nothing - that is the trap from
docs/validator_traps.md, item by item.
"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

import performance_table as pt  # noqa: E402

C = ROOT / "data" / "_cache" / "annual_report_text"
failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


def page(tk, yr, pno):
    d = json.loads((C / f"{tk}_{yr}.json").read_text(encoding="utf-8"))
    return {x["page_number"]: (x.get("text") or "") for x in d["pages"]}[pno]


def base(**over):
    d = dict(ceo_realised_rem=6.63, ceo_realised_rem_page=94,
             currency="AUD", figure_fy="2026",
             basis="disclosed_realised", source_method="disclosed",
             cross_check_calculated_m=7.04, cross_check_explained_by="unexplained",
             cross_check_source_page="", lti_status="deferred", context_notes=[],
             ceo_name="X", target_fy="2026")
    d.update(over)
    return d


print("=" * 88)
print("A. The RIO false-pass is now closed")
print("=" * 88)
p131 = {131: page("RIO", 2024, 131)}
v = pt._make_ceo_pay_validator(p131)
errs = v(base(ceo_realised_rem=8.45, ceo_realised_rem_page=131,
              cross_check_calculated_m=3.5, cross_check_explained_by="unexplained"))
check("8.45m claim citing a GBP'000 page is REJECTED", bool(errs),
      "no error raised - the false pass is still live")
check("rejection names the currency", any("GBP" in e for e in errs),
      str(errs)[:300])

print()
print("=" * 88)
print("B. Genuine AUD figures still pass")
print("=" * 88)
p94 = {94: page("SUN", 2026, 94)}
v94 = pt._make_ceo_pay_validator(p94)
errs94 = v94(base(ceo_realised_rem=6.63, ceo_realised_rem_page=94))
check("SUN FY26 6.63 on its own p.94 is ACCEPTED", not errs94,
      str(errs94)[:300])

p151 = {151: page("NAB", 2024, 151)}
v151 = pt._make_ceo_pay_validator(p151)
errs151 = v151(base(ceo_realised_rem=2.98, ceo_realised_rem_page=151,
                    cross_check_calculated_m=3.0,
                    cross_check_explained_by="unexplained"))
# Corrected expectation. This page declares no scale, so an earlier version
# refused the match outright and treated 2,982,645 as unverifiable. That was a
# false negative: 2.98m as units and 2,982.6m as thousands, so the units
# reading is the only plausible CEO total and is now inferred. The earlier
# strictness would have made four of fourteen reports permanently unverifiable.
check("NAB FY24 p.151 2,982,645 resolves to the units reading (2.98m)",
      not errs151, str(errs151)[:300])

# ...but a share count in the same band as a component must still not pass.
p72 = {72: page("SUN", 2025, 72)}
v72 = pt._make_ceo_pay_validator(p72)
errs72 = v72(base(ceo_realised_rem=0.095, ceo_realised_rem_page=72,
                  cross_check_calculated_m=None,
                  cross_check_explained_by=None))
check("an implausibly small 0.095m claim is still rejected",
      bool(errs72), "a share count satisfied a CEO-total claim")

print()
print("=" * 88)
print("C. Currency / year / not_disclosed rules")
print("=" * 88)
p94 = {94: page("SUN", 2026, 94)}
v94 = pt._make_ceo_pay_validator(p94, "SUN")

e = v94(base(ceo_realised_rem=6.63, ceo_realised_rem_page=94, currency=None,
             figure_fy="2026"))
check("missing currency is REJECTED", any("no currency was declared" in x for x in e),
      str(e)[:200])

e = v94(base(ceo_realised_rem=6.63, ceo_realised_rem_page=94, currency="GBP",
             figure_fy="2026"))
check("non-AUD currency on a $m claim is REJECTED",
      any("non_aud_currency" in x for x in e), str(e)[:200])

e = v94(base(ceo_realised_rem=6.63, ceo_realised_rem_page=94, currency="AUD",
             figure_fy="2023"))
check("figure_fy != target_fy is REJECTED",
      any("year_mismatch" in x for x in e), str(e)[:200])

e = v94(base(ceo_realised_rem=None, ceo_realised_rem_page=None,
             basis="not_disclosed", currency="GBP", not_disclosed_reason=None,
             cross_check_calculated_m=None))
check("not_disclosed without a reason is REJECTED",
      any("not_disclosed_reason" in x for x in e), str(e)[:200])

e = v94(base(ceo_realised_rem=6.63, ceo_realised_rem_page=94, currency="AUD",
             figure_fy="2026", basis="not_disclosed",
             not_disclosed_reason="no_realised_disclosure"))
check("not_disclosed WITH a populated figure is REJECTED",
      any("contradictory" in x for x in e), str(e)[:200])

# The correct RIO FY24 answer: GBP table, 2023 rows, no 2024 figure.
e = v94(base(ceo_realised_rem=None, ceo_realised_rem_page=None,
             basis="not_disclosed", currency="GBP", figure_fy="2023",
             not_disclosed_reason="year_mismatch",
             cross_check_calculated_m=None))
check("RIO FY24 shaped answer is ACCEPTED", not e, str(e)[:300])

# And the NAB FY24 shape: statutory-only, part-year CEO.
e = v94(base(ceo_realised_rem=None, ceo_realised_rem_page=None,
             basis="not_disclosed", currency="AUD", figure_fy="2024",
             not_disclosed_reason="part_year_ceo",
             cross_check_calculated_m=None))
check("NAB FY24 shaped answer is ACCEPTED", not e, str(e)[:300])

print()
print("=" * 88)
print("D. The digit-only path is gone")
print("=" * 88)
src = (ROOT / "src" / "performance_table.py").read_text(encoding="utf-8")
check("_page_contains_value no longer defined", "def _page_contains_value" not in src)
check("_page_contains_value no longer called", "_page_contains_value(" not in src)
check("reporting currency is not hardcoded at the call site",
      "reporting_currency_for(company)" in src,
      "validator still assumes AUD")

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Validator currency integration OK.")
