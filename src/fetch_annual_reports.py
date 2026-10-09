import io
import re
import sys
import time
from pathlib import Path
import pypdf
from playwright.sync_api import sync_playwright, Page, BrowserContext
from logging_utils import write_supporting_json

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "companies"

def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Returns the total number of pages in the PDF binary."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception as e:
        print(f"      [Warning] Could not parse PDF page count: {e}")
        return 0

def resolve_pdf_url(html_text: str) -> str | None:
    """Extracts direct PDF path from the ASX HTML wrapper page."""
    match = re.search(r'(/asxpdf/[^\s"\'<>]+)', html_text, re.IGNORECASE)
    if match:
        rel_url = match.group(1)
        return rel_url if rel_url.startswith("http") else f"https://www.asx.com.au{rel_url}"

    match = re.search(r'name=["\']pdfURL["\']\s+value=["\']([^"\']+)["\']', html_text, re.IGNORECASE) or \
            re.search(r'value=["\']([^"\']+)["\']\s+name=["\']pdfURL["\']', html_text, re.IGNORECASE)
    if match:
        rel_url = match.group(1)
        return rel_url if rel_url.startswith("http") else f"https://www.asx.com.au{rel_url}"

    match = re.search(r'(?:href|src)\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']', html_text, re.IGNORECASE)
    if match:
        rel_url = match.group(1)
        return rel_url if rel_url.startswith("http") else f"https://www.asx.com.au{rel_url}"

    return None

def detect_legacy_code_from_headline(headline: str, current_codes: list[str]) -> str | None:
    """Inline check to extract legacy ticker symbols from name/code change announcements."""
    if re.search(r"\b(?:Change of (?:Company )?Name|Change of (?:ASX )?Code|Name Change|Ticker Change)\b", headline, re.IGNORECASE):
        found_codes = re.findall(r"\b([A-Z0-9]{3})\b", headline)
        ignored = {"ASX", "NEW", "FOR", "AND", "THE", "LIMITED", "LTD"}
        for c in found_codes:
            c_upper = c.upper()
            if c_upper not in current_codes and c_upper not in ignored and not c_upper.isdigit():
                return c_upper
    return None

