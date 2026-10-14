#!/usr/bin/env python3
"""
ASX Annual Reports Downloader — GUI Application

Downloads annual reports and corporate governance statements from ASX announcements.
Single-file tkinter GUI with progress tracking and verification.

Built with PyInstaller/Nuitka frozen-detection support.
"""

import sys
import os
import io
import threading
import queue
import webbrowser
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime
import json
import csv
import re
import time

import requests
from bs4 import BeautifulSoup
import pypdf


# ---------------------------------------------------------------------------
# Frozen detection (PyInstaller / Nuitka) & platform-specific paths
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Running as compiled executable
    BASE_DIR = Path(sys.executable).parent
    IS_FROZEN = True
else:
    # Running as script
    BASE_DIR = Path(__file__).resolve().parent
    IS_FROZEN = False


def get_app_data_dir() -> Path:
    """Get platform-appropriate writable application data directory."""
    if sys.platform == "darwin":
        # macOS: ~/Library/Application Support/AppName
        return Path.home() / "Library" / "Application Support" / "ASX_Annual_Reports"
    elif sys.platform == "win32":
        # Windows: %LOCALAPPDATA%\AppName
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ASX_Annual_Reports"
    else:
        # Linux/Unix: ~/.local/share/AppName
        return Path.home() / ".local" / "share" / "ASX_Annual_Reports"


def get_default_download_dir() -> Path:
    """Get platform-appropriate default download directory."""
    if sys.platform == "darwin":
        return Path.home() / "Documents" / "ASX_Reports"
    elif sys.platform == "win32":
        return Path.home() / "Downloads" / "ASX_Reports"
    else:
        return Path.home() / "ASX_Reports"


# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------
ASX_API_BASE = "https://www.asx.com.au/asx/v2/statistics/announcements.do"
APP_DATA_DIR = get_app_data_dir()
DEFAULT_DOWNLOAD_DIR = get_default_download_dir()
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

AR_KEYWORDS = [
    "Annual Report",
    "Annual Financial Report",
    "Statutory Accounts",
    "Financial Statements",
]

CGS_KEYWORDS = [
    "Corporate Governance Statement",
    "Corporate Governance",
    "Appendix 4G",
    "CGC 4G",
]

NOM_PATTERN = r'\bnotice\b.*\b(meeting|agm|egm|gm)\b'

