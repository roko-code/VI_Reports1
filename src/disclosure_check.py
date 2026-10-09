# -*- coding: utf-8 -*-
"""Detect a *disclosed* realised-remuneration figure for the CEO.

Why this exists
---------------
A `not_disclosed` terminal state is an escape hatch. Live, the model used it on
NAB FY2024 - reporting `no_realised_disclosure` - while p.145 of that same
report is a voluntary non-statutory realised-remuneration table whose CEO row
reads `Andrew Irvine (for part year) 2024 1,237,544 506,250 1,743,794`. The
page was in the retrieved slice. Claiming nothing is disclosed has to be
*provable* from the pages, not merely asserted, or the model will take the exit
whenever a table is hard to read.

Why the row detection is fiddly
--------------------------------
Two table shapes occur in this corpus:

* role named inline with the figures, e.g. Rio Tinto FY26 p.133
  `Simon Trott (Chief Executive)1 2025 472 636 66 1,174 ...`
* a bare `Group CEO` header on its own line with named rows beneath, e.g.
  NAB FY24 p.145.

A first version matched only the inline form and therefore reported "no
disclosure" for the one page that mattered. Both shapes are handled here, and
the regression is pinned in tests/test_disclosure_check.py.

Scope note: Stages 5 and 6 have never validated a cited value against a cited
page at all. This module is the primitive they need; wiring it in is tracked
separately and is not yet done for those stages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REALISED_CAPTION = re.compile(
    r"(realised|realized)\s+remuneration"
    r"|remuneration\s+(actually\s+)?realised"
    r"|actual\s+remuneration"
    r"|remuneration\s+outcomes?\s+(versus|against)"
    r"|historical\s+outcomes",
    re.IGNORECASE)

# A bare role label on its own line, e.g. "Group CEO" / "Chief Executive Officer".
CEO_LABEL = re.compile(
    r"^\s*(the\s+)?(group\s+ceo|chief\s+executive(\s+officer)?|ceo)\b[^0-9]*$",
    re.IGNORECASE)
# The role named within a row that also carries figures.
CEO_INLINE = re.compile(
    r"(chief\s+executive|group\s+ceo|\bCEO\b)", re.IGNORECASE)
PART_YEAR = re.compile(r"\(\s*for\s+part[- ]?year\s*\)|part[- ]year", re.IGNORECASE)
MONEY = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{1,4}\b")

# How far a row may sit from the caption that introduces its table. Wide
# enough for a caption paragraph above a table, tight enough not to reach an
# unrelated table further down the page.
CAPTION_WINDOW = 60
ROLE_HEADER_WINDOW = 3


@dataclass
class DisclosedRow:
    page: int | None
    line_no: int
    text: str
    part_year: bool

    def describe(self) -> str:
        flag = " (part-year CEO)" if self.part_year else ""
        pg = f"p.{self.page}" if self.page is not None else "p.?"
        return f"{pg}{flag}: {self.text[:140]}"


def disclosed_ceo_rows(page_text: str, page_no: int | None = None,
                       min_money: int = 100_000) -> list[DisclosedRow]:
    """CEO rows inside a realised-remuneration disclosure on this page.

    `min_money` filters out years, percentages and share counts, which share the
    row format. It is a floor, not a total: a genuine part-year CEO figure is
    well above it, and a real annual CEO figure is higher still.
    """
    if not page_text or not REALISED_CAPTION.search(page_text):
        return []
    lines = page_text.split("\n")
    caption_at = [i for i, l in enumerate(lines) if REALISED_CAPTION.search(l)]
    if not caption_at:
        return []

    out: list[DisclosedRow] = []
    last_role = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if len(s) <= 60 and CEO_LABEL.match(s):
            last_role = i
            continue
        if len(s) > 260 or not MONEY.search(s):
            continue

        is_ceo = bool(CEO_INLINE.search(s))
        if (not is_ceo and last_role is not None
                and 0 < i - last_role <= ROLE_HEADER_WINDOW):
            is_ceo = bool(CEO_LABEL.match(lines[last_role].strip()))
        if not is_ceo:
            continue
        if not any(abs(c - i) <= CAPTION_WINDOW for c in caption_at):
            continue

        money = [int(m.replace(",", ""))
                 for m in re.findall(r"\b\d{1,3}(?:,\d{3})+\b", s)]
        if not money or max(money) < min_money:
            continue
        out.append(DisclosedRow(page_no, i, s, bool(PART_YEAR.search(s))))
    return out


def disclosed_realised_figures(pages_by_number: dict) -> list[DisclosedRow]:
    """Every disclosed realised CEO row across a page set."""
    out: list[DisclosedRow] = []
    for page_no in sorted(pages_by_number):
        out.extend(disclosed_ceo_rows(pages_by_number[page_no], page_no))
    return out
