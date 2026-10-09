"""
news_prefetch.py

Pre-fetches recent news for a company using a small set of fixed,
concern-oriented search queries, and formats the results as a plain-text
"NEWS SEARCH RESULTS" block for the board-questions LLM prompt.

Design notes:
- Uses Tavily's REST API directly (no SDK dependency) because it has a
  "news" topic with a simple day-window filter, which is exactly what's
  needed for "material news in the past 12 months". Swapping to Serper,
  Bing, or another provider only requires rewriting `_run_single_query`
  and the two field names read off its response (title/url/snippet/date) —
  everything else (query list, dedup, formatting) is provider-agnostic.
- Each query is run independently and failures are isolated: one query
  timing out or erroring does not abort the batch.
- No relevance/company-match filtering is done here. That's deliberate —
  the board-questions prompt is explicitly instructed to verify each
  item is actually about the right company before using it, since that
  judgement needs the full company name/ticker context the LLM has and
  a search API alone can get wrong (similarly-named companies, wrong
  exchange, etc).

Usage (once wired into the orchestrator):

    results = fetch_company_news(
        company_name="ALS Limited",
        ticker="ALQ",
        api_key=os.environ["TAVILY_API_KEY"],
    )
    news_block = format_news_block(results, company_name="ALS Limited", days=365)
    # news_block is the text you splice into the board-questions prompt
    # payload as the "NEWS SEARCH RESULTS" input.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from dotenv import load_dotenv, find_dotenv
from logging_utils import write_supporting_json, write_supporting_text, TechnicalLogger

load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

# Fixed, concern-oriented query templates. {company} is replaced with the
# company's full legal/listed name, quoted so multi-word names stay as a
# phrase. Add to this list as you learn what turns up useful results and
# what turns up noise for your company universe — it's meant to be tuned,
# not treated as final.
DEFAULT_QUERY_TEMPLATES: list[str] = [
    '"{company}" ASX lawsuit',
    '"{company}" regulator investigation',
    '"{company}" class action',
    '"{company}" data breach OR cyber attack',
    '"{company}" recall OR safety incident',
    '"{company}" downgrade OR profit warning',
    '"{company}" resignation OR "steps down"',
    '"{company}" whistleblower',
    '"{company}" fine OR penalty',
    '"{company}" fraud OR misconduct',
]


@dataclass
class NewsResult:
    query: str
    title: str
    snippet: str
    url: str
    date: Optional[str]  # ISO date string if the provider gave one, else None
    source: str  # domain name, derived from the URL


def _domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _run_single_query(
    query: str,
    api_key: str,
    days: int,
    max_results_per_query: int,
    timeout_seconds: float,
    logger=None,
) -> list[NewsResult]:
    """Run one query against Tavily's news search and return parsed results.

    Isolated try/except: any failure (network error, bad response, rate
    limit) is logged and results in an empty list for this query rather
    than raising, so one bad query never aborts the whole batch. ``logger``
    is passed explicitly (not read from module global) so this is safe to
    call from worker threads.
    """
    payload = {
        "api_key": api_key,
        "query": query,
        "topic": "news",
        "days": days,
        "max_results": max_results_per_query,
        "include_answer": False,
        "include_raw_content": False,
    }

    try:
        response = requests.post(TAVILY_SEARCH_URL, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        if logger:
            logger.warning("News search failed for query %r: %s", query, exc)
        return []
    except ValueError as exc:
        if logger:
            logger.warning("News search returned unparseable JSON for query %r: %s", query, exc)
        return []

    raw_results = data.get("results", []) or []
    parsed: list[NewsResult] = []
    for item in raw_results:
        url = item.get("url", "")
        parsed.append(
            NewsResult(
                query=query,
                title=(item.get("title") or "").strip(),
                snippet=(item.get("content") or "").strip(),
                url=url,
                date=item.get("published_date"),
                source=_domain_from_url(url),
            )
        )
    return parsed


def fetch_company_news(
    company_name: str,
    ticker: Optional[str] = None,
    api_key: Optional[str] = None,
    days: int = 365,
    max_results_per_query: int = 5,
    max_total_results: int = 30,
    query_templates: Optional[list[str]] = None,
    timeout_seconds: float = 15.0,
    logger=None,
) -> list[NewsResult]:
    """Run the fixed query set for one company and return deduplicated results.

    Args:
        company_name: Full legal/listed name, used to fill query templates.
        ticker: Optional ASX ticker. Not currently used to build queries
            (short tickers are too noisy as bare search terms), but kept
            as a parameter since you'll likely want it for logging/context
            and may want ticker-specific queries later.
        api_key: Tavily API key.
        days: How far back to search. Defaults to 365 (the "past 12
            months" window the board-questions prompt expects).
        max_results_per_query: Cap per individual query.
        max_total_results: Cap on the deduplicated, combined result set
            across all queries, most-recent-first. Keeps the block you
            hand to the LLM from growing unbounded if a company has a
            lot of news.
        query_templates: Override the default query list if needed.
        timeout_seconds: Per-query HTTP timeout.

    Returns:
        Deduplicated list of NewsResult, sorted most-recent-first (items
        with no parseable date are sorted last, not dropped).
    """
    if not api_key:
        raise ValueError("api_key is required (Tavily API key)")

    templates = query_templates or DEFAULT_QUERY_TEMPLATES
    queries = [t.format(company=company_name) for t in templates]

    # Run queries concurrently (independent network calls, no shared mutable
    # state). Tavily's rate limit is per-key, not per-connection, and these are
    # short queries; a modest pool turns the ~10s serial walk into ~2-3s.
    all_results: list[NewsResult] = []
    max_workers = min(10, len(queries)) if queries else 1
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_query = {
            pool.submit(
                _run_single_query,
                query=query,
                api_key=api_key,
                days=days,
                max_results_per_query=max_results_per_query,
                timeout_seconds=timeout_seconds,
                logger=logger,
            ): query
            for query in queries
        }
        for fut in as_completed(future_to_query):
            all_results.extend(fut.result())

    deduped = _dedupe_by_url(all_results)
    ordered = _sort_most_recent_first(deduped)
    return ordered[:max_total_results]


def _dedupe_by_url(results: list[NewsResult]) -> list[NewsResult]:
    seen: set[str] = set()
    deduped: list[NewsResult] = []
    for r in results:
        key = r.url.strip().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    # Try a few common formats; extend this if your provider returns
    # something else. Fall back to None (sorted last) rather than raising.
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _sort_most_recent_first(results: list[NewsResult]) -> list[NewsResult]:
    def sort_key(r: NewsResult):
        parsed = _parse_date(r.date)
        # Items with no parseable date sort after all dated items.
        return (parsed is None, -(parsed.timestamp()) if parsed else 0)

    return sorted(results, key=sort_key)


def format_news_block(
    results: list[NewsResult],
    company_name: str,
    days: int = 365,
) -> str:
    """Render results as the plain-text block the board-questions prompt expects.

    Matches the shape described in that prompt: each entry has a headline,
    a short snippet, a source, a publication date, and a URL.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = (
        "NEWS SEARCH RESULTS\n"
        f"Company searched: {company_name}\n"
        f"Search window: past {days} days (generated {generated_at})\n"
        f"Results returned: {len(results)}\n"
    )

    if not results:
        return header + "\n(No results returned by any query.)\n"

    lines = [header]
    for i, r in enumerate(results, start=1):
        date_display = r.date or "date unknown"
        title = r.title or "(no title)"
        snippet = r.snippet or "(no snippet available)"
        lines.append(
            f"\n{i}. [{date_display}] {r.source} — {title}\n"
            f"   {snippet}\n"
            f"   URL: {r.url}\n"
            f'   (matched query: "{r.query}")'
        )

    return "\n".join(lines) + "\n"


def _extract_company_name(workspace_dir: Path) -> str:
    """Extract company name from NoM JSON in the workspace."""
    nom_dirs = [
        workspace_dir / "extracted_json" / "notices",
        workspace_dir / "extracted_json" / "notices_archived",
    ]
    for nom_dir in nom_dirs:
        if nom_dir.exists():
            for json_file in nom_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        nom_data = json.load(f)
                    company_name = (
                        nom_data.get("AGM_Details", {}).get("company_name")
                        or nom_data.get("company_name")
                    )
                    if company_name:
                        return company_name
                except (json.JSONDecodeError, OSError):
                    continue
    return ""


def run_news_prefetch(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None,
) -> bool:
    """
    Run news prefetching for a company.

    Extracts the company name from the NoM JSON in the workspace, then
    runs the concern-oriented news search queries via Tavily, saving
    both the raw results (JSON) and a formatted text block for downstream
    consumption by the board-questions stage.

    Returns True if the process completed (even if no results were found),
    False if a critical error occurred (e.g. no API key, can't find company name).
    """
    if logger is None:
        logger = TechnicalLogger(Path("technical_log.md"))

    workspace_dir = Path(workspace_dir)
    supporting_data_dir = Path(supporting_data_dir)
    supporting_data_dir.mkdir(parents=True, exist_ok=True)

    # Extract ticker and year from directory name (pattern: TICKER_YEAR)
    workspace_name = workspace_dir.name
    match = re.match(r"([A-Z]+)_(\d{4})", workspace_name)
    if match:
        ticker = match.group(1)
        year = int(match.group(2))
    else:
        ticker = workspace_name.split("_")[0]
        try:
            year = int(workspace_name.split("_")[1])
        except (ValueError, IndexError):
            year = datetime.now().year

    logger.stage_start(f"Stage 7.5: News Prefetch for {ticker} FY{year}")

    # Extract company name from NoM JSON
    company_name = _extract_company_name(workspace_dir)
    if not company_name:
        logger.warning(f"Could not extract company name from NoM JSON for {ticker}")
        # Fall back to using ticker as search term
        company_name = ticker

    logger.info(f"Company name: {company_name}")

    # Get API key
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        logger.error("TAVILY_API_KEY not found in environment")
        print("[Stage 7.5] TAVILY_API_KEY not found. Skipping news prefetch.")
        write_supporting_text(supporting_data_dir, "news_prefetch_status.txt",
                              f"SKIPPED: No TAVILY_API_KEY configured\nCompany: {company_name}\nTicker: {ticker}")
        # Return False so the news_prefetched flag stays unset: a workspace
        # processed while the key was misconfigured would otherwise be flagged
        # complete forever and never backfill its news once the key exists.
        return False

    # Run the news search
    days = int(os.getenv("NEWS_FETCH_DAYS", "365"))
    max_results = int(os.getenv("NEWS_MAX_RESULTS_PER_QUERY", "5"))
    max_total = int(os.getenv("NEWS_MAX_TOTAL_RESULTS", "30"))

    logger.info(f"Running news search for '{company_name}' over past {days} days")

    search_start = time.time()
    news_results = fetch_company_news(
        company_name=company_name,
        ticker=ticker,
        api_key=api_key,
        days=days,
        max_results_per_query=max_results,
        max_total_results=max_total,
        logger=logger,
    )
    search_elapsed = round(time.time() - search_start, 2)

    logger.info(f"News search completed in {search_elapsed}s, {len(news_results)} results")

    # Save raw results as JSON
    raw_results = [asdict(r) for r in news_results]
    write_supporting_json(supporting_data_dir, "news_search_raw.json", raw_results)

    # Save formatted text block for downstream LLM consumption
    news_block = format_news_block(news_results, company_name=company_name, days=days)
    write_supporting_text(supporting_data_dir, "news_search_results.txt", news_block)

    # Also save to reports directory for easy access
    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{ticker}_{year}_News_Search_Results.txt").write_text(
        news_block, encoding="utf-8"
    )

    # Save metadata
    metadata = {
        "ticker": ticker,
        "year": year,
        "company_name": company_name,
        "search_query_count": len(DEFAULT_QUERY_TEMPLATES),
        "results_returned": len(news_results),
        "search_window_days": days,
        "search_elapsed_seconds": search_elapsed,
        "searched_at": datetime.now(timezone.utc).isoformat(),
    }
    write_supporting_json(supporting_data_dir, "news_prefetch_metadata.json", metadata)

    logger.stage_end(
        f"Stage 7.5: News Prefetch for {ticker} FY{year}",
        True,
        summary=f"Found {len(news_results)} news results in {search_elapsed}s"
    )

    return True


# Alias for orchestrator compatibility
run_pipeline = run_news_prefetch


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        print("Set TAVILY_API_KEY to run this smoke test.", file=sys.stderr)
        raise SystemExit(1)

    test_company = sys.argv[1] if len(sys.argv) > 1 else "ALS Limited"
    test_results = fetch_company_news(company_name=test_company, api_key=key, days=365)
    print(format_news_block(test_results, company_name=test_company, days=365))