"""Folder-driven loaders for the pipeline's reference and input documents.

Design rule (2026-10-10): stages load documents by globbing their folders, not
by hard-coded filenames. The ASA guidelines are under active review and the
comparator CSV is a placeholder awaiting a real-named replacement, so "drop a
new file in the folder" must be enough - no prompt or code edit.

    data/asa_guidelines/*.pdf            -> all guidelines, concatenated
    data/_reference/comparator/*.csv     -> the comparator (newest wins)
    {workspace}/governance/*_CGS.pdf     -> standalone Corporate Governance Statement
    {workspace}/vi_reports/*_VIR.pdf     -> prior ASA Voting Intentions (latest year)
    {workspace}/vi_reports/*_AGM.pdf     -> prior ASA AGM/EGM meeting report (latest)
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Optional

import pdfplumber

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ASA_GUIDELINES_DIR = DATA_DIR / "asa_guidelines"
COMPARATOR_DIR = DATA_DIR / "_reference" / "comparator"
# Backwards-compatible fallback for the pre-folder comparator location.
_LEGACY_COMPARATOR = DATA_DIR / "_reference" / "ceo_kmp_comparator.csv"

_YEAR_RE = re.compile(r"(\d{4})")


def extract_pdf_text(pdf_path: Path) -> str:
    """Full text of a PDF, one page per block, with page markers."""
    parts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"--- Page {idx + 1} ---\n{text}")
    return "\n".join(parts)


def _year_of(path: Path) -> int:
    m = _YEAR_RE.search(path.stem)
    return int(m.group(1)) if m else 0


def _latest_year(files: list[Path]) -> Optional[Path]:
    return max(files, key=_year_of) if files else None


def _newest_mtime(files: list[Path]) -> Optional[Path]:
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def load_asa_guidelines() -> str:
    """All ASA guideline PDFs in data/asa_guidelines/, concatenated.

    Every *.pdf in the folder is included, each under a `FILE:` header so the
    model can tell the documents apart. Adding or replacing a PDF in the folder
    changes what is sent with no other edit. Sorted by name for determinism.
    """
    files = sorted(ASA_GUIDELINES_DIR.glob("*.pdf"))
    blocks = []
    for f in files:
        blocks.append(f"===== ASA GUIDELINE FILE: {f.name} =====")
        blocks.append(extract_pdf_text(f))
    return "\n\n".join(blocks)


def load_cgs_text(workspace_dir: Path) -> Optional[str]:
    """The standalone Corporate Governance Statement, or None when absent.

    Only `*_CGS.pdf` files count; a miss means the CGS is embedded in the AR.
    """
    gov_dir = workspace_dir / "governance"
    if not gov_dir.exists():
        return None
    files = list(gov_dir.glob("*_CGS.pdf"))
    newest = _latest_year(files) or _newest_mtime(files)
    return extract_pdf_text(newest) if newest else None


def load_latest_vi_text(workspace_dir: Path) -> Optional[str]:
    """Text of the most recent prior ASA Voting Intentions report."""
    vi_dir = workspace_dir / "vi_reports"
    if not vi_dir.exists():
        return None
    files = list(vi_dir.glob("*_VIR.pdf"))
    latest = _latest_year(files)
    return extract_pdf_text(latest) if latest else None


def load_latest_meeting_text(workspace_dir: Path) -> Optional[str]:
    """Text of the most recent prior ASA AGM (or EGM) meeting report."""
    vi_dir = workspace_dir / "vi_reports"
    if not vi_dir.exists():
        return None
    files = list(vi_dir.glob("*_AGM.pdf")) + list(vi_dir.glob("*_EGM.pdf"))
    latest = _latest_year(files)
    return extract_pdf_text(latest) if latest else None


def find_comparator_csv() -> Optional[Path]:
    """Newest CSV in data/_reference/comparator/, falling back to the legacy file.

    The comparator is a placeholder that will be replaced by a differently-named
    file; consumers resolve it here so only one place needs to change.
    """
    if COMPARATOR_DIR.exists():
        files = list(COMPARATOR_DIR.glob("*.csv"))
        newest = _newest_mtime(files)
        if newest:
            return newest
    if _LEGACY_COMPARATOR.exists():
        return _LEGACY_COMPARATOR
    return None


def load_comparator_rows() -> list[dict]:
    """All comparator rows as a list of dicts, or [] when not present."""
    path = find_comparator_csv()
    if path is None:
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))