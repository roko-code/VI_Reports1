"""
Remuneration Assessment Module - Stage 5

Assesses remuneration resolutions against ASA Consolidated Voting and Assessment Guidelines.
Uses source documents: Annual Report, Notice of Meeting, prior year docs, comparator CSV.
"""

import os
import json
import re
import time
import csv
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text, TechnicalLogger
from ar_text_cache import get_ar_pages
from ar_retrieval import STAGE5_SPECS, build_context, write_manifest
from citation_check import check_citations, format_problems
from disclosure_check import check_disclosed_realised
from news_search_results import load_news_block, has_news_results
from ar_file_lookup import find_ar_file as _find_ar_file_lookup
from llm_config import rem_assess_models

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

REM_ASSESS_PRIMARY_MODEL, REM_ASSESS_FALLBACK_MODEL = rem_assess_models()
BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
REFERENCE_DIR = BASE_DIR / "data" / "_reference"

COMPARATOR_CSV_PATH = REFERENCE_DIR / "ceo_kmp_comparator.csv"

# Character budget for the retrieved annual report sections. This is no longer
# a bound on how much of the document we can read (that was the original
# defect); it is a bound on prompt size. Measured in src/retrieval_selftest.py
# against all 14 cached reports.
AR_BUDGET_CHARS = int(os.getenv("AR_BUDGET_CHARS", "250000"))
PRIOR_AR_BUDGET_CHARS = int(os.getenv("PRIOR_AR_BUDGET_CHARS", "120000"))

# Notices of Meeting are 15-25 pages (45k-85k characters measured). The
# previous 50,000 character cap silently dropped the last 25-40% of the
# larger ones, including explanatory notes on remuneration deferral.
NOM_BUDGET_CHARS = int(os.getenv("NOM_BUDGET_CHARS", "120000"))


# Token bucket rate limiter
class TokenBucket:
    def __init__(self, rate_per_minute: int = 18):
        self.rate = rate_per_minute / 60.0
        self.tokens = rate_per_minute
        self.last = time.time()

    def take(self):
        now = time.time()
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.rate * elapsed + self.tokens, self.rate * 60)
        if self.tokens >= 1:
            self.tokens -= 1
            self.last = now
            return 0
        wait = (1 - self.tokens) / self.rate
        self.tokens = 0
        self.last = now
        time.sleep(wait)
        return wait

_token_bucket = TokenBucket(20)  # 20 RPM = 3s between calls


def _load_prompt(filename: str) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _load_comparator_csv() -> List[Dict[str, Any]]:
    """Load comparator CSV (folder-driven) and filter to CEO rows with the
    latest year per company."""
    from input_sources import find_comparator_csv

    path = find_comparator_csv()
    if path is None:
        return []

    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Csuite Job Title", "").lower() == "chief executive officer":
                rows.append(row)

    latest_by_company = {}
    for row in rows:
        company = row.get("Company Name", "")
        fy_str = row.get("Financial Year End", "")
        try:
            fy = int(fy_str.split("-")[0])
        except:
            continue
        if company not in latest_by_company or fy > latest_by_company[company]["fy"]:
            row["fy"] = fy
            latest_by_company[company] = row

    return list(latest_by_company.values())


def _extract_ar_pages_for_fy(ticker: str, fy: int, ar_path: Path, logger=None) -> list[dict]:
    """Get cached pages for an AR."""
    return get_ar_pages(ticker, fy, ar_path, logger=logger)


def _find_ar_file(workspace_dir: Path, year: int, *, fallback: bool = True, logger=None) -> Optional[Path]:
    """Find the AR PDF for `year`.

    Delegates to ar_file_lookup.find_ar_file; `fallback` controls whether the
    most-recent-available AR is returned when the exact year is absent. The
    FY-N vs FY-(N-1) pair used for Stage 5's prior-year comparison is exact-
    match only, so those callers pass `fallback=False`.
    """
    lookup = _find_ar_file_lookup(workspace_dir, year, fallback=fallback, logger=logger)
    return lookup.path if lookup else None


