# -*- coding: utf-8 -*-
"""Force the two validator branches that had never fired.

Both were previously "true because the model reached the right terminal state by
a different path" - the self-confirmation pattern from docs/validator_traps.md.
Each test below supplies a real page and asserts the specific branch fires.

1. `figure_fy != target_fy`: NAB FY25 p.121 is a five-year historical realised
   table (2025, 2024, 2023, 2022, 2021). A 2023 row is a real AUD figure on a
   real page but the wrong year for a 2025 question.
2. `not_disclosed` with a disclosure present: NAB FY24 p.145 publishes a
   realised CEO figure. The model called this `no_realised_disclosure` live.
   That is a false negative and the rule must reject it.
"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

import performance_table as pt  # noqa: E402
from disclosure_check import disclosed_ceo_rows  # noqa: E402

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
             currency="AUD", figure_fy="2026", ceo_served_part_year=None,
             basis="disclosed_realised", source_method="disclosed",
             cross_check_calculated_m=7.04,
             cross_check_explained_by="unexplained",
             cross_check_source_page="", lti_status="deferred",
             context_notes=[], ceo_name="X", target_fy="2026")
    d.update(over)
    return d


print("=" * 90)
print("1. figure_fy != target_fy must fire on a real multi-year table")
print("=" * 90)
p121 = page("NAB", 2025, 121)
v121 = pt._make_ceo_pay_validator({121: p121}, "NAB")
e = v121(base(ceo_realised_rem=4.282063, ceo_realised_rem_page=121,
              currency="AUD", figure_fy="2023", target_fy="2025"))
check("a real 2023 figure answering a 2025 question is REJECTED",
      any("year_mismatch" in x for x in e), str(e)[:280])
check("the rejection names both years",
      any("2025" in x and "2023" in x for x in e), str(e)[:280])

e = v121(base(ceo_realised_rem=7.566768, ceo_realised_rem_page=121,
              currency="AUD", figure_fy="2025", target_fy="2025"))
check("the matching-year figure on the same page is ACCEPTED",
      not e, str(e)[:280])

print()
print("=" * 90)
print("2. not_disclosed must be rejected when a disclosure exists")
print("=" * 90)
p145 = page("NAB", 2024, 145)
rows = disclosed_ceo_rows(p145, 145)
check("detector finds the NAB FY24 p.145 CEO row",
      any("Irvine" in r.text for r in rows), str([r.text[:80] for r in rows]))
check("detector flags the part-year qualifier",
      any(r.part_year for r in rows), str([(r.text[:60], r.part_year) for r in rows]))

v145 = pt._make_ceo_pay_validator({145: p145}, "NAB")
e = v145(base(ceo_realised_rem=None, ceo_realised_rem_page=None,
              basis="not_disclosed", currency="AUD", figure_fy="2024",
              target_fy="2024",
              not_disclosed_reason="no_realised_disclosure",
              cross_check_calculated_m=None))
check("'no_realised_disclosure' is REJECTED on this page",
      any("DO disclose realised remuneration" in x for x in e), str(e)[:300])
check("the rejection points at p.145",
      any("p.145" in x for x in e), str(e)[:300])
check("the rejection redirects to the part-year flag",
      any("ceo_served_part_year" in x for x in e), str(e)[:300])

# The corrected answer for NAB FY24: a real figure, flagged part-year.
e = v145(base(ceo_realised_rem=1.743794, ceo_realised_rem_page=145,
              currency="AUD", figure_fy="2024", target_fy="2024",
              basis="disclosed_realised", ceo_served_part_year=True,
              cross_check_calculated_m=None, cross_check_explained_by=None))
check("the corrected part-year answer is ACCEPTED", not e, str(e)[:300])

print()
print("=" * 90)
print("3. RIO FY24 keeps not_disclosed - it genuinely has no realised table")
print("=" * 90)
rio = {}
for pno in (131, 138, 140):
    rio[pno] = page("RIO", 2024, pno)
v_rio = pt._make_ceo_pay_validator(rio, "Rio Tinto")
found_rio = pt.disclosed_realised_figures(rio)
check("no realised CEO disclosure on RIO FY24's key pages",
      not found_rio, str([r.describe()[:70] for r in found_rio]))
e = v_rio(base(ceo_realised_rem=None, ceo_realised_rem_page=None,
               basis="not_disclosed", currency="GBP", figure_fy="2023",
               target_fy="2024", not_disclosed_reason="year_mismatch",
               cross_check_calculated_m=None))
check("RIO FY24 not_disclosed / year_mismatch is still ACCEPTED",
      not e, str(e)[:300])

print()
print("=" * 90)
print("4. part_year_ceo must no longer be a valid not_disclosed reason")
print("=" * 90)
props = pt.PROMPT_3_SCHEMA["properties"]
check("part_year_ceo removed from not_disclosed_reason enum",
      "part_year_ceo" not in props["not_disclosed_reason"]["enum"],
      str(props["not_disclosed_reason"]["enum"]))
check("ceo_served_part_year exists as its own field",
      "ceo_served_part_year" in props)
prompt = (ROOT / "prompts" / "5yr_perf_hist_3.txt").read_text(encoding="utf-8")
check("prompt no longer offers part_year_ceo as a not_disclosed reason",
      "part_year_ceo" not in prompt)

print()
print("=" * 90)
print("5. schema must accept the normal disclosed answer (null reason/flag)")
print("=" * 90)
import jsonschema  # noqa: E402
ndr = props["not_disclosed_reason"]
check("not_disclosed_reason enum allows null",
      None in ndr["enum"], str(ndr["enum"]))
check("not_disclosed_reason enum matches the prompt's three reasons",
      sorted(x for x in ndr["enum"] if x) == ["no_realised_disclosure", "non_aud_currency", "year_mismatch"],
      str(ndr["enum"]))
check("ceo_served_part_year accepts null",
      "null" in props["ceo_served_part_year"]["type"],
      str(props["ceo_served_part_year"]["type"]))
disclosed_answer = {"ceo_name": "X", "target_fy": "2026", "source_method": "disclosed",
                    "ceo_realised_rem": 6.63, "ceo_served_part_year": None,
                    "not_disclosed_reason": None}
try:
    jsonschema.validate(disclosed_answer, pt.PROMPT_3_SCHEMA)
    check("a disclosed answer with null reason/flag validates", True, "")
except Exception as e:
    check("a disclosed answer with null reason/flag validates", False, str(e)[:200])

print()
print("=" * 90)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Branch-coverage tests passed.")
