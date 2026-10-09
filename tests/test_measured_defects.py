# -*- coding: utf-8 -*-
"""Regression tests for the two validator defects the A/B run exposed.

Both were found by measuring, not by reading: neither a negative nor a positive
from the measurement is trustworthy until the validator itself is checked against
a case designed to fool it.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from performance_table import (  # noqa: E402
    PROMPT_3_SCHEMA, _make_ceo_pay_validator,
)

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


print("=" * 88)
print("1. lti_grant_years_tested accepts integer years")
print("=" * 88)
# Every model in the A/B run returned 2019 as an int against a string-only
# schema, and that single field drove most of the 75% FULL-arm retry rate.
schema = PROMPT_3_SCHEMA["properties"]["lti_grant_years_tested"]
check("items accept both string and integer",
      set(schema["items"]["type"]) == {"string", "integer"},
      str(schema))

try:
    import jsonschema
    for value in (["2019"], [2019], ["2019", 2020, "2021"]):
        jsonschema.validate(value, schema)
    check("all three year shapes validate", True)
except ImportError:
    print("        (jsonschema unavailable; type list checked structurally)")

print()
print("=" * 88)
print("2. 'unrelated' does not require a calculation to exist")
print("=" * 88)
validate = _make_ceo_pay_validator({})
CALC_MISSING = "cross_check_calculated_m is absent"


def calc_missing_problem(data):
    """True when the 'no calculation behind the explanation' rule fired.

    Checked by message rather than by `validate(...) is None` so that unrelated
    page-context rules, which need real page text this test does not supply,
    cannot make the assertion pass or fail for the wrong reason.
    """
    res = validate(data)
    if res is None:
        return False
    msgs = res if isinstance(res, list) else [res]
    return any(CALC_MISSING in str(m) for m in msgs)


base = {
    "ceo_name": "Steve Johnston",
    "target_fy": 2026,
    "source_method": "ar",
    "basis": "disclosed_realised",
    "ceo_realised_rem": "6.634",
    "lti_status": "partially_vested",
    "context_notes": [],
    "ceo_served_part_year": None,
    "disclosed_realised_rem": "6.634",
    "disclosed_realised_page": 94,
}

# "unrelated" means there is nothing to reconcile, so it is incoherent to demand
# a calculation behind it. This combination failed two measured runs.
d = dict(base, cross_check_explained_by="unrelated",
         cross_check_calculated_m=None)
check("'unrelated' with no calculation is not a rejection",
      calc_missing_problem(d) is False)

d = dict(base, cross_check_explained_by="not_comparable",
         cross_check_calculated_m=None)
check("'not_comparable' with no calculation is not a rejection",
      calc_missing_problem(d) is False)

# A mechanism that describes a gap between two numbers still needs the number.
for mech in ("fx", "deferral_timing", "accounting_basis", "vesting_basis"):
    d = dict(base, cross_check_explained_by=mech,
             cross_check_calculated_m=None)
    check(f"'{mech}' with no calculation is still rejected",
          calc_missing_problem(d) is True)

# And a supplied calculation satisfies the requirement.
d = dict(base, cross_check_explained_by="fx",
         cross_check_calculated_m="6.20")
check("'fx' with a calculation is not rejected on that ground",
      calc_missing_problem(d) is False)

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Both measured defects are pinned.")