def _find_nom_file(workspace_dir: Path) -> Optional[Path]:
    """Find NoM PDF in workspace, checking multiple possible locations."""
    search_dirs = [
        workspace_dir / "notices",
        workspace_dir / "notices" / "processed",
        workspace_dir / "extracted_json" / "notices",
        workspace_dir / "extracted_json" / "notices_archived",
    ]
    for dir_path in search_dirs:
        if dir_path.exists():
            pdfs = list(dir_path.glob("*.pdf"))
            if pdfs:
                return pdfs[0]
    return None


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF using pdfplumber."""
    import pdfplumber
    text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Page {idx} ---\n" + page_text
    return text



def _extract_error_status(err: Exception) -> Optional[int]:
    """Extract HTTP status code from an OpenAI/OpenRouter error."""
    if hasattr(err, "status_code") and err.status_code:
        return int(err.status_code)
    if hasattr(err, "response") and err.response is not None:
        if hasattr(err.response, "status_code") and err.response.status_code:
            return int(err.response.status_code)
    match = re.search(r"(?:status code|error code|code)[:= ]+(\d{3})", str(err), re.I)
    if match:
        return int(match.group(1))
    return None


def _extract_error_body(err: Exception) -> dict | str | None:
    """Extract error response body from an OpenAI/OpenRouter error."""
    if hasattr(err, "response") and err.response is not None:
        try:
            return err.response.json()
        except Exception:
            try:
                return err.response.text[:2000]
            except Exception:
                pass
    return None


def _usage_to_dict(usage) -> dict | None:
    """Convert OpenAI usage object to dict."""
    if usage is None:
        return None
    result = {}
    for attr in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = getattr(usage, attr, None)
        if val is not None:
            result[attr] = val
    details = getattr(usage, "completion_tokens_details", None)
    if details:
        result["completion_tokens_details"] = {
            k: getattr(details, k, None)
            for k in ("accepted_prediction_tokens", "audio_tokens", "reasoning_tokens", "rejected_prediction_tokens", "image_tokens")
            if getattr(details, k, None) is not None
        }
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details:
        result["prompt_tokens_details"] = {
            k: getattr(prompt_details, k, None)
            for k in ("audio_tokens", "cached_tokens", "cache_write_tokens", "video_tokens")
            if getattr(prompt_details, k, None) is not None
        }
    cost = getattr(usage, "cost", None)
    if cost is not None:
        result["cost"] = cost
    return result



def _write_llm_call_metadata(supporting_data_dir: Path, call_name: str, metadata: dict) -> None:
    """Write LLM call metadata to supporting data directory."""
    if not supporting_data_dir:
        return
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", call_name)
    path = supporting_data_dir / f"llm_call_{safe_name}.json"
    try:
        write_supporting_json(supporting_data_dir, path.name, metadata)
    except Exception:
        pass


def _call_llm_text(
    client: OpenAI,
    model: str,
    prompt_text: str,
    user_content: str,
    logger=None,
    fallback_model: str = None,
    max_retries: int = 3,
    call_name: str = "llm_call",
    supporting_data_dir: Path = None,
) -> str:
    """Send a prompt to the LLM and return raw text response (no JSON parsing)."""
    call_start = time.time()

    metadata = {
        "call_name": call_name,
        "started_at": datetime.now().isoformat(),
        "primary_model": model,
        "fallback_model": fallback_model,
        "max_retries": max_retries,
        "attempts": [],
        "rate_limit_wait_seconds": 0,
        "status": "pending",
    }

    def _write_metadata():
        metadata["elapsed_seconds"] = round(time.time() - call_start, 2)
        _write_llm_call_metadata(supporting_data_dir, call_name, metadata)

    wait = _token_bucket.take()
    if wait > 0:
        metadata["rate_limit_wait_seconds"] = wait
        if logger:
            logger.info(f"[{call_name}] Rate limiting: waiting {wait:.1f}s")
        time.sleep(wait)

    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": user_content}
        ],
        "timeout": 120.0
    }

    primary_error = None
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(**request_kwargs)
            choices = getattr(response, "choices", None)
            if not choices:
                error_msg = f"No choices returned from LLM (possible rate limit). Response id: {getattr(response, 'id', 'N/A')}"
                if logger:
                    logger.warning(f"[{call_name}] {error_msg}")
                raise RuntimeError(error_msg)

            content = response.choices[0].message.content
            if content is None:
                raise RuntimeError("LLM returned no text content")
            content = content.strip()
            if not content:
                raise RuntimeError("LLM returned empty text content")

            attempt_meta = {
                "attempt": attempt,
                "model": model,
                "started_at": datetime.now().isoformat(),
                "elapsed_seconds": round(time.time() - call_start, 2),
                "response_id": getattr(response, "id", None),
                "response_model": getattr(response, "model", None),
                "usage": _usage_to_dict(getattr(response, "usage", None)),
                "status": "success",
                "is_fallback": False,
            }
            metadata["attempts"].append(attempt_meta)
            metadata["status"] = "success"
            metadata["model_used"] = model
            metadata["fallback_used"] = False
            _write_metadata()

            if logger:
                logger.info(
                    f"[{call_name}] success: model={model} elapsed={round(time.time() - call_start, 2):.1f}s"
                )
            return content

        except Exception as err1:
            status_code = _extract_error_status(err1)
            error_body = _extract_error_body(err1)

            attempt_meta = {
                "attempt": attempt,
                "model": model,
                "started_at": datetime.now().isoformat(),
                "elapsed_seconds": round(time.time() - call_start, 2),
                "status": "error",
                "is_fallback": False,
                "error": str(err1),
                "error_status": status_code,
                "error_body": error_body,
            }
            metadata["attempts"].append(attempt_meta)
            _write_metadata()

            if logger:
                logger.warning(
                    f"[{call_name}] error: model={model} status={status_code} error={str(err1)[:500]}"
                )

            is_rate_limit = status_code == 429 or "429" in str(err1) or "rate limit" in str(err1).lower()

            if is_rate_limit and attempt < max_retries:
                wait_time = 2 ** attempt
                if logger:
                    logger.warning(f"[{call_name}] Rate limit hit (attempt {attempt + 1}/{max_retries + 1}), waiting {wait_time}s: {err1}")
                print(f"  [LLM] Rate limit hit, waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue

            primary_error = err1

            if logger:
                logger.warning(f"[{call_name}] Primary model ({model}) failed: {err1}")
            print(f"  [LLM] Primary model ({model}) failed: {err1}")

            if not fallback_model:
                metadata["status"] = "failed"
                metadata["final_error"] = str(err1)
                _write_metadata()
                raise

            if logger:
                logger.info(f"[{call_name}] Falling back to model: {fallback_model}")
            print(f"  [LLM] Falling back to model: {fallback_model}")

            fallback_request_kwargs = {
                "model": fallback_model,
                "messages": request_kwargs["messages"],
                "timeout": 120.0
            }

            try:
                fb_wait = _token_bucket.take()
                if fb_wait > 0:
                    time.sleep(fb_wait)

                response = client.chat.completions.create(**fallback_request_kwargs)
                choices = getattr(response, "choices", None)
                if not choices:
                    error_msg = f"Fallback model also returned no choices. Response id: {getattr(response, 'id', 'N/A')}"
                    if logger:
                        logger.error(f"[{call_name}] {error_msg}")
                    raise RuntimeError(error_msg)

                content = response.choices[0].message.content
                if content is None:
                    raise RuntimeError("Fallback LLM returned no text content")
                content = content.strip()
                if not content:
                    raise RuntimeError("Fallback LLM returned empty text content")

                attempt_meta = {
                    "attempt": attempt,
                    "model": fallback_model,
                    "started_at": datetime.now().isoformat(),
                    "elapsed_seconds": round(time.time() - call_start, 2),
                    "response_id": getattr(response, "id", None),
                    "response_model": getattr(response, "model", None),
                    "usage": _usage_to_dict(getattr(response, "usage", None)),
                    "status": "success",
                    "is_fallback": True,
                }
                metadata["attempts"].append(attempt_meta)
                metadata["status"] = "success"
                metadata["model_used"] = fallback_model
                metadata["fallback_used"] = True
                _write_metadata()

                if logger:
                    logger.info(
                        f"[{call_name}] fallback success: model={fallback_model} elapsed={round(time.time() - call_start, 2):.1f}s"
                    )
                return content

            except Exception as err2:
                fb_status = _extract_error_status(err2)
                fb_error_meta = {
                    "attempt": attempt,
                    "model": fallback_model,
                    "started_at": datetime.now().isoformat(),
                    "elapsed_seconds": round(time.time() - call_start, 2),
                    "status": "error",
                    "is_fallback": True,
                    "error": str(err2),
                    "error_status": fb_status,
                }
                metadata["attempts"].append(fb_error_meta)
                if "429" in str(err2) or "rate limit" in str(err2).lower():
                    if logger:
                        logger.error(f"[{call_name}] Fallback model also rate limited: {err2}")
                    raise RuntimeError(f"Both primary and fallback models rate limited: {err2}")
                metadata["status"] = "failed"
                metadata["final_error"] = f"Primary: {primary_error}; Fallback: {err2}"
                _write_metadata()
                raise

    metadata["status"] = "failed"
    metadata["final_error"] = "Unexpected end of function"
    _write_metadata()
    raise RuntimeError("Unexpected end of _call_llm_text")


def run_remuneration_assessment(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None
) -> bool:
    """
    Run remuneration assessment for a company/year.

    Returns True if assessment completed successfully.
    """
    if logger is None:
        logger = TechnicalLogger(Path("technical_log.md"))

    workspace_dir = Path(workspace_dir)
    supporting_data_dir = Path(supporting_data_dir)
    supporting_data_dir.mkdir(parents=True, exist_ok=True)

    workspace_name = workspace_dir.name
    match = re.match(r"([A-Z]+)_(\d{4})", workspace_name)
    if match:
        ticker = match.group(1)
        year = int(match.group(2))
    else:
        raise ValueError(f"Workspace directory name must be in format TICKER_YEAR, got: {workspace_name}")

    ticker = ticker.upper()
    year = int(year)

    logger.stage_start(f"Stage 5: Remuneration Assessment for {ticker} FY{year}")

    # Find source documents
    nom_path = _find_nom_file(workspace_dir)
    ar_path = _find_ar_file(workspace_dir, year)
    prior_ar_path = _find_ar_file(workspace_dir, year - 1, fallback=False)

    logger.info(f"NOM path: {nom_path}")
    logger.info(f"AR path: {ar_path}")
    logger.info(f"Prior AR path: {prior_ar_path}")

    # Folder-driven supplementary sources (2026-10-10): the standalone CGS, the
    # prior ASA VI/AGM report, and the latest ASA guidelines are loaded from
    # their folders, not from prompt-embedded text, so updating a file in the
    # folder is enough to change what is assessed. Loaded before the document
    # inventory so docs_supplied can report their availability.
    from input_sources import (
        load_asa_guidelines, load_cgs_text, load_latest_meeting_text,
        load_latest_vi_text,
    )
    cgs_text = load_cgs_text(workspace_dir)
    prior_vi_text = load_latest_vi_text(workspace_dir)
    prior_meeting_text = load_latest_meeting_text(workspace_dir)
    asa_guidelines_text = load_asa_guidelines()

    # Document inventory
    from input_sources import find_comparator_csv as _find_comp
    docs_supplied = {
        "current_ar": ar_path is not None,
        "nom": nom_path is not None,
        "prior_ar": prior_ar_path is not None,
        "prior_asa_report": bool(prior_vi_text or prior_meeting_text),
        "cgs": bool(cgs_text),
        "asa_guidelines": bool(asa_guidelines_text),
        "peer_reports": False,
        "comparator_csv": _find_comp() is not None,
    }
    logger.info(f"Documents supplied: {docs_supplied}")

    # Extract text from documents
    # Use cached AR page extraction for efficiency
    if ar_path:
        logger.info(f"Extracting current AR text for {ticker} FY{year}")
        ar_pages = _extract_ar_pages_for_fy(ticker, year, ar_path, logger=logger)
        ar_retrieval = build_context(
            ar_pages,
            STAGE5_SPECS,
            budget_chars=AR_BUDGET_CHARS,
            logger=logger,
        )
        ar_text = ar_retrieval.text
        write_manifest(
            ar_retrieval, supporting_data_dir, "stage5", ticker, year, logger=logger
        )
        if ar_retrieval.missing_mandatory:
            logger.warning(
                "Stage 5 could not locate mandatory remuneration sections: "
                + ", ".join(ar_retrieval.missing_mandatory)
                + ". Rules depending on these will be assessed as Uncertain."
            )
    else:
        ar_pages = []
        ar_retrieval = None
        ar_text = ""
        logger.warning("No current annual report found.")

    if nom_path:
        logger.info(f"Extracting NoM text")
        nom_text = _extract_pdf_text(nom_path)
        logger.info(f"NoM text length: {len(nom_text)} chars")
    else:
        nom_text = ""
        logger.warning("No Notice of Meeting found.")

    if prior_ar_path:
        logger.info(f"Extracting prior AR text for {ticker} FY{year - 1}")
        prior_ar_pages = _extract_ar_pages_for_fy(ticker, year - 1, prior_ar_path, logger=logger)
        # Retrieved by section for the same reason as the current AR: the
        # prior-year remuneration tables sit past the first 80,000 characters.
        prior_retrieval = build_context(
            prior_ar_pages,
            STAGE5_SPECS,
            budget_chars=PRIOR_AR_BUDGET_CHARS,
            logger=logger,
        )
        prior_ar_text = prior_retrieval.text
        if supporting_data_dir:
            write_manifest(
                prior_retrieval, supporting_data_dir,
                "stage5_prior_ar", ticker, year - 1, logger=logger,
            )
        logger.info(
            f"Prior AR retrieval: {len(prior_retrieval.included_pages)}/"
            f"{prior_retrieval.total_pages} pages, "
            f"{prior_retrieval.chars_used:,} chars"
        )
    else:
        prior_ar_text = ""
        logger.warning("No prior annual report found.")

    comparator_data = _load_comparator_csv()
    logger.info(f"Comparator CSV rows (filtered CEO): {len(comparator_data)}")

    # Pre-fetched news (Stage 7.5). Supplementary context, not a source of
    # record: the model verifies company match and treats each item as an
    # unverified lead. Missing news is not an error - load_news_block returns
    # a standard "not available" marker in that case.
    news_block = load_news_block(workspace_dir)
    if logger:
        logger.info(f"News block: {'present' if has_news_results(workspace_dir) else 'not available'}")

    if logger:
        logger.info(
            "Supplementary sources: "
            f"cgs={'present' if cgs_text else 'absent'}, "
            f"prior_vi={'present' if prior_vi_text else 'absent'}, "
            f"prior_agm={'present' if prior_meeting_text else 'absent'}, "
            f"asa_guidelines={len(asa_guidelines_text)} chars"
        )

    # Load prompt
    prompt_text = _load_prompt("rem_assess.txt")

    # Build user content with all source documents.
    # The annual report is retrieved by section rather than truncated by
    # position, because the statutory pay table, realised remuneration and
    # malus/clawback disclosures sit well past the first 80,000 characters in
    # every report measured.
    ar_text_trunc = ar_text if ar_text else "NOT SUPPLIED"
    nom_text_trunc = nom_text[:NOM_BUDGET_CHARS] if nom_text else "NOT SUPPLIED"
    prior_ar_text_trunc = prior_ar_text if prior_ar_text else "NOT SUPPLIED"
    coverage_note = ar_retrieval.coverage_note() if ar_retrieval else "NOT SUPPLIED"

    prior_asa_block = "NOT SUPPLIED"
    if prior_vi_text or prior_meeting_text:
        prior_asa_block = ""
        if prior_vi_text:
            prior_asa_block += "--- Prior ASA Voting Intentions report ---\n" + prior_vi_text + "\n\n"
        if prior_meeting_text:
            prior_asa_block += "--- Prior ASA AGM/EGM meeting report ---\n" + prior_meeting_text + "\n\n"

    cgs_trunc = cgs_text if cgs_text else "NOT SUPPLIED"
    asa_trunc = asa_guidelines_text if asa_guidelines_text else "NOT SUPPLIED"

    user_content = f"""
