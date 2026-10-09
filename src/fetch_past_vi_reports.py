import re
from datetime import datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv, find_dotenv
from logging_utils import write_supporting_json

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
    """Scrape a single ASA company page for VI, AGM and EGM reports in target_years.

    The company page keeps the two report families in sibling item divs:
      div.voting-intentions -> "Voting Intentions Report YYYY"
      div.meeting-reports   -> "AGM Report YYYY" / "EGM Report YYYY"

    `downloaded` is keyed by (year, report_type) so a VI report for a year
    does not mask the AGM/EGM report for the same year.
    """
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

    # (div class, fixed report type); None means the type is read from the link text.
    sections = [
        ("voting-intentions", "VI"),
        ("meeting-reports", None),
    ]

    for class_name, fixed_type in sections:
        section = soup.find("div", class_=class_name)
        if not section:
            continue

        for link in section.find_all("a", href=True):
            href = link["href"]
            link_text = link.get_text(strip=True)

            year_match = re.search(r"(\d{4})", link_text)
            if not year_match:
                continue

            year = int(year_match.group(1))

            if year not in target_years:
                continue

            if fixed_type:
                report_type = fixed_type
            else:
                report_type = "EGM" if re.search(r"\bEGM\b", link_text, re.IGNORECASE) else "AGM"

            key = (year, report_type)
            if key in downloaded:
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

                # VI keeps the established VIR suffix; AGM/EGM use their own.
                suffix = "VIR" if report_type == "VI" else report_type
                filename = f"{ticker}_{year}_{suffix}.pdf"
                file_path = vi_dir / filename
                with open(file_path, "wb") as f:
                    f.write(pdf_response.content)

                size_kb = round(len(pdf_response.content) / 1024, 1)
                print(f"    Saved: {filename} ({size_kb} KB) [Type: {report_type}, Year: {year}]")
                downloaded[key] = file_path

            except Exception as e:
                print(f"    [ERROR] Failed to download: {e}")


def fetch_vi_reports(ticker: str, workspace_dir: Path, target_year: int, years_back: int = 5, legacy_codes: list = None, supporting_data_dir: Path = None, logger=None) -> bool:
    """
    Finds and downloads past VI (Voting Intentions) Reports plus the ASA
    AGM/EGM reports from the ASA website for the given company. If the company
    has changed ASX code, legacy_codes (discovered by fetch_annual_reports.py)
    should be passed so reports under the old code are also retrieved.

    The search window is [target_year - years_back, target_year - 1], so the
    current AGM year's reports are never downloaded.

    Returns True if at least one report was downloaded, False otherwise.
    Saves PDF files to workspace_dir/vi_reports/{TICKER}_{YYYY}_VIR.pdf,
    {TICKER}_{YYYY}_AGM.pdf and {TICKER}_{YYYY}_EGM.pdf.
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

    vi_downloaded = {year: path for (year, rtype), path in downloaded.items() if rtype == "VI"}
    agm_downloaded = {year: path for (year, rtype), path in downloaded.items() if rtype == "AGM"}
    egm_downloaded = {year: path for (year, rtype), path in downloaded.items() if rtype == "EGM"}

    if not downloaded:
        print(f"  No VI/AGM/EGM Reports found for target years {sorted(target_years)}")
        if len(codes_to_try) > 1:
            print(f"  Tried codes: {codes_to_try}")
        if logger:
            logger.warning(f"No reports found for target years {sorted(target_years)}. Tried codes: {codes_to_try}")
        if supporting_data_dir:
            write_supporting_json(supporting_data_dir, "vi_reports_summary.json", {
                "codes_tried": codes_to_try,
                "target_years": sorted(target_years),
                "downloaded": {},
                "agm_downloaded": {},
                "egm_downloaded": {},
                "missing_years": sorted(target_years),
                "status": "none_found"
            })
        return False

    # Missing-year reporting and status stay VI-based: not every company
    # publishes an AGM/EGM report in every year, so gaps there are normal.
    missing = target_years - set(vi_downloaded)
    if missing:
        print(f"  [Notice] Could not find VI Reports for years: {sorted(missing)}")
        if logger:
            logger.warning(f"VI Reports missing for years: {sorted(missing)}")
    else:
        print(f"  All {len(target_years)} target years have VI Reports.")
        if logger:
            logger.info(f"All {len(target_years)} target years have VI Reports.")

    print(f"  Reports downloaded: {len(vi_downloaded)} VI, {len(agm_downloaded)} AGM, {len(egm_downloaded)} EGM")
    if logger:
        logger.info(f"Reports downloaded: {len(vi_downloaded)} VI, {len(agm_downloaded)} AGM, {len(egm_downloaded)} EGM")

    if supporting_data_dir:
        write_supporting_json(supporting_data_dir, "vi_reports_summary.json", {
            "codes_tried": codes_to_try,
            "target_years": sorted(target_years),
            "downloaded": {str(year): path.name for year, path in vi_downloaded.items()},
            "agm_downloaded": {str(year): path.name for year, path in agm_downloaded.items()},
            "egm_downloaded": {str(year): path.name for year, path in egm_downloaded.items()},
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
        print(f"\nDone: VI/AGM/EGM Reports saved to {workspace / 'vi_reports'}")
    else:
        print("\nDone: No reports were downloaded.")

    raise SystemExit(0 if success else 1)
