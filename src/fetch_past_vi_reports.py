import os
import re
from datetime import datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv, find_dotenv
from logging_utils import write_supporting_json, write_supporting_text

# Load environment variables
load_dotenv(find_dotenv())

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

ASA_BASE_URL = "https://www.australianshareholders.com.au"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.australianshareholders.com.au/",
}


def _fetch_from_company_page(ticker: str, vi_dir: Path, target_years: set, downloaded: dict) -> None:
    """Scrape a single ASA company page for VI Reports in target_years."""
    company_url = f"{ASA_BASE_URL}/company/{ticker.lower()}/"
    print(f"  Scraping: {company_url}")

    try:
        response = requests.get(company_url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"    [WARN] Error connecting to ASA for {ticker}: {e}")
        return

    if response.status_code != 200:
        print(f"    [WARN] Company page returned HTTP {response.status_code} for {ticker}")
        return

    soup = BeautifulSoup(response.text, "html.parser")

    vi_section = soup.find("div", class_="voting-intentions")
    if not vi_section:
        return

    links = vi_section.find_all("a", href=True)

    for link in links:
        href = link["href"]
        link_text = link.get_text(strip=True)

        year_match = re.search(r"(\d{4})", link_text)
        if not year_match:
            continue

        year = int(year_match.group(1))

        if year not in target_years:
            continue

        if year in downloaded:
            continue

        pdf_url = href if href.startswith("http") else ASA_BASE_URL + href

        print(f"  Downloading: {link_text} -> {pdf_url}")

        try:
            pdf_response = requests.get(pdf_url, headers=HEADERS, timeout=60)
            if pdf_response.status_code != 200:
                print(f"    [WARN] HTTP {pdf_response.status_code} for {pdf_url}")
                continue

            if not pdf_response.content.startswith(b"%PDF"):
                print(f"    [WARN] Not a PDF (content-type mismatch)")
                continue

            filename = f"{ticker}_{year}_VIR.pdf"
            file_path = vi_dir / filename
            with open(file_path, "wb") as f:
                f.write(pdf_response.content)

            size_kb = round(len(pdf_response.content) / 1024, 1)
            print(f"    Saved: {filename} ({size_kb} KB) [Year: {year}]")
            downloaded[year] = file_path

        except Exception as e:
            print(f"    [ERROR] Failed to download: {e}")


def fetch_vi_reports(ticker: str, workspace_dir: Path, target_year: int, years_back: int = 5, legacy_codes: list = None, supporting_data_dir: Path = None, logger=None) -> bool:
    """
    Finds and downloads past VI (Voting Intentions) Reports from the ASA website
    for the given company. If the company has changed ASX code, legacy_codes
    (discovered by fetch_annual_reports.py) should be passed so VI Reports under
    the old code are also retrieved.

    The search window is [target_year - years_back, target_year - 1], so the
    current AGM year's VI Report is never downloaded.

    Returns True if at least one report was downloaded, False otherwise.
    Saves PDF files to workspace_dir/vi_reports/{TICKER}_{YYYY}_VIR.pdf
    """
    ticker = ticker.upper()
    target_years = set(range(target_year - years_back, target_year))

    vi_dir = workspace_dir / "vi_reports"
    vi_dir.mkdir(parents=True, exist_ok=True)

    codes_to_try = [ticker]
    if legacy_codes:
        for code in legacy_codes:
            if code.upper() not in codes_to_try:
                codes_to_try.append(code.upper())

    downloaded = {}

    for code in codes_to_try:
        _fetch_from_company_page(code, vi_dir, target_years, downloaded)

    if not downloaded:
        print(f"  No VI Reports found for target years {sorted(target_years)}")
        if len(codes_to_try) > 1:
            print(f"  Tried codes: {codes_to_try}")
        if logger:
            logger.warning(f"No VI Reports found for target years {sorted(target_years)}. Tried codes: {codes_to_try}")
        if supporting_data_dir:
            write_supporting_json(supporting_data_dir, "vi_reports_summary.json", {
                "codes_tried": codes_to_try,
                "target_years": sorted(target_years),
                "downloaded": {},
                "status": "none_found"
            })
        return False

    missing = target_years - set(downloaded.keys())
    if missing:
        print(f"  [Notice] Could not find VI Reports for years: {sorted(missing)}")
        if logger:
            logger.warning(f"VI Reports missing for years: {sorted(missing)}")
    else:
        print(f"  All {len(target_years)} target years acquired.")
        if logger:
            logger.info(f"All {len(target_years)} target years acquired.")

    print(f"  VI Reports downloaded: {len(downloaded)}")
    if logger:
        logger.info(f"VI Reports downloaded: {len(downloaded)}")

    if supporting_data_dir:
        write_supporting_json(supporting_data_dir, "vi_reports_summary.json", {
            "codes_tried": codes_to_try,
            "target_years": sorted(target_years),
            "downloaded": {year: str(path.name) for year, path in downloaded.items()},
            "missing_years": sorted(missing),
            "status": "partial" if missing else "complete"
        })

    return True


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2:
        ticker_arg = sys.argv[1]
    else:
        ticker_arg = "BHP"

    target_year_arg = int(sys.argv[2]) if len(sys.argv) > 2 else datetime.now().year
    workspace = DATA_DIR / "companies" / f"{ticker_arg.upper()}_{target_year_arg}"

    success = fetch_vi_reports(ticker=ticker_arg, workspace_dir=workspace, target_year=target_year_arg)
    if success:
        print(f"\nDone: VI Reports saved to {workspace / 'vi_reports'}")
    else:
        print("\nDone: No VI Reports were downloaded.")

    raise SystemExit(0 if success else 1)
