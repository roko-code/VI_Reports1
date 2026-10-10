# -*- coding: utf-8 -*-
"""Tests for the Stage 5 programme-level disclosure check.

check_disclosed_realised is the prose-level `not_disclosed` escape hatch: an
output that claims realised remuneration is not disclosed must not stand when a
supplied page actually publishes a CEO realised-remuneration row. The canonical
false negative is NAB FY24 p.145 - that page carries an Andrew Irvine
(for part year) realised row, and a live model answered `no_realised_disclosure`.
"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from disclosure_check import check_disclosed_realised  # noqa: E402

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


# NAB FY24 p.145 publishes a realised CEO row; p.146 is governance prose only.
p145 = page("NAB", 2024, 145)
p146 = page("NAB", 2024, 146)


def disclosed_rows():
    from disclosure_check import disclosed_realised_figures
    return disclosed_realised_figures({145: p145})


print("=" * 80)
print("A. a non-disclosure claim fires when a disclosed page is supplied")
print("=" * 80)
claimed = "Realised remuneration is not disclosed in the annual report."
problems = check_disclosed_realised(claimed, {145: p145})
check("non-disclosure claim is flagged", bool(problems), str(problems))
check("flags the disclosed page", any("p.145" in p for p in problems), str(problems))
check("flags the row text", any("Irvine" in p for p in problems), str(problems))
check("flags the part-year qualifier", any("part-year" in p for p in problems), str(problems))

print()
print("=" * 80)
print("B. no false positive when no page carries a CEO realised row")
print("=" * 80)
check("no problem when only governance page supplied",
      not check_disclosed_realised(claimed, {146: p146}),
      str(check_disclosed_realised(claimed, {146: p146})))
check("no problem when no pages supplied",
      not check_disclosed_realised(claimed, {}),
      str(check_disclosed_realised(claimed, {})))

print()
print("=" * 80)
print("C. no problem when the output does not claim non-disclosure")
print("=" * 80)
ok_output = "CEO realised remuneration of $1.74m is disclosed [p.145]."
check("a disclosing output is not flagged",
      not check_disclosed_realised(ok_output, {145: p145}),
      str(check_disclosed_realised(ok_output, {145: p145})))

print()
print("=" * 80)
print("D. alternative non-disclosure phrasings are caught")
print("=" * 80)
for phrase in [
    "No realised remuneration table was provided.",
    "The company does not disclose a realised remuneration figure.",
    "Realised remuneration: not available.",
    "The actual remuneration received by the CEO is not disclosed.",
]:
    p = check_disclosed_realised(phrase, {145: p145})
    check(f"flags: {phrase!r}", bool(p), str(p))

print()
print("=" * 80)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("disclosure_check OK.")