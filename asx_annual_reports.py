#!/usr/bin/env python3
"""
ASX Annual Reports Downloader — GUI Application

Downloads annual reports and corporate governance statements from ASX announcements.
Single-file tkinter GUI with progress tracking and verification.

Built with PyInstaller/Nuitka frozen-detection support.
"""

import sys
import os
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
from lxml import html
from bs4 import BeautifulSoup
import pypdf


# ---------------------------------------------------------------------------
# Frozen detection (PyInstaller / Nuitka)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Running as compiled executable
    BASE_DIR = Path(sys.executable).parent
    IS_FROZEN = True
else:
    # Running as script
    BASE_DIR = Path(__file__).resolve().parent
    IS_FROZEN = False


# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------
ASX_API_BASE = "https://www.asx.com.au/asx/v2/statistics/announcements.do"
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "ASX_Reports"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

MIN_PDF_PAGES = 10
MIN_PDF_SIZE_BYTES = 500_000  # 500 KB

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
]


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
        """Search ASX announcements for a company/year."""
        params = {
            "by": "asxCode",
            "asxCode": asx_code.upper(),
            "timeframe": timeframe,
            "year": year,
        }
        try:
            resp = self.session.get(ASX_API_BASE, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            self._log(f"Search failed for {asx_code} {year}: {e}", "error")
            return []

    def fetch_pdf_bytes(self, url: str) -> bytes | None:
        """Download PDF content from URL."""
        try:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
                self._log(f"URL does not appear to be a PDF: {url}", "warning")
            return resp.content
        except Exception as e:
            self._log(f"PDF fetch failed for {url}: {e}", "error")
            return None


# ---------------------------------------------------------------------------
# PDF Verification
# ---------------------------------------------------------------------------
def verify_pdf(pdf_bytes: bytes) -> tuple[int, int, bool]:
    """Verify PDF meets minimum page count and size. Returns (pages, size, valid)."""
    size = len(pdf_bytes)
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = len(reader.pages)
    except Exception:
        pages = 0

    valid = pages >= MIN_PDF_PAGES and size >= MIN_PDF_SIZE_BYTES
    return pages, size, valid


# ---------------------------------------------------------------------------
# Report Matching Logic
# ---------------------------------------------------------------------------
def is_annual_report(headline: str) -> bool:
    """Check if announcement headline matches annual report patterns."""
    headline_lower = headline.lower()
    return any(kw.lower() in headline_lower for kw in AR_KEYWORDS)


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
        self.verify_pdfs = tk.BooleanVar(value=True)
        self.skip_existing = tk.BooleanVar(value=True)

        ttk.Checkbutton(opt_frame, text="Download Annual Reports", variable=self.download_ar).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Download Corporate Governance Statements", variable=self.download_cgs).pack(anchor=tk.W)
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
        settings_file = BASE_DIR / "settings.json"
        if settings_file.exists():
            try:
                with open(settings_file) as f:
                    data = json.load(f)
                self.dir_var.set(data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR)))
                self.download_dir = Path(self.dir_var.get())
            except Exception:
                pass

    def _save_settings(self):
        settings_file = BASE_DIR / "settings.json"
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

        if not self.download_ar.get() and not self.download_cgs.get():
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
        total_tasks = len(codes) * (end_y - start_y + 1) * (2 if self.download_ar.get() and self.download_cgs.get() else 1)
        completed = 0

        for code in codes:
            if not self.running:
                break

            self.task_queue.put(("status", f"Processing {code}..."))

            # Check for AR announcements in each year
            for year in range(start_y, end_y + 1):
                if not self.running:
                    break

                # Annual Reports
                if self.download_ar.get():
                    self.task_queue.put(("status", f"{code} {year}: Searching AR announcements..."))
                    announcements = self.client.search_announcements(code, year)
                    ar_candidates = [a for a in announcements if is_annual_report(a.get("headline", ""))]

                    if ar_candidates:
                        for cand in ar_candidates:
                            if not self.running:
                                break
                            url = cand.get("url") or cand.get("documentUrl") or cand.get("pdfUrl")
                            if not url:
                                continue
                            if not url.startswith("http"):
                                url = "https://www.asx.com.au" + url

                            self.task_queue.put(("status", f"{code} {year}: Downloading AR..."))
                            pdf_bytes = self.client.fetch_pdf_bytes(url)
                            if pdf_bytes:
                                pages, size, valid = verify_pdf(pdf_bytes) if self.verify_pdfs.get() else (0, len(pdf_bytes), True)
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
                                else:
                                    self.task_queue.put(("result", {"code": code, "year": year, "type": "AR", "status": "failed", "error": f"PDF verification failed: {pages} pages, {size} bytes"}))

                    completed += 1
                    self.task_queue.put(("progress", completed / total_tasks * 100))

                # Corporate Governance Statements
                if self.download_cgs.get() and self.running:
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
                                pages, size, valid = verify_pdf(pdf_bytes) if self.verify_pdfs.get() else (0, len(pdf_bytes), True)
                                if valid or not self.verify_pdfs.get():
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
                                else:
                                    self.task_queue.put(("result", {"code": code, "year": year, "type": "CGS", "status": "failed", "error": f"PDF verification failed: {pages} pages, {size} bytes"}))

                    completed += 1
                    self.task_queue.put(("progress", completed / total_tasks * 100))

        self.task_queue.put(("done", None))

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