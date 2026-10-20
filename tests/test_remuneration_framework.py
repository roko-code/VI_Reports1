# -*- coding: utf-8 -*-
"""Tests for the Annual Report CEO remuneration framework extractor.

The defect this pins: Stage 10's "CEO Remuneration Framework" table rendered
0.00 in every cell for AMP.  The renderer parsed the model's Section 4 prose,
which states the amounts in thousands ("$1,715 k"), and divided them by a
million.  The issuer also publishes no "Awarded Remuneration" table, so the
renderer's AR-based path found nothing either and the table stayed empty.

The extractor now reads the framework from the Annual Report itself and
reconciles it against the remuneration mix the issuer discloses alongside it,
so a mis-parsed table is rejected rather than rendered.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import remuneration_framework as rf  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


def page(text, number):
    return {"page_number": number, "text": text}


REALISED_PAGE = page(
    "44\n"
    "Remuneration report\n"
    "2.2 Actual remuneration realised by current executive KMP in 2025\n"
    "The table below is a voluntary, non-statutory disclosure that shows the realised remuneration received during 2025.\n"
    "STI & other LTI equity Total\n"
    "Fixed1 equity awards awards Other remuneration\n"
    "Executive KMP Year remuneration Cash STI2 vested3 released4 benefits5 received\n"
    "$'000 $'000 $'000 $'000 $'000 $'000\n"
    "Alexis George 2025 1,715 988 622 – – 3,325\n"
    "2024 1,715 948 301 359 1 3,324\n",
    48)

STI_PAGE = page(
    "56\n"
    "Remuneration report\n"
    "4.3 2025 STI outcomes\n"
    "The following table shows the STI awarded to current and former Executive KMP for the 2025 performance year.\n"
    "Pro rated target Total STI 40% to be as % of pro rated\n"
    "STI outcome 60% to be paid delivered in rated target STI max STI\n"
    "opportunity1 awarded2 as cash3 share rights3 opportunity4 opportunity4\n"
    "$'000 $'000 $'000 $'000 % %\n"
    "Executive KMP\n"
    "Alexis George 1,715 1,646 988 659 96% 64%\n"
    "David Cullen 523 501 301 200 96% 64%\n",
    60)

FRAMEWORK_PAGE = page(
    "48\n"
    "Remuneration report\n"
    "3.3 Remuneration framework in detail\n"
    "2025 STI\n"
    "STI opportunity Target STI opportunity is 100% of fixed remuneration (FR) for the CEO and Executive KMP and\n"
    "70% of FR for the former Chief Risk Officer (CRO). Maximum STI opportunity is 150% of target STI.\n"
    "Delivery 60% of the STI is delivered as cash and 40% is deferred into equity.\n"
    "2025 LTI\n"
    "LTI opportunity The value of the LTI awarded to the CEO and current Executive KMP is expressed as a percentage\n"
    "of FR. The face value of the 2025 LTI performance rights opportunity is 100% of FR.\n",
    52)

MIX_PAGE = page(
    "47\n"
    "Remuneration report\n"
    "3.2 Our remuneration mix\n"
    "Remuneration mix at maximum opportunity\n"
    "At maximum opportunity, the CEO and ExCo have 71.4% of their total remuneration delivered as variable reward.\n"
    "CEO and other Executive Committee members\n"
    "Fixed Remuneration 29% STI Deferred Share Rights 17%\n"
    "STI Cash 26% LTI Performance Rights 29%\n",
    51)

PAGES = [REALISED_PAGE, STI_PAGE, FRAMEWORK_PAGE, MIX_PAGE]

print("=" * 80)
print("1. CEO realised pay row")
print("=" * 80)
realised = rf._find_realised_pay(PAGES, "Alexis George")
check("row found", realised is not None)
if realised:
    check("fixed pay 1,715", realised["fixed"] == 1715.0, str(realised))
    check("cash STI 988", realised["cash_sti"] == 988.0, str(realised))
    check("total 3,325", realised["total"] == 3325.0, str(realised))
    check("source page 48", realised["page"] == 48, str(realised))
check("a row whose total does not reconcile is rejected",
      rf._find_realised_pay([page("Alexis George 2025 1,715 988 622 3,999", 48)], "Alexis George") is None)

print()
print("=" * 80)
print("2. STI outcomes row")
print("=" * 80)
sti = rf._find_sti_outcomes(PAGES, "Alexis George")
check("row found", sti is not None)
if sti:
    check("target 1,715", sti["target"] == 1715.0, str(sti))
    check("awarded 1,646", sti["awarded"] == 1646.0, str(sti))
    check("cash 988", sti["cash"] == 988.0, str(sti))
    check("deferred 659", sti["deferred"] == 659.0, str(sti))
    check("cash share about 60%", abs(sti["cash_pct"] - 60.0) < 0.5, str(sti))
    check("maximum about 2,572", abs(sti["max"] - 2572.0) < 2.0, str(sti))

print()
print("=" * 80)
print("3. Percentage rules")
print("=" * 80)
rules = rf._percent_rules(FRAMEWORK_PAGE["text"])
check("STI target 100% of FR", rules["sti_target_pct_of_fr"] == 100.0, str(rules))
check("STI max 150% of target", rules["sti_max_pct_of_target"] == 150.0, str(rules))
check("STI cash 60%", rules["sti_cash_pct"] == 60.0, str(rules))
check("LTI 100% of FR", rules["lti_pct_of_fr"] == 100.0, str(rules))

print()
print("=" * 80)
print("4. Disclosed remuneration mix")
print("=" * 80)
mix = rf._find_mix_at_maximum(PAGES)
check("mix found", mix is not None)
if mix:
    check("fixed 29%", mix.get("fixed") == 29.0, str(mix))
    check("STI cash 26%", mix.get("sti_cash") == 26.0, str(mix))
    check("STI equity 17%", mix.get("sti_equity") == 17.0, str(mix))
    check("LTI 29%", mix.get("lti") == 29.0, str(mix))

print()
print("=" * 80)
print("5. Full framework")
print("=" * 80)
result = rf.extract_ceo_framework(PAGES, "Alexis George")
check("framework derived", result is not None)
if result:
    check("fixed 1.715", abs(result["fixed"] - 1.715) < 0.001, str(result))
    check("STI cash target 1.029", abs(result["sti_cash"] - 1.029) < 0.001, str(result))
    check("STI equity target 0.686", abs(result["sti_equity"] - 0.686) < 0.001, str(result))
    check("LTI target 1.715", abs(result["lti"] - 1.715) < 0.001, str(result))
    check("target total 5.145", abs(result["total_target"] - 5.145) < 0.002, str(result))
    check("max STI cash 1.544", abs(result["max_sti_cash"] - 1.5435) < 0.002, str(result))
    check("max STI equity 1.029", abs(result["max_sti_equity"] - 1.029) < 0.002, str(result))
    check("max total 6.002", abs(result["total_max"] - 6.0025) < 0.005, str(result))
    check("validated against the disclosed mix",
          result["validation"].get("disclosed_mix") is not None, str(result))

print()
print("=" * 80)
print("6. A framework that contradicts the disclosed mix is rejected")
print("=" * 80)
wrong_mix = page(
    "47\n"
    "Remuneration mix at maximum opportunity\n"
    "Fixed Remuneration 50% STI Deferred Share Rights 5%\n"
    "STI Cash 10% LTI Performance Rights 35%\n",
    51)
check("mismatched mix rejected",
      rf.extract_ceo_framework(PAGES[:3] + [wrong_mix], "Alexis George") is None)
check("no CEO name -> no framework", rf.extract_ceo_framework(PAGES, "") is None)
check("unknown CEO -> no framework", rf.extract_ceo_framework(PAGES, "Someone Else") is None)

print()
print("=" * 80)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("remuneration_framework OK.")
