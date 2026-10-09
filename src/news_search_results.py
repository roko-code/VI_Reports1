"""
news_search_results.py

Helper module for retrieving pre-fetched news search results during
pipeline stages (primarily Stage 9: Board Questions).

The news results are fetched by Stage 7.5 (news_prefetch) and stored in:
  {workspace}/supporting_data/stage7_5_news_prefetch/news_search_results.txt

This module provides `load_news_block` which reads that file so downstream
stages don't need to know the on-disk layout.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_news_file(workspace_dir: Path) -> Path | None:
    """Locate the saved news_search_results.txt file in supporting_data."""
    supporting_data_dir = workspace_dir / "supporting_data"
    if not supporting_data_dir.exists():
        return None

    # Search all stage subdirectories for the file
    for stage_dir in sorted(supporting_data_dir.iterdir(), reverse=True):
        if stage_dir.is_dir():
            candidate = stage_dir / "news_search_results.txt"
            if candidate.exists():
                return candidate

    # Also check reports directory
    reports_news = workspace_dir / "reports"
    if reports_news.exists():
        for f in reports_news.glob("*News_Search_Results.txt"):
            return f

    return None


def load_news_block(workspace_dir: Path) -> str:
    """
    Load the formatted NEWS SEARCH RESULTS block for a company.

    Args:
        workspace_dir: Path to the company workspace (e.g. data/companies/SUN_2026)

    Returns:
        The formatted news block string. If no results file is found,
        returns a standard "not available" message that the board-questions
        prompt expects to handle.
    """
    workspace_dir = Path(workspace_dir)
    news_file = _find_news_file(workspace_dir)

    if news_file and news_file.exists():
        try:
            content = news_file.read_text(encoding="utf-8")
            logger.info(f"Loaded news block from {news_file}")
            return content
        except OSError as e:
            logger.warning(f"Could not read news results file {news_file}: {e}")

    logger.warning(f"No news results found for workspace {workspace_dir.name}")
    return "NEWS SEARCH RESULTS\n\n(No news results were available for this company — news prefetch was not run or skipped due to missing configuration.)\n"


def has_news_results(workspace_dir: Path) -> bool:
    """Check if news results exist for a workspace."""
    return _find_news_file(workspace_dir) is not None
