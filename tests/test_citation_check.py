# -*- coding: utf-8 -*-
"""Tests for citation_check.

Stage 5 and 6 emit markdown with inline [p.N] citations and, before this, had
no validation of any kind on that axis. These tests pin the behaviour on the
real Stage 5 artifact where one exists, and on the cases most likely to fool
the extractor.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

from citation_check import (  # noqa: E402
    check_citations, cited_pages, format_problems,
)

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


supplied = [{"page_number": n} for n in range(90, 160)]

print("=" * 88)
print("1. citation forms")
print("=" * 88)
md = ("[p.94] and [p.94-99] and [p.94\u201399] and [PDF p.94] and "
      "[pp.120-131] and [p.131]")
check("all six citation forms are extracted",
      cited_pages(md) == [(94, 94), (94, 99), (94, 99), (94, 94),
                          (120, 131), (131, 131)],
      str(cited_pages(md)))
rev = "evidence [p.99-94]"
check("a reversed range is normalised, not dropped",
      cited_pages(rev) == [(94, 99)], str(cited_pages(rev)))

print()
print("=" * 88)
print("2. supplied vs invented")
print("=" * 88)
rep = check_citations(
    "Realised remuneration [p.94]. Prior year [p.99]. Invented [p.412].",
    supplied)
check("5 citations counted, 3 resolve, 1 does not",
      rep.total == 3 and rep.valid == 2 and rep.unsupplied == [(412, 412)],
      f"total={rep.total} valid={rep.valid} bad={rep.unsupplied}")
check("the invented page is named in the problem message",
      any("p.412" in p for p in format_problems(rep)))
check("a citation to a page outside the supplied set fails",
      rep.ok is False)

print()
print("=" * 88)
print("3. ranges are satisfied by partial overlap")
print("=" * 88)
rep = check_citations("a long section [p.90-200]", supplied)
check("a range overlapping supplied pages resolves",
      rep.ok is True and rep.valid == 1, f"{rep.summary()}")
rep = check_citations("a range entirely outside [p.500-560]", supplied)
check("a range with no overlap fails", rep.ok is False)

print()
print("=" * 88)
print("4. degenerate and hostile inputs")
print("=" * 88)
rep = check_citations("No citations at all in this output.", supplied)
check("zero citations is reported as unverifiable, not as a pass",
      rep.ok is True and rep.total == 0
      and any("no [p.N] citations" in p for p in format_problems(rep)),
      f"ok={rep.ok} total={rep.total}")
rep = check_citations("", supplied)
check("empty output does not raise", rep.total == 0)
rep = check_citations("pages [p.0] and [p.99999]", supplied)
check("page 0 and 5-digit 'pages' are ignored, not counted as citations",
      rep.total == 0, f"total={rep.total} {cited_pages('pages [p.0] and [p.99999]')}")
check("malformed brackets are ignored, not misparsed",
      cited_pages("[p.abc] and [p.] and [] and [xyz]") == [],
      str(cited_pages("[p.abc] and [p.] and [] and [xyz]")))
rep = check_citations("[p.94]", {"94": "x", "95": "y"})
check("a string-keyed dict of page numbers is accepted as supplied",
      rep.ok is True, f"{rep.summary()}")
rep = check_citations("[p.94]", {94: "text"})
check("an int-keyed page dict is accepted as supplied", rep.ok is True)
rep = check_citations("[p.94]", set([94, 95]))
check("a set of page numbers is accepted as supplied", rep.ok is True)

print()
print("=" * 88)
print("5. printed-page convention is flagged, not silently failed")
print("=" * 88)
# Both prompts ask for PRINTED page numbers, which need not equal the PDF
# index. A mismatch must produce an explanatory problem, not a bare rejection.
rep = check_citations("cite [p.412]", supplied)
msgs = format_problems(rep)
check("mismatch message mentions the printed/PDF distinction",
      any("printed page numbers" in m for m in msgs), str(msgs)[:200])

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Citation-check tests passed.")