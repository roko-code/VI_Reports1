# -*- coding: utf-8 -*-
"""Regression tests for the CEO-table percentage columns and the Chunk-5
options 2 (REM-S09 discretion reframe) and 3 (adversarial benchmarking).

The CEO Remuneration Framework table (Table 3 in the VI report) previously
left the "% of Total" columns empty and LTI Max. Opp blank, and used
inconsistent decimal places (1.527, 6.300, 4). It now:
  - shows every dollar figure to 2 decimal places,
  - fills "% of Total" for each row = row value / the column's total,
  - sets LTI Max. Opp = LTI target (face value, no leverage).

Options 2 and 3 are prompt-level fixes to rem_assess.txt, pinned by source
assertions on the prompt text.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

import docx  # noqa: E402
import report_rendering as rr  # noqa: E402

PROMPT = (ROOT / "prompts" / "rem_assess.txt").read_text(encoding="utf-8")

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


print("=" * 88)
print("1. CEO Remuneration Framework table: decimals, %, LTI max")
print("=" * 88)
template = ROOT / "data" / "_reference" / "word_templates" / "VI_Report_Blank.docx"
rem_md = (
    "| 2. Target and maximum framework | CEO fixed remuneration: $1,526,819. "
    "Target STI: $6,300,000 approx. (30% cash, 70% retained equity) from "
    "awarded profit share $21m pre-adjustment; maximum profit share before "
    "risk adjustment approx. $28m. LTI target face value approx. $4m PSUs "
    "granted. | High |"
)
doc = docx.Document(str(template))
rr._populate_ceo_remuneration_table(doc, rem_md)
t = doc.tables[3]
rows = [[c.text for c in r.cells] for r in t.rows]

fixed = rows[1]
sti_cash = rows[2]
sti_eq = rows[3]
lti = rows[4]
total = rows[5]

check("Fixed target formatted to 2dp", fixed[1] == "1.53", str(fixed))
check("Fixed max equals fixed", fixed[3] == "1.53", str(fixed))
check("STI-Cash target 2dp", sti_cash[1] == "6.30", str(sti_cash))
check("STI-Equity derived as 70% of STI", sti_eq[1] == "14.70", str(sti_eq))
check("LTI target 2dp", lti[1] == "4.00", str(lti))
check("LTI max = LTI target (face value)", lti[3] == "4.00", str(lti))
check("Target total = 26.53", total[1] == "26.53", str(total))
check("Max total = 33.53", total[3] == "33.53", str(total))
check("% of total column filled (target) for Fixed",
      fixed[2] == "5.76%", str(fixed))
check("% of total column filled (target) for Total",
      total[2] == "100.00%", str(total))
check("% of total column filled (max) for Total",
      total[4] == "100.00%", str(total))
check("every target % sums correctly: Fixed+STIcash+STIeq+LTI = total",
      float(total[1]) == round(1.526819 + 6.3 + 14.7 + 4.0, 2))

print()
print("=" * 88)
print("2. Option 2: REM-S09 flags reliance on unstructured discretion")
print("=" * 88)
check("REM-S09 names the discretionary-reliance test",
      "reliance on unstructured discretion" in PROMPT)
check("REM-S09 asks how much pay depends on disclosed rules",
      "how much of total target pay is determined by disclosed rules" in PROMPT)
check("REM-S09 says disclosure of discretion != disclosure of rules",
      "disclosure of discretion is not the same as the disclosure of the rules" in PROMPT)
check("REM-S09 gives a Partial/Fail consequence",
      "A material discretion-dependent component with no disclosed hurdle, sizing rule or justification is a Partial" in PROMPT)

print()
print("=" * 88)
print("3. Option 3: adversarial benchmarking (max entitlement, market-cap band)")
print("=" * 88)
check("REM-M01 compares MAXIMUM potential remuneration",
      "MAXIMUM potential remuneration" in PROMPT)
check("REM-M01 contrasts maximum entitlement vs realised pay",
      "maximum entitlement is out of line" in PROMPT)
check("REM-M01 narrows to a market-cap band",
      "0.5x" in PROMPT and "2x" in PROMPT and "market-cap band" in PROMPT)
check("Peer quantum row (Section 4) compares max STI + max LTI",
      "max STI + max LTI" in PROMPT)


print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("CEO-table and Chunk-5 options 2/3 regression tests passed.")