COMPANY: {ticker}
FINANCIAL_YEAR: {year}

{coverage_note}

=== DOCUMENT 1: CURRENT ANNUAL REPORT (retrieved sections) ===
{ar_text_trunc}

=== DOCUMENT 2: NOTICE OF MEETING ===
{nom_text_trunc}

=== DOCUMENT 4: PRIOR YEAR ANNUAL REPORT (if available) ===
{prior_ar_text_trunc}

=== DOCUMENT 5: PRIOR ASA VOTING INTENTIONS / AGM REPORT (if available) ===
{prior_asa_block}

=== DOCUMENT 7: COMPARATOR CSV (CEO/KMP data) ===
Available companies: {len(comparator_data)} CEO rows

=== DOCUMENT 8: NEWS SEARCH RESULTS (pre-fetched, ~12 months) ===
{news_block}

=== DOCUMENT 9: ASA GUIDELINES (latest, loaded from the guidelines folder) ===
{asa_trunc}

=== DOCUMENT 10: CORPORATE GOVERNANCE STATEMENT (standalone CGS, if available) ===
{cgs_trunc}
"""

    # Call LLM for assessment (expecting Markdown output, not JSON)
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    logger.info(f"Calling LLM with primary model: {REM_ASSESS_PRIMARY_MODEL}")
    llm_output = _call_llm_text(
        client=client,
        model=REM_ASSESS_PRIMARY_MODEL,
        prompt_text=prompt_text,
        user_content=user_content,
        logger=logger,
        fallback_model=REM_ASSESS_FALLBACK_MODEL,
        call_name="rem_assess",
        supporting_data_dir=supporting_data_dir
    )

    # Save result as Markdown
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Remuneration_Assessment.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(llm_output, encoding="utf-8")

    # Also save as JSON structure for downstream pipeline consumption
    result_json_path = workspace_dir / "extracted_json" / f"{ticker}_{year}_Remuneration_Assessment.json"
    try:
        # Try to parse the LLM Markdown output into a structured JSON
        # The LLM output is Markdown with tables - we save it as-is with metadata
        result_json = {
            "ticker": ticker,
            "year": year,
            "documents_supplied": docs_supplied,
            "content_type": "markdown",
            "raw_content": llm_output,
            "assessed_at": datetime.now().isoformat(),
        }
        with open(result_json_path, "w", encoding="utf-8") as f:
            json.dump(result_json, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Could not save structured JSON: {e}")

    if supporting_data_dir:
        write_supporting_text(supporting_data_dir, "rem_assess_raw_output.md", llm_output)

    # Citation integrity. Stage 5 has never validated its [p.N] citations, so
    # an invented or out-of-scope page number was indistinguishable from a good
    # one. Checked against the pages actually supplied, logged rather than
    # enforced - see citation_check for why existence is the cheap half and
    # accuracy is not attempted here.
    try:
        supplied = (ar_retrieval.included_pages if ar_retrieval else None)
        if supplied is None:
            rep = None
        else:
            rep = check_citations(llm_output, supplied)
            for problem in format_problems(rep):
                logger.warning(f"Stage 5 citation check: {problem}")
            logger.info(f"Stage 5 citation check: {rep.summary()}")
    except Exception as e:                      # never fail the stage on this
        logger.warning(f"Stage 5 citation check could not run: {e}")

    # Realised-remuneration disclosure check. The worst failure mode is a false
    # `not_disclosed` terminal answer: if the retrieved pages carry a CEO realised
    # row but the model says realised pay is not disclosed, surface it. Logged,
    # not enforced, so it never blocks a run; a human reviewer sees the warning.
    try:
        if ar_retrieval is not None:
            supplied_numbers = set(ar_retrieval.included_pages)
            supplied_pages = {
                p.get("page_number"): (p.get("text") or "")
                for p in ar_pages
                if p.get("page_number") in supplied_numbers
            }
            for problem in check_disclosed_realised(llm_output, supplied_pages):
                logger.warning(f"Stage 5 disclosure check: {problem}")
    except Exception as e:                      # never fail the stage on this
        logger.warning(f"Stage 5 disclosure check could not run: {e}")

    logger.stage_end("Stage 5: Remuneration Assessment", True)
    return True


# Alias for orchestrator compatibility
run_pipeline = run_remuneration_assessment


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        ticker = sys.argv[1]
        year = int(sys.argv[2])
        workspace = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(f"data/companies/{ticker}_{year}")
        supporting = workspace / "supporting_data" / "stage5_remuneration_assessment"
        result = run_remuneration_assessment(workspace, supporting)
        print(f"Result: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/remuneration_assess.py <ticker> <year> [workspace_dir]")
        sys.exit(1)
