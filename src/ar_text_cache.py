"""
AR Text Cache Module

Persistent, cross-run cache for annual report extracted text.
Stores page-by-page text with metadata for invalidation and diagnostics.
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional

import pdfplumber

from ar_retrieval import is_unusable_text

# Cache configuration
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "_cache" / "annual_report_text"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Extractor version - increment when PDF extraction logic changes to force cache regeneration
# 1.1.0 adds unusable-page detection (see _extract_ar_pages).
EXTRACTOR_VERSION = "1.1.0"


def _get_cache_path(ticker: str, fy: int) -> Path:
    """Get cache file path for a ticker/fiscal year."""
    return CACHE_DIR / f"{ticker.upper()}_{fy}.json"


def _compute_file_hash(pdf_path: Path) -> str:
    """Compute SHA256 hash of PDF file for audit/integrity."""
    hasher = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _get_file_stat(pdf_path: Path) -> tuple[int, float]:
    """Get file size and modification time."""
    stat = pdf_path.stat()
    return stat.st_size, stat.st_mtime


def _is_cache_valid(cached_meta: dict, pdf_path: Path) -> bool:
    """Check whether a cache entry still describes the PDF on disk.

    Validity is decided by SHA-256 whenever the entry recorded one, so that an
    identical PDF copied into a new workspace (with a different mtime) is still
    a cache hit. Size and mtime remain the fallback for entries written before
    hashes were recorded. The extractor version is always enforced, because
    changing extraction logic must invalidate existing entries.
    """
    if not pdf_path.exists():
        return False

    if cached_meta.get("extractor_version") != EXTRACTOR_VERSION:
        return False

    current_size, current_mtime = _get_file_stat(pdf_path)

    cached_hash = cached_meta.get("file_hash")
    if cached_hash:
        return _compute_file_hash(pdf_path) == cached_hash

    cached_size = cached_meta.get("file_size")
    cached_mtime = cached_meta.get("file_mtime")
    if cached_size is None or cached_mtime is None:
        return False

    return cached_size == current_size and abs(cached_mtime - current_mtime) < 1.0


def _extract_ar_pages(pdf_path: Path) -> tuple[list[dict], list[int], list[int]]:
    """
    Extract all page text from a PDF.

    Pages whose text is undecodable glyph noise are marked ``usable: False``
    with their text removed. A PDF that embeds a font without a usable
    character map yields raw ``(cid:N)`` tokens instead of letters; this is not
    repairable at read time (pypdf, pdfminer and MuPDF all resolve it the same
    way). Left in place, such text consumes the retrieval budget, can match
    keywords by accident, and would be presented to the model as evidence.

    Returns (pages_list, page_char_counts, unusable_page_numbers).
    """
    pages = []
    char_counts = []
    unusable: list[int] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            usable = not is_unusable_text(page_text)
            if not usable:
                unusable.append(idx)
                page_text = ""
            pages.append(
                {"page_number": idx, "text": page_text, "usable": usable}
            )
            char_counts.append(len(page_text))

    return pages, char_counts, unusable


def _load_cached_ar_pages(ticker: str, fy: int) -> Optional[dict]:
    """Load cached AR pages if cache file exists."""
    cache_path = _get_cache_path(ticker, fy)
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cached_ar_pages(ticker: str, fy: int, cache_data: dict) -> None:
    """Save AR pages to cache with full metadata."""
    cache_path = _get_cache_path(ticker, fy)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # Best effort


def get_ar_pages(ticker: str, fy: int, pdf_path: Path, logger=None) -> list[dict]:
    """
    Get AR page texts, using cache if valid, otherwise extract and cache.
    
    Returns list of {"page_number": int, "text": str}.
    """
    ticker = ticker.upper()
    
    # Try cache first
    cached = _load_cached_ar_pages(ticker, fy)
    if cached:
        if _is_cache_valid(cached, Path(pdf_path)):
            if logger:
                logger.info(f"Cache HIT: {ticker}_{fy} ({len(cached['pages'])} pages)")
            return cached["pages"]
        else:
            if logger:
                logger.info(f"Cache MISS (invalidated): {ticker}_{fy}")
    
    # Cache miss or invalid - extract fresh
    if logger:
        logger.info(f"Cache MISS: {ticker}_{fy} - extracting from PDF")
    
    pages, char_counts, unusable = _extract_ar_pages(Path(pdf_path))

    # Build cache entry with full metadata
    file_size, file_mtime = _get_file_stat(Path(pdf_path))
    file_hash = _compute_file_hash(Path(pdf_path))

    cache_data = {
        "ticker": ticker,
        "fy": fy,
        "extractor_version": EXTRACTOR_VERSION,
        "extraction_timestamp": datetime.now().isoformat(),
        "file_size": file_size,
        "file_mtime": file_mtime,
        "file_hash": file_hash,
        "page_count": len(pages),
        "unreadable_pages": unusable,
        "page_char_counts": char_counts,
        "total_chars": sum(char_counts),
        "pages": pages,
    }

    _save_cached_ar_pages(ticker, fy, cache_data)

    if logger:
        logger.info(
            f"Cached: {ticker}_{fy} ({len(pages)} pages, {sum(char_counts)} chars)"
            + (f", {len(unusable)} unreadable pages" if unusable else "")
        )

    return pages


def clear_cache(ticker: Optional[str] = None, fy: Optional[int] = None) -> int:
    """
    Clear cache entries.
    If ticker and fy provided, clear specific entry.
    If only ticker, clear all years for that ticker.
    If neither, clear all cache.
    Returns number of files removed.
    """
    removed = 0
    
    if ticker and fy:
        cache_path = _get_cache_path(ticker, fy)
        if cache_path.exists():
            cache_path.unlink()
            removed = 1
    elif ticker:
        for cache_file in CACHE_DIR.glob(f"{ticker.upper()}_*.json"):
            cache_file.unlink()
            removed += 1
    else:
        for cache_file in CACHE_DIR.glob("*.json"):
            cache_file.unlink()
            removed += 1
    
    return removed


def get_cache_info(ticker: str, fy: int) -> Optional[dict]:
    """Get cache metadata without loading full page text."""
    cached = _load_cached_ar_pages(ticker, fy)
    if not cached:
        return None
    
    # Return metadata without pages
    return {k: v for k, v in cached.items() if k != "pages"}