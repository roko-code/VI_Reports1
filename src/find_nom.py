import os
import re
import io
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from pypdf import PdfReader

# Base Directory Resolution
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.asx.com.au/"
})

EXCLUDE_KEYWORDS = ["letter to shareholders", "sample proxy", "voting form only"]
ASX_300_TICKERS = None  # Set to a set like {"BHP", "CBA"} or keep None for all ASX companies

def is_nom_headline(headline):
    """Flexible matching capturing Meeting, AGM, EGM, and standalone GM."""
    pattern = r'\bnotice\b.*\b(meeting|agm|egm|gm)\b'
    return bool(re.search(pattern, headline.lower()))

def extract_and_download_pdf(wrapper_url):
    """Fetches wrapper page and retrieves raw binary PDF content."""
    res = SESSION.get(wrapper_url, allow_redirects=True)
    
    if res.content.startswith(b"%PDF"):
        return res.content

    soup = BeautifulSoup(res.text, "html.parser")
    pdf_tag = soup.find("iframe") or soup.find("embed") or soup.find("object")
    pdf_target = pdf_tag.get("src") if pdf_tag else None

    if not pdf_target:
        match = re.search(r'https?://[^\s"\']+(?:markitdigital|asxpdf|\.pdf)[^\s"\']*', res.text)
        if match:
            pdf_target = match.group(0)

    if pdf_target:
        if pdf_target.startswith("/"):
            pdf_target = "https://www.asx.com.au" + pdf_target
            
        pdf_res = SESSION.get(pdf_target, allow_redirects=True)
        if pdf_res.status_code == 200 and pdf_res.content.startswith(b"%PDF"):
            return pdf_res.content

    return None

def extract_meeting_year(pdf_bytes, fallback_year):
    """Parses PDF text from cover pages to find the actual meeting year."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        extracted_text = ""
        
        # Scan text from the first 3 pages
        for page in reader.pages[:3]:
            extracted_text += (page.extract_text() or "") + "\n"

        # Pattern 1: Look for full dates (e.g., "25 November 2026" or "November 25, 2026")
        date_match = re.search(
            r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d\d)\b', 
            extracted_text, 
            re.IGNORECASE
        )
        if date_match:
            return date_match.group(1)

        # Pattern 2: Look for year directly following keywords like "held on", "Date:", "Meeting 2026"
        context_match = re.search(
            r'\b(?:held|date|time|meeting|agm|egm)[^\n]{0,50}\b(20\d\d)\b', 
            extracted_text, 
            re.IGNORECASE
        )
        if context_match:
            return context_match.group(1)

    except Exception:
        pass
    
    return str(fallback_year)

def parse_page_count(headline_text):
    match = re.search(r'(\d+)\s+page', headline_text.lower())
    return int(match.group(1)) if match else 0

def scan_today_announcements():
    """Scans daily market releases, extracts AGM year from PDF text, and saves NoMs to company workspaces."""
    url = "https://www.asx.com.au/asx/v2/statistics/todayAnns.do"
    fallback_year = datetime.now().year
    
    print(f"[1/3] Connecting to ASX Market Announcements page...")
    try:
        response = SESSION.get(url, timeout=10)
    except Exception as e:
        print(f"Error connecting to ASX: {e}")
        return []

    if response.status_code != 200:
        print(f"Connection failed. HTTP Status: {response.status_code}")
        return []

    print("[2/3] Parsing announcements and grouping matches by company...")
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.find_all("tr")

    scanned_count = 0
    candidates_by_ticker = defaultdict(list)

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        
        scanned_count += 1
        ticker = cells[0].text.strip().upper()
        headline = " ".join(cells[3].text.split())
        link_tag = cells[3].find("a")

        if ASX_300_TICKERS and ticker not in ASX_300_TICKERS:
            continue

        if is_nom_headline(headline):
            if link_tag and "href" in link_tag.attrs:
                href = link_tag["href"]
                pages = parse_page_count(headline)
                is_letter = any(ex in headline.lower() for ex in EXCLUDE_KEYWORDS) and not is_nom_headline(headline)
                
                candidates_by_ticker[ticker].append({
                    "headline": headline,
                    "href": href,
                    "pages": pages,
                    "is_letter": is_letter
                })

    print(f"[3/3] Processing candidates across {len(candidates_by_ticker)} matching companies...")
    download_count = 0
    saved_noms = []

    for ticker, items in candidates_by_ticker.items():
        items.sort(key=lambda x: (not x["is_letter"], x["pages"]), reverse=True)
        selected = items[0]
        
        wrapper_url = "https://www.asx.com.au" + selected["href"]
        
        print(f"\n--> Selected for [{ticker}]: {selected['headline']}")
        print(f"    Fetching PDF binary & extracting AGM date...")
        
        pdf_bytes = extract_and_download_pdf(wrapper_url)
        if pdf_bytes:
            meeting_year = extract_meeting_year(pdf_bytes, fallback_year)
            
            # Construct isolated per-company workspace target directory
            nom_dir = DATA_DIR / "companies" / f"{ticker}_{meeting_year}" / "notices"
            nom_dir.mkdir(parents=True, exist_ok=True)
            
            filename = nom_dir / f"{ticker}_{meeting_year}_NOM.pdf"
            
            with open(filename, "wb") as f:
                f.write(pdf_bytes)
            size_kb = round(len(pdf_bytes) / 1024, 1)
            print(f"    SUCCESS: Saved {filename.relative_to(BASE_DIR)} ({size_kb} KB) [Extracted Year: {meeting_year}]")
            download_count += 1
            
            saved_noms.append({
                "ticker": ticker,
                "year": meeting_year,
                "path": filename
            })
        else:
            print(f"    FAILED: Could not resolve binary PDF stream.")

    print("\n" + "=" * 60)
    print("DAILY SCAN SUMMARY:")
    print(f" - Total market announcements scanned: {scanned_count}")
    print(f" - Primary NoM PDFs saved across company workspaces: {download_count}")
    print("=" * 60)

    return saved_noms

if __name__ == "__main__":
    scan_today_announcements()