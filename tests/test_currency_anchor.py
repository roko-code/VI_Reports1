# -*- coding: utf-8 -*-
"""Tests for currency_anchor, built around the failure it was written to stop.

The digit-only check `_page_contains_value(t, 8.45)` returned True against a
Rio Tinto page headed in GBP. These tests exist so that cannot regress quietly
on the next refactor. Read docs/validator_traps.md before adding to them.
"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from currency_anchor import (  # noqa: E402
    anchored_figures, aud_million_anchors, contains_aud_million,
    near_miss_currencies,
)

FIXTURE = ROOT / "tests" / "fixtures" / "rio_2024_mixed_currency.txt"
CACHE = ROOT / "data" / "_cache" / "annual_report_text"

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")
        failures.append(name)


def load_fixture():
    return FIXTURE.read_text(encoding="utf-8")


def cached_page(tk, yr, pno):
    d = json.loads((CACHE / f"{tk}_{yr}.json").read_text(encoding="utf-8"))
    return {x["page_number"]: (x.get("text") or "") for x in d["pages"]}[pno]


print("=" * 88)
print("1. Per-row currency, mixed A$ / GBP in one column (RIO FY24 p.138)")
print("=" * 88)
fx = load_fixture()
allf = anchored_figures(fx)


def figs_with(raw, seq=None):
    """All figures with this raw value. Not a dict - raws repeat across pages."""
    seq = allf if seq is None else seq
    return [f for f in seq if abs(f.raw - raw) < 1e-9]


def one(raw, seq=None):
    hits = figs_with(raw, seq)
    return hits[0] if hits else None


check("A$10,476 binds to AUD",
      (lambda f: f is not None and f.currency == "AUD")(one(10476)),
      str(one(10476)))
check("GBP3,116 binds to GBP",
      (lambda f: f is not None and f.currency == "GBP")(one(3116)),
      str(one(3116)))
check("GBP8,450 binds to GBP",
      (lambda f: f is not None and f.currency == "GBP")(one(8450)),
      str(one(8450)))
check("thousands scale applied -> GBP8,450 = 8.450m",
      abs(one(8450).value_m - 8.45) < 1e-6, str(one(8450).describe()))
check("thousands scale applied -> A$10,476 = 10.476m",
      abs(one(10476).value_m - 10.476) < 1e-6, str(one(10476).describe()))
check("percentages excluded (88.4% not a money figure)",
      not figs_with(88.4))

print()
print("=" * 88)
print("2. THE REGRESSION: a $m claim must not be satisfied by a GBP row")
print("=" * 88)
check("8.45 rejected although GBP8,450 digits match",
      contains_aud_million(fx, 8.45) is False)
check("3.116 rejected although GBP3,116 digits match",
      contains_aud_million(fx, 3.116) is False)
nm = near_miss_currencies(fx, 8.45)
check("near-miss explains it is GBP, not AUD",
      any(f.currency == "GBP" for f in nm), str([f.describe() for f in nm]))
check("3.92 was a RIO FY25 p.130 claim, not p.138 - assert it is absent here",
      near_miss_currencies(fx, 3.92) == [],
      str([f.describe() for f in near_miss_currencies(fx, 3.92)]))

print()
print("=" * 88)
print("3. Attached currency code, no space (RIO FY24 p.140 SGD981)")
print("=" * 88)
figs2 = anchored_figures(fx)
check("SGD981 parsed with no separating space",
      (lambda f: f is not None and f.currency == "SGD")(one(981, figs2)),
      str(one(981, figs2)))
check("A$1,188 parsed", (lambda f: f and f.currency == "AUD")(one(1188, figs2)),
      str(one(1188, figs2)))
check("C$258 parsed", (lambda f: f and f.currency == "CAD")(one(258, figs2)),
      str(one(258, figs2)))
check("GBP941 parsed", (lambda f: f and f.currency == "GBP")(one(941, figs2)),
      str(one(941, figs2)))
check("981 SGD does not satisfy a $m claim",
      contains_aud_million(fx, 0.981) is False)

print()
print("=" * 88)
print("4. Adversarial: footnote marker jammed against the digits")
print("=" * 88)
adv = ("Single total figure of remuneration (\u00a3\u2019000)\n"
       "Jakob Stausholm (Chief Executive) 2023 1,227 110 172 1,509 \u00a38,4501 56%\n"
       "Bold Baatar 140% \u00a39412 70% 30%\n")
f3 = anchored_figures(adv)
raws = [f.raw for f in f3]
check("jammed footnote digit not absorbed into the value",
      8450 in raws, f"parsed raws={raws}")
check("8,4501 splits to 8450 rather than 84501",
      84501 not in raws, f"parsed raws={raws}")
check("jammed GBP row still not accepted as AUD",
      contains_aud_million(adv, 8.45) is False)
f9412 = [f for f in f3 if abs(f.raw - 9412) < 1e-9]
# "£9412" with no separator is genuinely ambiguous: 9,412, or 941 with a
# footnote marker 2 jammed on. There is no way to tell, so the parser reads
# the digits as printed under the declared '000 scale. The comma-grouped case
# above ("£8,4501") IS separable, and is split correctly. This assertion
# documents the limit rather than pretending it away.
check("unseparated '£9412' reads as printed (9,412 '000) - ambiguity documented",
      bool(f9412) and f9412[0].currency == "GBP"
      and abs(f9412[0].value_m - 9.412) < 1e-6,
      str([f.describe() for f in f9412]))
check("unseparated '£9412' is still not accepted as AUD",
      contains_aud_million(adv, 9.412) is False)

print()
print("=" * 88)
print("5. Header-declared currency applies to rows with no per-row prefix")
print("=" * 88)
p131 = cached_page("RIO", 2024, 131)
f4 = anchored_figures(p131, 131)
ceo = [f for f in f4 if f.raw == 8450]
check("p.131 header (\u00a3'000) binds the 8,450 row to GBP",
      bool(ceo) and ceo[0].currency == "GBP",
      str([f.describe() for f in ceo]))
check("p.131 8,450 is NOT accepted as an $m figure",
      contains_aud_million(p131, 8.45, 131) is False)
check("p.131 near-miss reports GBP",
      any(f.currency == "GBP" for f in near_miss_currencies(p131, 8.45, 131)))

print()
print("=" * 88)
print("6. AUD still works - the fix must not break the passing case")
print("=" * 88)
# All three are ASX-listed issuers whose reports write a bare "$" for AUD, so
# the issuer context is passed explicitly.
ASX = dict(bare_currency="AUD")
p94 = cached_page("SUN", 2026, 94)
check("SUN FY26 p.94 6,634 accepted as AUD ('All figures are in $000s')",
      contains_aud_million(p94, 6.634, 94, **ASX) is True,
      str([f.describe() for f in aud_million_anchors(p94, 94, "AUD")][:4]))
check("SUN FY26 p.94 is rejected without the AUD context",
      contains_aud_million(p94, 6.634, 94) is False)
p72 = cached_page("SUN", 2025, 72)
check("SUN FY25 p.72 6,325 accepted as AUD",
      contains_aud_million(p72, 6.325, 72, **ASX) is True,
      str([f.describe() for f in aud_million_anchors(p72, 72, "AUD")][:4]))
p151 = cached_page("NAB", 2024, 151)
# p.151 declares no scale anywhere, so the scale is inferred: 2,982,645 is
# 2.98m as units and 2,982.6m as thousands, and only the units reading lands in
# a plausible CEO-total band. Both readings are computed and only a unique
# plausible one is accepted - see docs/validator_traps.md.
check("NAB FY24 p.151 2,982,645 resolves to the units reading",
      contains_aud_million(p151, 2.982645, 151, **ASX) is True,
      str([f.describe() for f in aud_million_anchors(p151, 151, "AUD")][:4]))
check("...and is marked inferred, not declared",
      any(f.scale_from == "inferred" and abs(f.raw - 2982645) < 1
          for f in anchored_figures(p151, 151, "AUD")))
check("a share-count magnitude still does not resolve (outside the band)",
      contains_aud_million(p151, 0.095363, 151, **ASX) is False)
check("a thousands reading of 2,982.6m is NOT accepted (out of band)",
      contains_aud_million(p151, 2982.645, 151, **ASX) is False)

print()
print("=" * 88)
print("7. Bare $ is never silently treated as AUD without issuer context")
print("=" * 88)
bare = "Total remuneration 6,634 1,412 921\n"
f5 = anchored_figures(bare)
check("bare $ flagged ambiguous, not AUD",
      all(f.currency != "AUD" for f in f5),
      str([f.describe() for f in f5]))
check("bare $ cannot satisfy a $m claim with no context",
      contains_aud_million(bare, 6.634) is False)
check("qualifier A$ is accepted",
      contains_aud_million("All figures in $000s\nA$6,634", 6.634) is True)
check("NZ$ is not accepted as AUD",
      contains_aud_million("All figures in $000s\nNZ$6,634", 6.634) is False)
check("US$ is not accepted as AUD",
      contains_aud_million("All figures in $000s\nUS$6,634", 6.634) is False)
check("bare $ WITH AUD context is accepted (ASX convention)",
      contains_aud_million("All figures in $000s\n$6,634", 6.634,
                           bare_currency="AUD") is True)

print()
print("=" * 88)
print("8. Spaced notation - a corpus sweep found 5 tokens the first version missed")
print("=" * 88)
# Rio Tinto FY23 prints "A $", "US $" and "S $" with a space. The first regex
# required the qualifier and symbol to be adjacent and silently missed all five.
spaced = ("Total remuneration ($000)\n"
          "J Dubois 2024 1,200 3,100 A $4,500 60% 40%\n"
          "K Smith 2024 1,100 2,200 US $3,750 55% 45%\n"
          "L Ng 2024 900 1,400 S $2,650 50% 50%\n")
sf = anchored_figures(spaced)
sp_vals = {round(f.value_m, 3): f.currency for f in sf if f.value_m}
check("'A $4,500' resolves to AUD", sp_vals.get(4.5) == "AUD", str(sp_vals))
check("'US $3,750' resolves to USD", sp_vals.get(3.75) == "USD", str(sp_vals))
check("'S $2,650' resolves to SGD", sp_vals.get(2.65) == "SGD", str(sp_vals))
check("'A $4,500' satisfies an AUD $m claim",
      contains_aud_million(spaced, 4.5, bare_currency="AUD") is True)
check("'US $3,750' does NOT satisfy an AUD $m claim",
      contains_aud_million(spaced, 3.75, bare_currency="AUD") is False)
check("'S $2,650' does NOT satisfy an AUD $m claim",
      contains_aud_million(spaced, 2.65, bare_currency="AUD") is False)

print()
print("=" * 88)
print("9. Reporting currency is per company, not hardcoded AUD")
print("=" * 88)
from currency_anchor import reporting_currency_for  # noqa: E402

bare_page = "Total remuneration ($000)\nJ Dubois 2024 1,200 2,500 60% 40%\n"
check("comparator resolves a known company", reporting_currency_for("BHP Group") == "AUD",
      reporting_currency_for("BHP Group"))
check("unknown company falls back to AUD",
      reporting_currency_for("Nonexistent Co") == "AUD")
check("AUD-reporting issuer: bare $ satisfies an AUD claim",
      contains_aud_million(bare_page, 2.5, bare_currency="AUD") is True)
# The failure mode the user named: a USD-reporting filer whose bare $ figures
# were silently tagged AUD. Must not accept an AUD claim.
check("USD-reporting issuer: bare $ does NOT satisfy an AUD claim",
      contains_aud_million(bare_page, 2.5, bare_currency="USD") is False)
check("USD-reporting issuer: bare $ does satisfy a USD claim context",
      not [f for f in anchored_figures(bare_page, bare_currency="USD")
           if f.is_unambiguous_aud])

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("All currency_anchor tests passed.")
