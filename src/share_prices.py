"""
share_prices.py

Fetches year-end share prices from Yahoo Finance's public (unauthenticated)
chart endpoint to back-fill the `share_price` and `simple_tsr` metrics that
many Annual Reports do not publish as a multi-year history (Macquarie's
five-year summary carries NPAT/EPS/DPS/ROE but no share-price column).

Design notes:
- Uses Yahoo's chart end-point directly (no SDK) - the same one `yfinance`
  wraps - because it needs no API key. A User-Agent header is sent; Yahoo
  rate-limits abusive bursts but a handful of symbols fetch cleanly.
- ASX symbols are `{TICKER}.AX`. Dual-listed issuers (RIO) are fetched on
  their `.AX` line, which is the ASA-relevant listing.
- The FY-end date differs by issuer (MQG 31 Mar, SUN/CSL 30 Jun, NAB 30 Sep,
  RIO 31 Dec). It is never hard-coded: it is detected from the Annual Report
  ("financial year ended 31 March 2026") so a new issuer self-configures.
- Results are cached per ticker under data/_reference/share_prices/, because a
  historical close is an immutable fact shared across every AGM year produced
  for that ticker. A completed cache is not re-fetched.
- A missing close (delisted, suspended, no trade on the date) yields None, and
  is reported honestly rather than guessed; TSR is only computed when both the
  start and end year closes exist.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Optional

import requests

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

BASE_DIR = Path(__file__).resolve().parent.parent
SHARE_PRICES_DIR = BASE_DIR / "data" / "_reference" / "share_prices"

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VI-reports/1.0)"}

_FY_END_RE = re.compile(
    r"(?:financial\s+year\s+ended|for\s+the\s+year\s+ended|year\s+ended)\s+"
    r"(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"], start=1)}


def yahoo_symbol(ticker: str) -> str:
    return f"{ticker.upper()}.AX"


def detect_fy_end_month_day(ar_text: str) -> Optional[tuple[int, int]]:
    """(month, day) of the FY-end from the AR cover text, else None."""
    m = _FY_END_RE.search(ar_text or "")
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    return (month, day)


def _fy_end_date(year: int, month: int, day: int) -> dt.date:
    """The FY-end date for `year`, clamped to a valid calendar day."""
    while True:
        try:
            return dt.date(year, month, day)
        except ValueError:
            day -= 1


def fetch_daily_closes(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, float | None]]:
    """Daily (date, close) series. Empty list on failure (no raise - missing
    data is handled by the caller)."""
    p1 = int(dt.datetime(start.year, start.month, start.day).timestamp())
    p2 = int(dt.datetime(end.year, end.month, end.day, 23, 59, 59).timestamp())
    params = {"period1": p1, "period2": p2, "interval": "1d"}
    try:
        r = requests.get(f"{YAHOO_CHART_URL}/{symbol}", params=params,
                         headers=_HEADERS, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
    except Exception:
        return []
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote", [{}])[0].get("close")) or []
    out = []
    tz = dt.timezone.utc
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((dt.datetime.fromtimestamp(t, tz).date(), float(c)))
    return out


def _closest_on_or_before(closes, target: dt.date, window_days: int = 10) -> Optional[float]:
    best = None
    best_date = None
    floor = target - dt.timedelta(days=window_days)
    for d, c in closes:
        if floor <= d <= target:
            if best_date is None or d > best_date:
                best, best_date = c, d
    return best


def get_fy_closes(ticker: str, fy_end_month: int, fy_end_day: int,
                  years: list[int]) -> dict[int, Optional[float]]:
    """Year-end closes for `years`, cached per ticker. Returns {year: close
    or None}."""
    SHARE_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = SHARE_PRICES_DIR / f"{ticker.upper()}_fy_closes.json"
    cached: dict = {}
    if cache_path.exists():
        try:
            cached = {int(k): v for k, v in json.loads(cache_path.read_text(encoding="utf-8")).items()}
        except Exception:
            cached = {}

    result: dict[int, Optional[float]] = dict(cached)
    missing = [y for y in years if y not in result]
    if missing:
        symbol = yahoo_symbol(ticker)
        for y in missing:
            target = _fy_end_date(y, fy_end_month, fy_end_day)
            closes = fetch_daily_closes(symbol, target - dt.timedelta(days=15), target + dt.timedelta(days=5))
            result[y] = _closest_on_or_before(closes, target)
        # persist back (int keys -> str for JSON)
        json.dump({str(k): v for k, v in sorted(result.items())},
                  cache_path.open("w", encoding="utf-8"), indent=2)
    return result


def compute_simple_tsr(closes: dict[int, Optional[float]]) -> dict[int, Optional[float]]:
    """Simple TSR (%) for years that have both a start and end close."""
    years = sorted(closes)
    tsr: dict[int, Optional[float]] = {}
    for i in range(1, len(years)):
        prev, cur = closes[years[i - 1]], closes[years[i]]
        if prev and cur:
            tsr[years[i]] = round((cur / prev - 1.0) * 100.0, 2)
        else:
            tsr[years[i]] = None
    return tsr