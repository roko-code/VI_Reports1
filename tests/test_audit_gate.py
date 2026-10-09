# -*- coding: utf-8 -*-
"""Regression test for the Stage 7 audit pass gate.

The gate used to read ``audit_result.get("aurdit_passed_threshold")`` - a typo
of the key two lines above - so the lookup always returned None and the gate
degenerated to ``overall_assessment != "FAIL"``. A PARTIAL below the 75-point
threshold therefore passed, and the score gate documented in run_audit's
docstring ("Returns True if audit passes (score >= threshold)") was not in
force.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from stage7_audit import _audit_passes  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


print("=" * 88)
print("audit gate: _audit_passes")
print("=" * 88)

check("score at/above threshold passes",
      _audit_passes({"audit_passed_threshold": True,
                     "overall_assessment": "PARTIAL"}) is True)

# The exact case that leaked before: below threshold, but the LLM said
# something other than FAIL. Under the old gate this returned True.
check("below threshold with PARTIAL assessment does NOT pass",
      _audit_passes({"audit_passed_threshold": False,
                     "overall_assessment": "PARTIAL"}) is False)
check("below threshold with PASS assessment does NOT pass",
      _audit_passes({"audit_passed_threshold": False,
                     "overall_assessment": "PASS"}) is False)

check("missing flag is a fail, not a fallthrough",
      _audit_passes({"overall_assessment": "PASS"}) is False)
check("empty result is a fail", _audit_passes({}) is False)

# Demonstrate the old expression would have mis-gated the first two cases.
def old_buggy_gate(a):
    return a.get("aurdit_passed_threshold") or a.get("overall_assessment") != "FAIL"

check("old gate actually passed a below-threshold PARTIAL (sanity)",
      old_buggy_gate({"overall_assessment": "PARTIAL"}) in (True, None))

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Audit-gate regression tests passed.")
