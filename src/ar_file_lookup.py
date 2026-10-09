"""Shared Annual Report PDF file lookup.

Before this module, four stage modules each carried their own copy of
"find the AR PDF for a year", and the copies disagreed on fallback policy:

* remuneration_assess, board_questions, text_summaries: exact-year only.
* other_resolutions_assess: exact year, then most-recent-fallback.
* director_assess: ticker-prefix match, then year-descending sort.

This module is the single source of truth. Callers that intentionally need
exact-match semantics (e.g. comparing the current year to the prior year) pass
`fallback=False`; the rest get the most-recent-available fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


AR_DIR_NAME = "annual_reports"
_YEAR_RE = re.compile(r"(\d{4})")


@dataclass
class ARLookup:
    """Result of looking up an Annual Report PDF."""

    path: Path
    year: int
    is_fallback: bool  # True when the requested year was absent and a
    # more-recent-or-equal AR was substituted.


def _year_of(path: Path) -> Optional[int]:
    match = _YEAR_RE.search(path.stem)
    return int(match.group(1)) if match else None


def find_ar_file(
    workspace_dir: Path,
    year: int,
    *,
    fallback: bool = True,
    ar_dir_name: str = AR_DIR_NAME,
    logger=None,
) -> Optional[ARLookup]:
    """Find the AR PDF for ``year`` in ``workspace_dir/<ar_dir_name>``.

    Returns an :class:`ARLookup`, or None if no AR exists at all.

    When ``fallback`` is True (the default) and no AR for the exact ``year``
    is present, the most recent available AR is returned with ``is_fallback``
    set to True — used when "latest available remuneration disclosure" is an
    acceptable stand-in (Stages 5/8/9 other-doc lookups, Stage 6/9 doc 6).

    When ``fallback`` is False, only the exact year is considered. Used by
    pairs of lookups that must be comparable (the FY-N vs FY-N-1 pair used by
    Stage 5's prior-year comparison and Stage 9's prior-VI lookup).
    """
    ar_dir = Path(workspace_dir) / ar_dir_name
    candidates = [p for p in ar_dir.glob("*.pdf") if _year_of(p) is not None] if ar_dir.is_dir() else []

    exact = [p for p in candidates if _year_of(p) == year]
    if exact:
        return ARLookup(exact[0], year, is_fallback=False)

    if not fallback:
        return None

    # Fallback: most recent available AR.
    if candidates:
        newest = max(candidates, key=lambda p: _year_of(p))
        new_year = _year_of(newest)
        if logger is not None:
            logger.warning(
                f"No AR for FY{year} in {ar_dir.name}; using most-recent "
                f"FY{new_year} ({newest.name}) as fallback"
            )
        return ARLookup(newest, new_year, is_fallback=True)

    return None


def parse_year_from_stem(stem: str) -> Optional[int]:
    """Extract a 4-digit year from a filename-like stem (e.g. NAB_2026_NOM -> 2026)."""
    match = _YEAR_RE.search(stem)
    return int(match.group(1)) if match else None
