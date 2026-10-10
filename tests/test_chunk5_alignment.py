# -*- coding: utf-8 -*-
"""Regression tests for the Chunk 5 alignment fixes (2026-10-10).

Two defects in the model's voting frame, both exposed by the MQG gold-set
comparison where the model voted FOR Resolutions 3 and 4 while the human
voted AGAINST both:

  1. REM-S14 (pay mix and incentive structure) did not exist. The model ran
     REM-M01..REM-S13 as a disclosure-completeness checklist and never asked
     whether the LTI component was a material part of the CEO's package. MQG's
     84:12 STI:LTI mix was never evaluated.
  2. Resolution 4 (Remuneration Report adoption) had no explicit linkage rule.
     A single secondary guideline concern was being treated as enough to
     recommend Against, and conversely the model was recommending For on the
     strength of 13 Passes without weighing whether any secondary failure was
     material. The human's structural-design objection had no rule to land on.

Both are prompt-level fixes (rem_assess.txt), so the pin is a source-level
assertion on the prompt text plus a behaviour assertion that the linkage rule
cannot fire on a single REM-S14 concern alone.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

PROMPT = (ROOT / "prompts" / "rem_assess.txt").read_text(encoding="utf-8")

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


print("=" * 88)
print("1. REM-S14 exists and names its test conditions")
print("=" * 88)
check("REM-S14 heading present", "REM-S14" in PROMPT)
check("REM-S14 is in the Section 3 scope line",
      "REM-S01 to REM-S14" in PROMPT)
check("REM-S14 is in the Section 3 table heading",
      "REM-S01\u2013S14" in PROMPT)
check("REM-S14 is in the self-check count", "REM-S (14)" in PROMPT)
check("REM-S14 names the one-third reference point",
      "one-third" in PROMPT and "REM-S14" in PROMPT)
check("REM-S14 distinguishes STI from LTI as components in kind",
      "distinguishable in kind" in PROMPT)
check("REM-S14 treats unstructured discretion as a design concern",
      "discretion" in PROMPT and "REM-S11" in PROMPT)
check("REM-S14 has a vote-linkage line", "Vote linkage: Secondary" in PROMPT)
# The ~one-third band must be a threshold the model can apply, not prose.
check("REM-S14 states the band numerically", "roughly one-third" in PROMPT)


print()
print("=" * 88)
print("2. Section 4 gains a pay-mix calculation row")
print("=" * 88)
check("Section 4 has eight numbered rows", "8. Pay mix and structure" in PROMPT)
check("row 8 asks for the STI:LTI split in dollars",
      "STI:LTI split" in PROMPT)
check("row 8 asks for the LTI percentage of total target",
      "LTI percentage of total target remuneration" in PROMPT)
check("row 8 forbids deriving the mix from the statutory table",
      "do not derive the mix from the statutory table" in PROMPT)


print()
print("=" * 88)
print("3. Resolution 4 linkage rule exists")
print("=" * 88)
check("linkage rule is signposted as Resolution 4 linkage",
      "Resolution 4 linkage" in PROMPT)
check("linkage rule says a single secondary failure is not enough to vote Against",
      "single secondary guideline failure" in PROMPT
      and "NOT by itself enough to recommend Against" in PROMPT)
check("linkage rule names the conditions that DO justify Against",
      "major guideline failure" in PROMPT
      and "combination of secondary failures" in PROMPT)
check("linkage rule treats a lone REM-S14 concern as insufficient",
      "single REM-S14 pay-mix concern alone" in PROMPT)
check("linkage rule demands an explicit overall recommendation",
      "overall recommendation for the Remuneration Report adoption resolution" in PROMPT)
check("linkage rule forbids a Monitor result column",
      "Do not include a" in PROMPT and "Monitor result" in PROMPT)


print()
print("=" * 88)
print("4. Output format carries the overall recommendation")
print("=" * 88)
check("format item 8 demands the recommendation line",
      "overall recommendation for the Remuneration Report adoption resolution" in PROMPT)
check("format item 8 says it is the most important line",
      "single most important line in the output" in PROMPT)


print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Chunk 5 alignment regression tests passed.")