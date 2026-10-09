# -*- coding: utf-8 -*-
"""Tests for wiring the pre-fetched news block into the assessment stages.

Stages 5, 6, 7 and 7-Audit must feed the Stage 7.5 output into their LLM
prompts; Stages 8 and 9 already did. Two classes of defect are pinned here:

  1. load_news_block / has_news_results behave correctly for a workspace with
     and without news files (the shared consumer contract).
  2. Each stage still contains its news section marker. Payload construction
     is inline in these modules, so a source-level marker check is the
     practical regression pin; a real end-to-end confirmation needs one LLM
     call per stage, which is done manually, not in CI.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from news_search_results import load_news_block, has_news_results  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


NEWS_DIR = "stage7_5_news_prefetch"
NEWS_FILE = "news_search_results.txt"
SAMPLE = "NEWS SEARCH RESULTS\n\n[1] AFR - 'Board faces pay backlash' - 12 Aug 2026 - https://example.com/a\n"

print("=" * 88)
print("1. load_news_block / has_news_results contract")
print("=" * 88)

with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp) / "SUN_2026"
    news_dir = ws / "supporting_data" / NEWS_DIR
    news_dir.mkdir(parents=True)

    # Empty workspace: standard not-available marker, no crash.
    check("has_news_results is False with no news files",
          has_news_results(ws) is False)
    empty_block = load_news_block(ws)
    check("load_news_block returns the not-available marker when absent",
          "No news results were available" in empty_block, empty_block[:100])

    # With a news file: content round-trips.
    (news_dir / NEWS_FILE).write_text(SAMPLE, encoding="utf-8")
    check("has_news_results is True once the file exists",
          has_news_results(ws) is True)
    check("load_news_block returns the file content",
          load_news_block(ws) == SAMPLE)

    # The reader must not depend on the stage directory name - a file in any
    # stage* directory is found. (This is what makes the 7.5 repositioning safe.)
    other_dir = ws / "supporting_data" / "stage0_alt"
    other_dir.mkdir()
    (other_dir / NEWS_FILE).write_text(SAMPLE, encoding="utf-8")
    check("a news file in a differently-named stage dir is still found",
          load_news_block(ws) == SAMPLE)

print()
print("=" * 88)
print("2. source markers: each stage still carries its news section")
print("=" * 88)

markers = {
    "remuneration_assess.py": [
        "from news_search_results import load_news_block",
        "=== DOCUMENT 8: NEWS SEARCH RESULTS",
    ],
    "director_assess.py": [
        "from news_search_results import load_news_block",
        "news_block: str = \"\"",
        "=== INPUT 4: NEWS SEARCH RESULTS",
        "news_block=news_block",
    ],
    "other_resolutions_assess.py": [
        "from news_search_results import load_news_block",
        "=== DOCUMENT 4: NEWS SEARCH RESULTS",
    ],
    "stage7_audit.py": [
        "from news_search_results import load_news_block, has_news_results",
        "SUPPLEMENTARY: NEWS SEARCH RESULTS",
        "NOT a pipeline output",
    ],
    # Stages 8 and 9 already consumed news before this change - guard them too.
    "text_summaries.py": ["from news_search_results import load_news_block"],
    "board_questions.py": ["from news_search_results import load_news_block"],
}

for module, needles in markers.items():
    src = (ROOT / "src" / module).read_text(encoding="utf-8")
    for needle in needles:
        check(f"{module}: {needle!r}", needle in src)

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("News-wiring tests passed.")