def safe_goto(page: Page, context: BrowserContext, url: str, max_retries: int = 3) -> Page:
    """Navigates to a URL with retry logic and page re-creation on HTTP2/network failures."""
    for attempt in range(1, max_retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return page
        except Exception as e:
            print(f"    [Network Retry {attempt}/{max_retries}] Failed loading {url}: {e}")
            time.sleep(2)
            try:
                page.close()
            except Exception:
                pass
            page = context.new_page()
    return page

def fetch_annual_reports(ticker: str, target_year: int, count: int = 5, supporting_data_dir: Path = None, logger=None) -> tuple:
    """Downloads the past `count` Annual Reports plus the latest Corporate
    Governance Statement (CGS) from ASX announcements.

    Annual reports are saved to data/companies/{TICKER}_{YEAR}/annual_reports/
    as {TICKER}_{YEAR}_AR.pdf. The CGS is a separate ASX announcement
    ("Appendix 4G" / "CGC 4G") and is searched for target_year only; it is
    saved to the sibling governance/ folder as {TICKER}_{YEAR}_CGS.pdf so it
    does not collide with the AR files consumed by the later stages. Many
    issuers embed the CGS in the AR and file no standalone announcement; in
    that case the lookup records not_found (assumed embedded) and does not
    fail the stage.

    Returns (success, all_codes) where success reflects the AR acquisition
    and all_codes may include legacy tickers discovered from name/code change
    announcements.
    """
    ticker = ticker.upper()
    all_codes = [ticker]

    company_dir = DATA_DIR / f"{ticker}_{target_year}"
    ar_dir = company_dir / "annual_reports"
    ar_dir.mkdir(parents=True, exist_ok=True)

    target_years = list(range(target_year - count + 1, target_year + 1))
    required_years = set(target_years)
    downloaded_files = {}
    # Cache of announcement rows by year so the CGS pass (which queries the
    # same target-year ASX page) does not fetch and parse it a second time.
    announcement_rows_cache: dict[int, list] = {}

    print("==========================================")
    print(f" [Pass 1] Playwright AR Scraper for [{ticker}]")
    print(f" Target Years: {sorted(list(required_years))}")
    print(f" Target Folder: {ar_dir.relative_to(BASE_DIR)}")
    print("==========================================")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True
        )
        page = context.new_page()

        for search_year in sorted(target_years, reverse=True):
            if not required_years:
                print("\nAll required 5 years of Annual Reports successfully acquired!")
                break

            code_idx = 0
            while code_idx < len(all_codes):
                current_code = all_codes[code_idx]
                code_idx += 1

                if search_year not in required_years:
                    break

                print(f"\n--> Querying historical announcements for year {search_year} using ticker [{current_code}]...")
                search_url = f"https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={current_code}&timeframe=Y&year={search_year}"
                print(f"    Search URL: {search_url}")

                page = safe_goto(page, context, search_url)

                try:
                    rows = page.locator("table tr").all()
                    # Cache the target-year rows for the CGS pass to avoid a
                    # duplicate fetch of the same ASX announcements page.
                    if search_year == target_year:
                        announcement_rows_cache[search_year] = rows
                    ar_candidates = []

                    for row in rows:
                        cols = row.locator("td").all()
                        if len(cols) >= 3:
                            headline_el = cols[2]
                            headline_text = headline_el.inner_text().strip()
                            link_tag = headline_el.locator("a")

                            # Inline ticker change detection
                            legacy_code = detect_legacy_code_from_headline(headline_text, all_codes)
                            if legacy_code:
                                print(f"    [Inline Detection] Discovered former ticker code [{legacy_code}] from headline: '{headline_text}'")
                                all_codes.append(legacy_code)

                            is_ar_title = re.search(r"\b(?:Annual Report|Annual Financial Report|Statutory Accounts)\b", headline_text, re.IGNORECASE)
                            is_excluded = re.search(
                                r"\b(?:Concise|Notice|Proxy|Presentation|Explanatory|Note|Notes|Addendum|Clarification|Erratum|Correction|Letter|Covering|Dividend|Extract)\b", 
                                headline_text, 
                                re.IGNORECASE
                            )

                            if link_tag.count() > 0 and is_ar_title and not is_excluded:
                                href = link_tag.get_attribute("href")
                                full_url = href if href.startswith("http") else f"https://www.asx.com.au{href}"
                                ar_candidates.append({
                                    "headline": headline_text,
                                    "url": full_url,
                                    "pub_year": search_year
                                })

                    print(f"    Found {len(ar_candidates)} candidate(s) for [{current_code}] in {search_year}.")

                    for cand in ar_candidates:
                        print(f"\n    --> Candidate: {cand['headline']}")
                        print(f"        Announcement URL: {cand['url']}")
                        
                        try:
                            res = context.request.get(cand["url"])
                            if not res.ok:
                                print(f"        FAILED: Announcement page returned HTTP {res.status}")
                                continue

                            pdf_bytes = res.body()
                            pdf_url = cand["url"]

                            if not pdf_bytes.startswith(b"%PDF"):
                                html_text = pdf_bytes.decode("utf-8", errors="ignore")
                                resolved_url = resolve_pdf_url(html_text)
                                
                                if resolved_url:
                                    pdf_url = resolved_url
                                    print(f"        Resolved Direct PDF URL: {pdf_url}")
                                    pdf_res = context.request.get(pdf_url)
                                    if pdf_res.ok:
                                        pdf_bytes = pdf_res.body()
                                else:
                                    page = safe_goto(page, context, cand["url"])
                                    pdf_input = page.locator('input[name="pdfURL"], input[id="pdfURL"]')
                                    if pdf_input.count() > 0:
                                        rel_path = pdf_input.first.get_attribute("value")
                                        pdf_url = rel_path if rel_path.startswith("http") else f"https://www.asx.com.au{rel_path}"
                                        print(f"        Resolved Direct PDF URL (via DOM): {pdf_url}")
                                        pdf_res = context.request.get(pdf_url)
                                        if pdf_res.ok:
                                            pdf_bytes = pdf_res.body()

                            if pdf_bytes.startswith(b"%PDF"):
                                total_pages = get_pdf_page_count(pdf_bytes)
                                
                                headline_year_match = re.search(r"\b(202[0-9])\b", cand["headline"])
                                if headline_year_match:
                                    headline_year = int(headline_year_match.group(1))
                                    # Headline may reference a fiscal-year range (e.g., "2024-2025 Annual Report")
                                    # published in the announcement year. Prefer the publication year.
                                    if headline_year == cand["pub_year"]:
                                        report_year = headline_year
                                    else:
                                        report_year = cand["pub_year"]
                                else:
                                    report_year = cand["pub_year"]

                                if total_pages < 10:
                                    print(f"        Skipping (Document has only {total_pages} page(s); likely an explanatory note).")
                                    continue

                                if report_year in required_years:
                                    file_name = ar_dir / f"{ticker}_{report_year}_AR.pdf"
                                    with open(file_name, "wb") as f:
                                        f.write(pdf_bytes)
                                    
                                    size_mb = round(len(pdf_bytes) / (1024 * 1024), 2)
                                    print(f"        SUCCESS: Saved {file_name.name} ({size_mb} MB, {total_pages} pages) [Year: {report_year}]")
                                    downloaded_files[report_year] = str(file_name)
                                    required_years.remove(report_year)
                                    break
                                else:
                                    print(f"        Skipping (Year {report_year} already downloaded or outside range).")
                            else:
                                print(f"        FAILED: Could not resolve valid PDF binary from {pdf_url}")

                        except Exception as e:
                            print(f"        Error processing candidate: {e}")
                        
                        time.sleep(1)

                except Exception as e:
                    print(f"    [Error] Failed processing year {search_year} for [{current_code}]: {e}")

                time.sleep(1.5)

        # ---- Corporate Governance Statement (latest = target_year only) ----
        # A standalone CGS is a separate ASX announcement ("Appendix 4G",
        # formerly "CGC 4G"). Many issuers embed the CGS in the Annual
        # Report instead; those file no standalone announcement, so a miss
        # is expected and is reported, not treated as a failure.
        cgs_dir = company_dir / "governance"
        cgs_dir.mkdir(parents=True, exist_ok=True)
        cgs_file = cgs_dir / f"{ticker}_{target_year}_CGS.pdf"
        cgs_found = False

        if cgs_file.exists():
            # Validate the reuse candidate: a zero-byte or corrupt file must
            # not be trusted on its way to the AR-derived CGS fallback.
            _peek = cgs_file.read_bytes()[:5]
            if _peek.startswith(b"%PDF"):
                print(f"\n[CGS] Already present and valid: {cgs_file.name}")
                cgs_found = True
            else:
                print(f"\n[CGS] Existing {cgs_file.name} is not a valid PDF; re-fetching.")
                cgs_file.unlink(missing_ok=True)
        else:
            for current_code in all_codes:
                print(f"\n--> Querying Corporate Governance Statement for FY{target_year} using ticker [{current_code}]...")
                search_url = f"https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={current_code}&timeframe=Y&year={target_year}"

                # Reuse the announcement rows already fetched during Pass 1 for
                # the target year (same ticker, same page) instead of refetching.
                if target_year in announcement_rows_cache and current_code == ticker:
                    rows = announcement_rows_cache[target_year]
                    print(f"    Reusing target-year announcement rows from cache (no duplicate fetch).")
                else:
                    print(f"    Search URL: {search_url}")
                    page = safe_goto(page, context, search_url)
                    rows = page.locator("table tr").all()

                try:
                    cgs_candidates = []
                    for row in rows:
                        cols = row.locator("td").all()
                        if len(cols) < 3:
                            continue
                        headline_el = cols[2]
                        headline_text = headline_el.inner_text().strip()
                        link_tag = headline_el.locator("a")
                        if link_tag.count() == 0:
                            continue
                        # A combined "Annual Report and Corporate Governance
                        # Statement" lodgement is the AR itself (already
                        # fetched above); skip it to avoid saving the AR
                        # under a CGS name.
                        if re.search(r"\b(?:Annual Report|Annual Financial Report)\b", headline_text, re.IGNORECASE):
                            continue
                        if re.search(r"(Appendix 4G|CGC 4G|Corporate Governance Statement)", headline_text, re.IGNORECASE):
                            href = link_tag.get_attribute("href")
                            full_url = href if href.startswith("http") else f"https://www.asx.com.au{href}"
                            cgs_candidates.append(full_url)

                    print(f"    Found {len(cgs_candidates)} CGS candidate(s) for [{current_code}] in {target_year}.")

                    for cand_url in cgs_candidates:
                        try:
                            res = context.request.get(cand_url)
                            if not res.ok:
                                print(f"    [WARN] Announcement page returned HTTP {res.status}")
                                continue

                            pdf_bytes = res.body()
                            if not pdf_bytes.startswith(b"%PDF"):
                                resolved_url = resolve_pdf_url(pdf_bytes.decode("utf-8", errors="ignore"))
                                if resolved_url:
                                    print(f"        Resolved Direct PDF URL: {resolved_url}")
                                    pdf_res = context.request.get(resolved_url)
                                    if pdf_res.ok:
                                        pdf_bytes = pdf_res.body()

                            if not pdf_bytes.startswith(b"%PDF"):
                                print(f"    [WARN] Could not resolve valid PDF binary from {cand_url}")
                                continue

                            with open(cgs_file, "wb") as f:
                                f.write(pdf_bytes)

                            size_mb = round(len(pdf_bytes) / (1024 * 1024), 2)
                            print(f"    SUCCESS: Saved {cgs_file.name} ({size_mb} MB) [Year: {target_year}]")
                            if logger:
                                logger.info(f"CGS saved: {cgs_file.name}")
                            cgs_found = True
                            break
                        except Exception as e:
                            print(f"    [WARN] CGS candidate failed: {e}")
                        time.sleep(1)

                    if cgs_found:
                        break
                except Exception as e:
                    print(f"    [Error] CGS search failed for year {target_year} [{current_code}]: {e}")
                time.sleep(1.5)

            if not cgs_found:
                print(f"    [Notice] No standalone Corporate Governance Statement found for FY{target_year}.")
                print("    Assuming the CGS is embedded in the Annual Report (common for some issuers).")
                if logger:
                    logger.warning(f"No standalone CGS found for FY{target_year}; assuming it is embedded in the AR.")

        browser.close()

    if supporting_data_dir:
        write_supporting_json(supporting_data_dir, "cgs_summary.json", {
            "ticker": ticker,
            "target_year": target_year,
            "file": cgs_file.name if cgs_found else None,
            "status": "downloaded" if cgs_found else "not_found",
            "assumed_embedded_in_ar": not cgs_found,
        })

    if required_years:
        print(f"\n[Notice] Could not find Annual Reports for years: {sorted(list(required_years))}")
        # If current year AR not yet published (company hasn't reported), 
        # accept partial download if at least 1 year was acquired
        if downloaded_files and (target_year in required_years):
            print(f"[Stage 2] Partial success: acquired {len(downloaded_files)} of {count} Annual Reports.")
            print(f"[Stage 2] Current year FY{target_year} not yet published - using most recent available.")
            return True, all_codes
        return False, all_codes

    print(f"\n[Stage 2] All {count} Annual Reports acquired successfully.")
    return True, all_codes

if __name__ == "__main__":
    ticker_arg = sys.argv[1] if len(sys.argv) > 1 else "DNL"
    year_arg = int(sys.argv[2]) if len(sys.argv) > 2 else 2026

    success, codes = fetch_annual_reports(ticker=ticker_arg, target_year=year_arg)
    if codes:
        print(f"\nLegacy codes discovered: {codes}")
    if success:
        print("\nExecution finished successfully.")
    else:
        print("\nExecution finished with missing reports.")