# Case-Based AR Selection — matches the logic in src/fetch_annual_reports.py
# Cases are tried in order; the first match wins. Each case defines:
#   - pattern: regex to match the report headline (case-insensitive)
#   - exclude: keywords that invalidate a match
#   - min_pages: minimum PDF page count threshold
#   - min_size_bytes: minimum PDF file size threshold
AR_SELECTION_CASES = [
    {
        "name": "annual_report",
        "pattern": r"Annual Report|Annual Financial Report|Statutory Accounts",
        "exclude": ["Concise", "Notice", "Proxy", "Presentation", "Explanatory",
                    "Note", "Addendum", "Clarification", "Erratum", "Correction",
                    "Letter", "Covering", "Dividend", "Extract"],
        "min_pages": 50,
        "min_size_bytes": 2_000_000,  # 2 MB
    },
    {
        "name": "audited_financial",
        "pattern": r"Audited Financial Statements|Consolidated Annual Financial Statements|Financial Statements\b",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "appendix_4e",
        "pattern": r"Appendix 4E|Full Year Report|Corporate Report",
        "exclude": ["Letter", "Proxy", "Notice"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "preliminary_final",
        "pattern": r"Preliminary Final Report",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "form_10k",
        "pattern": r"Form 10-K",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "full_year_results",
        "pattern": r"Full Year Results|Full Year Financial Results",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "annual_information_form",
        "pattern": r"Consolidated Financial Statements",
        "exclude": ["Concise", "Notice", "Proxy", "Presentation", "Explanatory"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "annual_results",
        "pattern": r"Annual Results",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
    {
        "name": "fy_results",
        "pattern": r"FY\d{2}\s+Results",
        "exclude": ["Letter"],
        "min_pages": 10,
        "min_size_bytes": 10_000_000,  # 10 MB — disambiguate multiple FY Results matches
    },
    {
        "name": "integrated_report",
        "pattern": r"Integrated Report",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 1_000_000,
    },
    {
        "name": "financial_results_full_year",
        "pattern": r"Financial Results\s+for\s+full\s+Year",
        "exclude": ["Letter"],
        "min_pages": 5,
        "min_size_bytes": 100_000,
    },
]

# Exceptional companies: data spread across multiple reports
# Type "split_remuneration" = remuneration report filed separately from AR
EXCEPTIONAL_COMPANIES = {
    "BHP": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "RIO": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "TCL": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "NAB": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "WBC": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "ANZ": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "WES": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "CSL": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report"},
    "CSC": {"type": "split_remuneration", "description": "Remuneration report filed separately from annual report as Management Information Circular"},
}


# ---------------------------------------------------------------------------
# ASX Client
# ---------------------------------------------------------------------------
class ASXClient:
    def __init__(self, logger=None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.logger = logger

    def _log(self, msg, level="info"):
        if self.logger:
            getattr(self.logger, level)(msg)
        else:
            print(f"[{level.upper()}] {msg}")

    def search_announcements(self, asx_code: str, year: int, timeframe: str = "Y") -> list[dict]:
        """Search ASX announcements for a company/year. Parses HTML response."""
        params = {
            "by": "asxCode",
            "asxCode": asx_code.upper(),
            "timeframe": timeframe,
            "year": year,
        }
        try:
            resp = self.session.get(ASX_API_BASE, params=params, timeout=30)
            resp.raise_for_status()
            
            # Parse HTML response
            soup = BeautifulSoup(resp.text, "html.parser")
            announcements = []
            
            # Find all announcement links
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if "/asx/v2/statistics/displayAnnouncement.do" in href:
                    raw_text = link.get_text(strip=True)
                    # Extract reported file size (e.g., "pages3.4MB", "12 pages2.1MB")
                    size_match = re.search(r'pages?\s*(\d+(?:\.\d+)?)\s*([KM])B?', raw_text, re.IGNORECASE)
                    reported_size = 0
                    if size_match:
                        num = float(size_match.group(1))
                        unit = size_match.group(2).upper()
                        reported_size = int(num * (1000 if unit == "K" else 1_000_000))
                    # Clean up headline (remove size info like "pages6.7MB")
                    headline = re.sub(r"\s*pages?[\d\.]+[KM]?B?$", "", raw_text, flags=re.IGNORECASE).strip()
                    
                    # Build full URL
                    if href.startswith("/"):
                        url = "https://www.asx.com.au" + href
                    else:
                        url = href
                    
                    announcements.append({
                        "headline": headline,
                        "reported_size_bytes": reported_size,
                        "url": url,
                        "documentUrl": url,
                        "pdfUrl": url,
                    })
            
            return announcements
        except Exception as e:
            self._log(f"Search failed for {asx_code} {year}: {e}", "error")
            return []

    def verify_asx_code(self, asx_code: str, year: int) -> bool:
        """Verify that an ASX code is valid by checking the announcements page responds with data.
        Returns True if the code exists and has announcement rows, False otherwise.
        """
        try:
            resp = self.session.get(
                ASX_API_BASE,
                params={
                    "by": "asxCode",
                    "asxCode": asx_code.upper(),
                    "timeframe": "Y",
                    "year": year,
                },
                timeout=15,
            )
            if resp.status_code == 404:
                self._log(f"ASX code '{asx_code}' not found (404)", "error")
                return False
            if resp.status_code != 200:
                self._log(f"ASX code '{asx_code}' returned HTTP {resp.status_code}", "warning")
                return False
            # Check if the page contains announcement table rows
            soup = BeautifulSoup(resp.text, "html.parser")
            has_announcements = bool(soup.find("a", href=re.compile(r"displayAnnouncement\.do")))
            if not has_announcements:
                self._log(f"ASX code '{asx_code}' — no announcements found for {year}", "warning")
                return False
            return True
        except Exception as e:
            self._log(f"ASX code verification failed for '{asx_code}': {e}", "error")
            return False

    def fetch_pdf_bytes(self, url: str) -> bytes | None:
        """Download PDF content from URL. Handles ASX HTML wrapper pages."""
        try:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            
            # If already a PDF, return directly
            if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
                return resp.content
            
            # If HTML, extract actual PDF URL from page
            if "html" in content_type.lower():
                # Look for PDF URL in announcements.asx.com.au domain
                import re
                pdf_urls = re.findall(r'https?://announcements\.asx\.com\.au/asxpdf/[^"\s>]+\.pdf', resp.text)
                if pdf_urls:
                    real_pdf_url = pdf_urls[0]
                    self._log(f"Found real PDF URL: {real_pdf_url}")
                    pdf_resp = self.session.get(real_pdf_url, timeout=60)
                    pdf_resp.raise_for_status()
                    return pdf_resp.content
                
                # Fallback: look for any .pdf URL
                pdf_urls = re.findall(r'https?://[^"\s>]+\.pdf', resp.text)
                if pdf_urls:
                    real_pdf_url = pdf_urls[0]
                    self._log(f"Found fallback PDF URL: {real_pdf_url}")
                    pdf_resp = self.session.get(real_pdf_url, timeout=60)
                    pdf_resp.raise_for_status()
                    return pdf_resp.content
            
            self._log(f"URL does not appear to be a PDF and no PDF link found: {url}", "warning")
            return resp.content
        except Exception as e:
            self._log(f"PDF fetch failed for {url}: {e}", "error")
            return None


# ---------------------------------------------------------------------------
# PDF Verification
# ---------------------------------------------------------------------------
def verify_pdf(pdf_bytes: bytes, min_pages: int = 10, min_size_bytes: int = 1_000_000) -> tuple[int, int, bool]:
    """Verify PDF meets minimum page count and size. Returns (pages, size, valid)."""
    size = len(pdf_bytes)
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = len(reader.pages)
    except Exception:
        pages = 0

    valid = pages >= min_pages and size >= min_size_bytes
    return pages, size, valid


# ---------------------------------------------------------------------------
# Case-Based Report Matching Logic
# ---------------------------------------------------------------------------
def select_report_case(headline: str) -> dict | None:
    """Match a headline against AR selection cases. Returns the matched case dict or None."""
    headline_lower = headline.lower()
    for case in AR_SELECTION_CASES:
        if any(ex.lower() in headline_lower for ex in case["exclude"]):
            continue
        if re.search(case["pattern"], headline, re.IGNORECASE):
            return case
    return None


def is_annual_report(headline: str) -> bool:
    """Check if announcement headline matches any annual report case. Maintains backward compat."""
    return select_report_case(headline) is not None


def is_cgs_report(headline: str) -> bool:
    """Check if announcement headline matches CGS patterns."""
    headline_lower = headline.lower()
    return any(kw.lower() in headline_lower for kw in CGS_KEYWORDS)


def extract_report_year(headline: str, pub_year: int) -> int:
    """Extract the financial year from headline, fallback to publication year."""
    # Look for FY2026, 2026, year ending 2026, etc.
    patterns = [
        r"FY\s*(\d{4})",
        r"(\d{4})\s*Annual Report",
        r"year ending\s+(\d{4})",
        r"financial year\s+(\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, headline, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return pub_year


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------
class ASXDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ASX Annual Reports Downloader")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)

        self.client = ASXClient()
        self.download_dir = DEFAULT_DOWNLOAD_DIR
        # Ensure app data dir exists for settings
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Ensure download dir exists
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.task_queue = queue.Queue()
        self.results = []
        self.running = False

        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        # Main frame
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Search Parameters", padding=10)
        input_frame.pack(fill=tk.X, pady=(0, 10))

        # ASX Code input
        ttk.Label(input_frame, text="ASX Code(s):").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.code_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=self.code_var, width=40).grid(row=0, column=1, sticky=tk.EW, padx=5, pady=2)
        ttk.Label(input_frame, text="(comma-separated, e.g. BHP, CBA, TLS)").grid(row=0, column=2, sticky=tk.W)

        # Year range
        ttk.Label(input_frame, text="Year Range:").grid(row=1, column=0, sticky=tk.W, pady=2)
        year_frame = ttk.Frame(input_frame)
        year_frame.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=2)
        current_year = datetime.now().year
        self.start_year = tk.IntVar(value=current_year - 4)
        self.end_year = tk.IntVar(value=current_year)
        ttk.Spinbox(year_frame, from_=2000, to=current_year, textvariable=self.start_year, width=8).pack(side=tk.LEFT)
        ttk.Label(year_frame, text="to").pack(side=tk.LEFT, padx=5)
        ttk.Spinbox(year_frame, from_=2000, to=current_year, textvariable=self.end_year, width=8).pack(side=tk.LEFT)

        # Download directory
        ttk.Label(input_frame, text="Download Folder:").grid(row=2, column=0, sticky=tk.W, pady=2)
        dir_frame = ttk.Frame(input_frame)
        dir_frame.grid(row=2, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=2)
        self.dir_var = tk.StringVar(value=str(self.download_dir))
        ttk.Entry(dir_frame, textvariable=self.dir_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dir_frame, text="Browse", command=self._browse_dir).pack(side=tk.LEFT, padx=5)

        input_frame.columnconfigure(1, weight=1)

        # Options
        opt_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        opt_frame.pack(fill=tk.X, pady=(0, 10))

        self.download_ar = tk.BooleanVar(value=True)
        self.download_cgs = tk.BooleanVar(value=True)
        self.download_nom = tk.BooleanVar(value=True)
        self.download_vi = tk.BooleanVar(value=True)
        self.verify_pdfs = tk.BooleanVar(value=True)
        self.skip_existing = tk.BooleanVar(value=True)

        ttk.Checkbutton(opt_frame, text="Download Annual Reports", variable=self.download_ar).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Download Corporate Governance Statements", variable=self.download_cgs).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Download Notice of Meeting (NoM)", variable=self.download_nom).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Download VI/AGM Reports (from ASA)", variable=self.download_vi).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Verify PDFs (page count + size)", variable=self.verify_pdfs).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Skip existing files", variable=self.skip_existing).pack(anchor=tk.W)

        # Control buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))
        self.start_btn = ttk.Button(btn_frame, text="Start Download", command=self._start_download, style="Accent.TButton")
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self._stop_download, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Open Folder", command=self._open_folder).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT, padx=5)

        # Progress
        prog_frame = ttk.LabelFrame(main_frame, text="Progress", padding=10)
        prog_frame.pack(fill=tk.X, pady=(0, 10))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(prog_frame, textvariable=self.status_var).pack(anchor=tk.W, pady=2)

        # Log output
        log_frame = ttk.LabelFrame(main_frame, text="Activity Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=15, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=scrollbar.set)

        # Results summary
        self.summary_var = tk.StringVar(value="")
        ttk.Label(main_frame, textvariable=self.summary_var, font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=5)

    def _browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_var.get())
        if path:
            self.dir_var.set(path)
            self.download_dir = Path(path)

    def _load_settings(self):
        settings_file = APP_DATA_DIR / "settings.json"
        if settings_file.exists():
            try:
                with open(settings_file) as f:
                    data = json.load(f)
                self.dir_var.set(data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR)))
                self.download_dir = Path(self.dir_var.get())
            except Exception:
                pass

    def _save_settings(self):
        settings_file = APP_DATA_DIR / "settings.json"
        try:
            with open(settings_file, "w") as f:
                json.dump({"download_dir": str(self.download_dir)}, f)
        except Exception:
            pass

    def _log(self, msg, level="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = {"info": "ℹ", "warning": "⚠", "error": "✖", "success": "✔"}.get(level, "•")
        self.log_text.insert(tk.END, f"[{timestamp}] {prefix} {msg}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def _clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def _open_folder(self):
        if sys.platform == "win32":
            os.startfile(self.download_dir)
        elif sys.platform == "darwin":
            subprocess.run(["open", self.download_dir])
        else:
            subprocess.run(["xdg-open", self.download_dir])

    def _start_download(self):
        codes_raw = self.code_var.get().strip()
        if not codes_raw:
            messagebox.showwarning("Input Required", "Please enter at least one ASX code.")
            return

        codes = [c.strip().upper() for c in codes_raw.split(",") if c.strip()]
        invalid = [c for c in codes if not re.match(r"^[A-Z]{3,4}$", c)]
        if invalid:
            messagebox.showwarning("Invalid Codes", f"Invalid ASX codes: {', '.join(invalid)}")
            return

        start_y = self.start_year.get()
        end_y = self.end_year.get()
        if start_y > end_y:
            messagebox.showwarning("Invalid Range", "Start year cannot be after end year.")
            return

        if not self.download_ar.get() and not self.download_cgs.get() and not self.download_nom.get():
            messagebox.showwarning("No Selection", "Please select at least one document type to download.")
            return

        self.download_dir = Path(self.dir_var.get())
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._save_settings()

        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress_var.set(0)
        self.results.clear()

        self._log(f"Starting download for {len(codes)} code(s): {', '.join(codes)}")
        self._log(f"Year range: {start_y}–{end_y}")
        self._log(f"Download folder: {self.download_dir}")

        # Start worker thread
        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(codes, start_y, end_y),
            daemon=True
        )
        self.worker_thread.start()

        # Poll queue
        self.root.after(100, self._poll_queue)

    def _stop_download(self):
        self.running = False
        self._log("Stop requested...", "warning")
        self.stop_btn.config(state=tk.DISABLED)

    def _worker(self, codes: list[str], start_y: int, end_y: int):
        total_tasks = len(codes) * (end_y - start_y + 1)
        if self.download_ar.get():
            total_tasks += len(codes)
        if self.download_cgs.get():
            total_tasks += len(codes)
        if self.download_nom.get():
            total_tasks += len(codes)
        if self.download_vi.get():
            total_tasks += len(codes)
        completed = 0

        for code in codes:
            if not self.running:
                break

            self.task_queue.put(("status", f"Processing {code}..."))

            # Step 1: Verify ASX code validity — stop if invalid to avoid 404 spam
            if not self.client.verify_asx_code(code, end_y):
                self.task_queue.put(("result", {"code": code, "year": end_y, "type": "ALL", "status": "error", "error": f"Invalid or unknown ASX code: {code}"}))
                completed += 1
                self.task_queue.put(("progress", completed / total_tasks * 100))
                continue

            # Check for AR announcements in each year
            for year in range(start_y, end_y + 1):
                if not self.running:
                    break

                # Annual Reports
                if self.download_ar.get():
                    self.task_queue.put(("status", f"{code} {year}: Searching AR announcements..."))
                    announcements = self.client.search_announcements(code, year)
                    
                    # Case-based selection: each announcement matched against cases in order
                    ar_candidates = []
                    for ann in announcements:
                        matched_case = select_report_case(ann.get("headline", ""))
                        if matched_case:
                            ann["case"] = matched_case
                            ar_candidates.append(ann)

                    if ar_candidates:
                        # Disambiguation: sort by case priority (annual_report first),
                        # then by reported file size within the same case (prefer larger = more complete).
                        case_priority = {c["name"]: i for i, c in enumerate(AR_SELECTION_CASES)}
                        ar_candidates.sort(key=lambda c: (
                            case_priority.get(c["case"]["name"], 99),
                            -c.get("reported_size_bytes", 0),
                        ))
                        for cand in ar_candidates:
                            if not self.running:
                                break
                            url = cand.get("url") or cand.get("documentUrl") or cand.get("pdfUrl")
                            if not url:
                                continue
                            if not url.startswith("http"):
                                url = "https://www.asx.com.au" + url

                            case = cand.get("case", AR_SELECTION_CASES[0])
                            self.task_queue.put(("status", f"{code} {year}: Downloading AR ({case['name']})..."))
                            pdf_bytes = self.client.fetch_pdf_bytes(url)
                            if pdf_bytes:
                                pages, size, valid = verify_pdf(pdf_bytes, case["min_pages"], case["min_size_bytes"]) if self.verify_pdfs.get() else (0, len(pdf_bytes), True)
                                if valid or not self.verify_pdfs.get():
                                    report_year = extract_report_year(cand.get("headline", ""), year)
                                    fname = f"{code}_{report_year}_AR.pdf"
                                    fpath = self.download_dir / fname
                                    if self.skip_existing.get() and fpath.exists():
                                        self.task_queue.put(("result", {"code": code, "year": report_year, "type": "AR", "status": "skipped", "path": str(fpath)}))
                                    else:
                                        try:
                                            fpath.write_bytes(pdf_bytes)
                                            self.task_queue.put(("result", {"code": code, "year": report_year, "type": "AR", "status": "success", "path": str(fpath), "pages": pages, "size": size}))
                                        except Exception as e:
                                            self.task_queue.put(("result", {"code": code, "year": year, "type": "AR", "status": "error", "error": str(e)}))
                                    break  # First valid AR for this year/case — stop downloading remaining candidates
                                else:
                                    self.task_queue.put(("result", {"code": code, "year": year, "type": "AR", "status": "failed", "error": f"PDF verification failed: {pages} pages, {size} bytes (case: {case['name']})"}))

                    completed += 1
                    self.task_queue.put(("progress", completed / total_tasks * 100))

                # Corporate Governance Statements (latest year only)
                if self.download_cgs.get() and self.running and year == end_y:
                    self.task_queue.put(("status", f"{code} {year}: Searching CGS announcements..."))
                    announcements = self.client.search_announcements(code, year)
                    cgs_candidates = [a for a in announcements if is_cgs_report(a.get("headline", ""))]

                    if cgs_candidates:
                        for cand in cgs_candidates:
                            if not self.running:
                                break
                            url = cand.get("url") or cand.get("documentUrl") or cand.get("pdfUrl")
                            if not url:
                                continue
                            if not url.startswith("http"):
                                url = "https://www.asx.com.au" + url

                            self.task_queue.put(("status", f"{code} {year}: Downloading CGS..."))
                            pdf_bytes = self.client.fetch_pdf_bytes(url)
                            if pdf_bytes:
                                if not pdf_bytes.startswith(b"%PDF"):
                                    self.task_queue.put(("result", {"code": code, "year": year, "type": "CGS", "status": "failed", "error": "Not a valid PDF"}))
                                    continue

                                size = len(pdf_bytes)
                                pages, _, _ = verify_pdf(pdf_bytes) if self.verify_pdfs.get() else (0, size, True)
                                # No min size/pages check for CGS — any valid PDF is accepted
                                fname = f"{code}_{year}_CGS.pdf"
                                fpath = self.download_dir / fname
                                if self.skip_existing.get() and fpath.exists():
                                    self.task_queue.put(("result", {"code": code, "year": year, "type": "CGS", "status": "skipped", "path": str(fpath)}))
                                else:
                                    try:
                                        fpath.write_bytes(pdf_bytes)
                                        self.task_queue.put(("result", {"code": code, "year": year, "type": "CGS", "status": "success", "path": str(fpath), "pages": pages, "size": size}))
                                    except Exception as e:
                                        self.task_queue.put(("result", {"code": code, "year": year, "type": "CGS", "status": "error", "error": str(e)}))
                                break  # First valid CGS — stop downloading remaining candidates

                    completed += 1
                    self.task_queue.put(("progress", completed / total_tasks * 100))

                # Exceptional companies: download split remuneration report (latest year only)
                if code in EXCEPTIONAL_COMPANIES and year == end_y:
                    exc = EXCEPTIONAL_COMPANIES[code]
                    if exc.get("type") == "split_remuneration":
                        self.task_queue.put(("status", f"{code}: Downloading split remuneration report..."))
                        remuneration_saved = self._download_exceptional_report(code, end_y, end_y)
                        if remuneration_saved:
                            self.task_queue.put(("result", {"code": code, "year": end_y, "type": "REMUNERATION", "status": "success", "path": str(remuneration_saved)}))

                # VI Reports from ASA (for latest year only)
                if self.download_vi.get() and self.running and year == end_y:
                    self.task_queue.put(("status", f"{code}: Downloading VI/AGM reports from ASA..."))
                    vi_saved = self._download_vi_reports(code, end_y)
                    if vi_saved:
                        self.task_queue.put(("result", {"code": code, "year": end_y, "type": "VI", "status": "success", "path": str(vi_saved)}))

                # Notice of Meeting (NoM) — latest year only
                if self.download_nom.get() and self.running and year == end_y:
                    self.task_queue.put(("status", f"{code}: Downloading Notice of Meeting (NoM)..."))
                    nom_saved = self._download_nom_report(code, end_y)
                    if nom_saved:
                        self.task_queue.put(("result", {"code": code, "year": end_y, "type": "NOM", "status": "success", "path": str(nom_saved)}))

        self.task_queue.put(("done", None))

    def _download_exceptional_report(self, ticker: str, search_year: int, target_year: int) -> str | None:
        """Download split remuneration report for exceptional companies."""
        announcements = self.client.search_announcements(ticker, search_year)
        # Collect all candidates first, then sort by reported size (prefer larger = full report)
        rem_candidates = []
        for ann in announcements:
            headline = ann.get("headline", "")
            if re.search(r"Remuneration Report|Remuneration Statement|Director Remuneration|Management Information Circular",
                        headline, re.IGNORECASE):
                if re.search(r"\b(Appendix|Note|Letter|Ancillary)\b", headline, re.IGNORECASE):
                    continue
                url = ann.get("url")
                if not url:
                    continue
                rem_candidates.append({"headline": headline, "url": url, "reported_size_bytes": ann.get("reported_size_bytes", 0)})

        # Sort by reported file size descending (prefer larger = full report over voting forms)
        rem_candidates.sort(key=lambda c: c.get("reported_size_bytes", 0), reverse=True)

        for cand in rem_candidates:
            headline = cand["headline"]
            url = cand["url"]
            self._log(f"Exceptional: Trying remuneration report for {ticker}: {headline} ({cand['reported_size_bytes']} bytes reported)")
            pdf_bytes = self.client.fetch_pdf_bytes(url)
            if pdf_bytes and pdf_bytes.startswith(b"%PDF"):
                fname = f"{ticker}_{target_year}_REMUNERATION.pdf"
                fpath = self.download_dir / fname
                try:
                    fpath.write_bytes(pdf_bytes)
                    size_mb = round(len(pdf_bytes) / (1024 * 1024), 2)
                    self._log(f"Exceptional: Saved {fname} ({size_mb} MB)")
                    return str(fpath)
                except Exception as e:
                    self._log(f"Exceptional: Error saving remuneration report: {e}", "error")
        self._log(f"Exceptional: No remuneration report found for {ticker}")
        return None

    def _download_nom_report(self, ticker: str, target_year: int) -> str | None:
        """Download Notice of Meeting (NoM) PDF for the given ticker (latest year only).
        Searches both target_year and target_year-1, matching src/fetch_nom.py logic.
        """
        for search_year in [target_year, target_year - 1]:
            announcements = self.client.search_announcements(ticker, search_year)
            if not announcements:
                self._log(f"NoM: No ASX announcements found for {ticker} in {search_year}")
                continue

            # Filter to NoM headlines only
            nom_candidates = [a for a in announcements if re.search(NOM_PATTERN, a.get("headline", ""), re.IGNORECASE)]
            if not nom_candidates:
                self._log(f"NoM: No Notice of Meeting found for {ticker} in {search_year}")
                continue

            # Disambiguation: prefer non-letter announcements, then by reported file size
            nom_candidates.sort(key=lambda a: (
                not re.search(r"\bletter\b", a.get("headline", ""), re.IGNORECASE),
                a.get("reported_size_bytes", 0),
            ), reverse=False)

            for cand in nom_candidates:
                url = cand.get("url")
                if not url:
                    continue
                if not url.startswith("http"):
                    url = "https://www.asx.com.au" + url

                self._log(f"NoM: Downloading {cand['headline']} for {ticker}")
                pdf_bytes = self.client.fetch_pdf_bytes(url)
                if pdf_bytes and pdf_bytes.startswith(b"%PDF"):
                    fname = f"{ticker}_{target_year}_NOM.pdf"
                    fpath = self.download_dir / fname
                    try:
                        fpath.write_bytes(pdf_bytes)
                        size_kb = round(len(pdf_bytes) / 1024, 1)
                        self._log(f"NoM: Saved {fname} ({size_kb} KB)")
                        return str(fpath)
                    except Exception as e:
                        self._log(f"NoM: Error saving: {e}", "error")
                else:
                    self._log(f"NoM: Could not resolve PDF for {cand['headline']}", "warning")

        self._log(f"NoM: No valid PDF found for {ticker}")
        return None

    def _download_vi_reports(self, ticker: str, target_year: int) -> str | None:
        """Download VI/AGM/EGM reports from ASA website."""
        ASA_BASE_URL = "https://www.australianshareholders.com.au"
        HEADERS = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        years_back = 5
        target_years = set(range(target_year - years_back, target_year))
        vi_dir = self.download_dir / "vi_reports"
        vi_dir.mkdir(parents=True, exist_ok=True)

        company_url = f"{ASA_BASE_URL}/company/{ticker.lower()}/"
        self._log(f"ASA: Scraping {company_url}")

        try:
            response = self.client.session.get(company_url, headers=HEADERS, timeout=15)
        except Exception as e:
            self._log(f"ASA: Error connecting for {ticker}: {e}", "warning")
            return None

        if response.status_code != 200:
            self._log(f"ASA: Page returned HTTP {response.status_code} for {ticker}", "warning")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        sections = [
            ("voting-intentions", "VI"),
            ("meeting-reports", None),
        ]

        count = 0
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

                pdf_url = href if href.startswith("http") else ASA_BASE_URL + href
                self._log(f"ASA: Downloading {link_text}")

                try:
                    pdf_response = self.client.session.get(pdf_url, headers=HEADERS, timeout=60)
                    if pdf_response.status_code != 200:
                        continue
                    if not pdf_response.content.startswith(b"%PDF"):
                        continue

                    suffix = "VIR" if report_type == "VI" else report_type
                    filename = f"{ticker}_{year}_{suffix}.pdf"
                    file_path = vi_dir / filename
                    with open(file_path, "wb") as f:
                        f.write(pdf_response.content)

                    size_kb = round(len(pdf_response.content) / 1024, 1)
                    self._log(f"ASA: Saved {filename} ({size_kb} KB)")
                    count += 1
                except Exception as e:
                    self._log(f"ASA: Error downloading {link_text}: {e}", "error")

        if count > 0:
            return str(vi_dir)
        self._log(f"ASA: No VI reports found for years {sorted(target_years)}")
        return None

    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self.task_queue.get_nowait()
                if msg_type == "status":
                    self.status_var.set(data)
                elif msg_type == "progress":
                    self.progress_var.set(data)
                elif msg_type == "result":
                    self.results.append(data)
                    status = data.get("status", "unknown")
                    if status == "success":
                        self._log(f"{data['code']} {data.get('year', '')} {data['type']}: Saved ({data.get('pages', 0)} pages, {data.get('size', 0)//1024} KB)", "success")
                    elif status == "skipped":
                        self._log(f"{data['code']} {data.get('year', '')} {data['type']}: Skipped (already exists)", "warning")
                    elif status == "failed":
                        self._log(f"{data['code']} {data.get('year', '')} {data['type']}: Failed - {data.get('error', 'unknown')}", "error")
                    elif status == "error":
                        self._log(f"{data['code']} {data.get('year', '')} {data['type']}: Error - {data.get('error', 'unknown')}", "error")
                elif msg_type == "done":
                    self._download_complete()
                    return
        except queue.Empty:
            pass

        if self.running:
            self.root.after(100, self._poll_queue)

    def _download_complete(self):
        self.running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.progress_var.set(100)
        self.status_var.set("Complete")

        success = sum(1 for r in self.results if r.get("status") == "success")
        skipped = sum(1 for r in self.results if r.get("status") == "skipped")
        failed = sum(1 for r in self.results if r.get("status") in ("failed", "error"))

        self.summary_var.set(f"Completed: {success} downloaded, {skipped} skipped, {failed} failed")
        self._log(f"Download complete: {success} downloaded, {skipped} skipped, {failed} failed", "success" if failed == 0 else "warning")

        # Save results summary
        summary_file = self.download_dir / f"download_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(summary_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["code", "year", "type", "status", "path", "pages", "size", "error"])
                writer.writeheader()
                for r in self.results:
                    writer.writerow(r)
            self._log(f"Summary saved to: {summary_file}")
        except Exception as e:
            self._log(f"Failed to save summary: {e}", "error")


def main():
    root = tk.Tk()
    try:
        # Use modern theme if available
        from tkinter import ttk
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "aqua" in style.theme_names():
            style.theme_use("aqua")
    except Exception:
        pass

    app = ASXDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()