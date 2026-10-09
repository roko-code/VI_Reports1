# -*- coding: utf-8 -*-
"""Verify that every page cited in a stage's markdown output was actually seen.

Why this exists
---------------
Stage 4d had a semantic validator that could check a structured
`value + page` pair against the page text. Stages 5 and 6 have never had any
equivalent. They emit markdown with inline `[p.N]` citations, and nothing has
ever confirmed those page numbers correspond to pages that were supplied.

That is a *missing* check rather than a wrong one, which makes it worse, not
better: a false pass fails loudly once somebody looks, whereas a missing check
fails silently on every result the stage has ever produced. The whole class
here is long messy statutory tables, where the same value-plus-page mistake has
now been found three times in Stage 4 alone.

Scope, honestly stated
---------------------
This verifies citation *existence*, not citation *accuracy*: that p.145 was in
the retrieved set, not that the number quoted next to `[p.145]` appears there.
Existence is the cheap, high-yield half - invented page numbers are the
dominant failure and need no prose parsing. Accuracy for Stages 5/6 would need
structured output fields to check against, which those stages do not emit;
that is a larger change and is not attempted here.

Note on printed vs PDF page numbers: both prompts instruct the model to cite
the *printed* page number, which need not equal the PDF index. This checker
accepts a citation if it matches any supplied page's number or its printed
label, and reports near-misses rather than failing outright, precisely because
the two numbering schemes legitimately differ.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# [p.94]  [p.94-99]  [p.94–99]  [PDF p.94]  [pp.94-99]
# 1-4 digits, not followed by another digit: page 0 does not exist, and a
# 5-digit "page" is not a page number. Both are ignored rather than counted as
# citations, because a citation we silently drop is a citation nobody checks.
CITE = re.compile(
    r"\[(?:PDF\s+)?p{1,2}\.?\s*(\d{1,4})\s*(?:[-–—]\s*(\d{1,4}))?\](?!\w)",
    re.IGNORECASE)


@dataclass
class CitationReport:
    total: int = 0
    valid: int = 0
    unsupplied: list[tuple[int, int]] = field(default_factory=list)  # (lo, hi)
    distinct_pages: set[int] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.unsupplied

    def summary(self) -> str:
        if self.total == 0:
            return ("no [p.N] citations found in the output - the stage is "
                    "required to cite evidence for every result, so this "
                    "needs checking by hand")
        pct = self.valid / self.total * 100
        return (f"{self.valid}/{self.total} citations ({pct:.0f}%) resolve to "
                f"supplied pages; {len(self.unsupplied)} do not")


def cited_pages(text: str) -> list[tuple[int, int]]:
    """Every citation as an inclusive (lo, hi) page span.

    Page numbers below 1 are dropped - page 0 is not a page.
    """
    out = []
    for m in CITE.finditer(text or ""):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo:
            lo, hi = hi, lo
        if lo < 1:
            continue
        out.append((lo, hi))
    return out


def check_citations(output_text: str, supplied_pages) -> CitationReport:
    """Check citations against the page numbers actually supplied to the model.

    `supplied_pages` may be a list of page dicts (as returned by
    `ar_text_cache` / `ar_retrieval`) or a set of ints.
    """
    if isinstance(supplied_pages, dict):
        # Two shapes arrive here: {page_number: text} (int keys) and
        # {"94": "text"} (string keys). Take ints from both the keys and any
        # integer values, so neither shape silently matches nothing - which is
        # what a string-keyed dict did before, failing every citation.
        known = set()
        for k, v in supplied_pages.items():
            for cand in (k, v):
                try:
                    known.add(int(cand))
                except (TypeError, ValueError):
                    continue
    elif isinstance(supplied_pages, (set, frozenset)):
        known = set(supplied_pages)
    else:
        known = set()
        for p in supplied_pages or []:
            if isinstance(p, dict):
                for k in ("page_number", "page", "printed_page", "label"):
                    if p.get(k) is not None:
                        try:
                            known.add(int(p[k]))
                        except (TypeError, ValueError):
                            pass
            else:
                try:
                    known.add(int(p))
                except (TypeError, ValueError):
                    pass
    # a citation to a range is satisfied if any page in it was supplied; a
    # citation to a single page must match exactly.
    rep = CitationReport()
    for lo, hi in cited_pages(output_text):
        rep.total += 1
        span = set(range(lo, hi + 1))
        rep.distinct_pages |= span
        if span & known:
            rep.valid += 1
        else:
            rep.unsupplied.append((lo, hi))
    return rep


def format_problems(rep: CitationReport, limit: int = 12) -> list[str]:
    """Human-readable problems, for a log line or a validator message."""
    problems = []
    if rep.total == 0:
        problems.append(
            "no [p.N] citations found; the prompt requires a page citation for "
            "every result, so this output is unverifiable")
        return problems
    if rep.unsupplied:
        shown = ", ".join(f"p.{lo}" if lo == hi else f"p.{lo}-{hi}"
                          for lo, hi in rep.unsupplied[:limit])
        more = "" if len(rep.unsupplied) <= limit else (
            f" (+{len(rep.unsupplied) - limit} more)")
        problems.append(
            f"{len(rep.unsupplied)} of {rep.total} citations reference pages "
            f"that were not supplied to the model: {shown}{more}. Either the "
            "page numbers are invented, or they are printed page numbers that "
            "differ from the PDF index - check the latter before assuming the "
            "former, and do not cite a page you were not given")
    return problems
