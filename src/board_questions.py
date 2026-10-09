"""
Stage 9 - Board Questions Generator

Generates board-level questions based on all upstream pipeline data.
Uses the board_questions.txt prompt and consumes:
- Remuneration Assessment output (Markdown + JSON)
- Other Resolutions Assessment output (JSON)
- Director Election Assessment output (Markdown)
- News Search Results (from Stage 7.5 news_prefetch)
- Annual Report text (via ar_text_cache)

Output is saved to:
  reports/{TICKER}_{YEAR}_Board_Questions.md
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text, TechnicalLogger
from ar_text_cache import get_ar_pages
from ar_retrieval import STAGE9_SPECS, build_context, write_manifest
from news_search_results import load_news_block, has_news_results
from ar_file_lookup import find_ar_file as _find_ar_file_lookup
from llm_config import board_questions_models

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

BOARD_QUESTIONS_MODEL, BOARD_QUESTIONS_FALLBACK_MODEL = board_questions_models()

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"

# Character budget for retrieved Annual Report sections. Verified in
# src/retrieval_selftest.py against all 14 cached reports.
AR_BUDGET_CHARS = int(os.getenv("AR_BUDGET_CHARS", "150000"))


class TokenBucket:
    def __init__(self, rate_per_minute: int = 20):
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

_token_bucket = TokenBucket(20)


def _load_prompt(filename: str) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _find_ar_file(workspace_dir: Path, year: int, *, fallback: bool = True, logger=None) -> Optional[Path]:
    """Find the AR PDF for `year` in workspace (delegates to ar_file_lookup)."""
    lookup = _find_ar_file_lookup(workspace_dir, year, fallback=fallback, logger=logger)
    return lookup.path if lookup else None


def _extract_company_name(workspace_dir: Path) -> str:
    """Extract company name from NoM JSON."""
    nom_dirs = [
        workspace_dir / "extracted_json" / "notices",
        workspace_dir / "extracted_json" / "notices_archived",
    ]
    for nom_dir in nom_dirs:
        if nom_dir.exists():
            for json_file in nom_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        nom_data = json.load(f)
                    company_name = (
                        nom_data.get("AGM_Details", {}).get("company_name")
                        or nom_data.get("company_name")
                    )
                    if company_name:
                        return company_name
                except (json.JSONDecodeError, OSError):
                    continue
    return ""


def _extract_ar_text(workspace_dir: Path, ticker: str, year: int, logger=None,
                     supporting_data_dir: Path = None) -> tuple:
    """Retrieve the Annual Report sections relevant to board questions.

    Returns (text, retrieval_result). The result may be None when no Annual
    Report was available. Sections are selected by keyword across the whole
    document rather than by taking the first N characters, because a 40,000
    character prefix reaches roughly 2-8% of a typical report and misses the
    risk management and board composition disclosures entirely.
    """
    ar_path = _find_ar_file(workspace_dir, year)
    if not ar_path:
        if logger:
            logger.warning(f"No AR PDF found for {ticker} FY{year}")
        return "", None

    try:
        pages = get_ar_pages(ticker, year, ar_path, logger=logger)
        result = build_context(
            pages,
            STAGE9_SPECS,
            budget_chars=AR_BUDGET_CHARS,
            logger=logger,
        )
        if supporting_data_dir is not None:
            write_manifest(
                result, supporting_data_dir, "stage9", ticker, year, logger=logger
            )
        if logger:
            logger.info(
                f"AR retrieval: {len(result.included_pages)}/{result.total_pages} pages, "
                f"{result.chars_used:,} chars, span {result.page_range}"
            )
        if result.missing_mandatory:
            logger.warning(
                "Stage 9 could not locate mandatory sections: "
                + ", ".join(result.missing_mandatory)
                + ". Questions depending on these will be omitted rather than guessed."
            )
        return result.text, result
    except Exception as e:
        if logger:
            logger.warning(f"AR text extraction failed: {e}")
        return "", None


def _load_remuneration_markdown(workspace_dir: Path, ticker: str, year: int) -> str:
    """Load remuneration assessment markdown output."""
    reports_dir = workspace_dir / "reports"
    md_files = list(reports_dir.glob(f"{ticker}_{year}_Remuneration_Assessment.md"))
    if not md_files:
        md_files = list(reports_dir.glob("*Remuneration_Assessment.md"))
    if md_files:
        try:
            return md_files[0].read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _load_remuneration_json(workspace_dir: Path, ticker: str, year: int) -> Dict[str, Any]:
    """Load remuneration assessment JSON output."""
    json_path = workspace_dir / "extracted_json" / f"{ticker}_{year}_Remuneration_Assessment.json"
    if not json_path.exists():
        json_files = list((workspace_dir / "extracted_json").glob("*Remuneration_Assessment.json"))
        if json_files:
            json_path = json_files[0]

    if json_path and json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_other_resolutions(workspace_dir: Path) -> str:
    """Load other resolutions assessment output as text."""
    reports_dir = workspace_dir / "reports"
    json_files = list(reports_dir.glob("*Other_Resolutions_Assessment*"))
    if json_files:
        try:
            with open(json_files[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            # Convert key fields to readable text.
            # Stage 7's in_scope_resolutions items carry resolution_number +
            # ai_result (strict schema); titles/categories live on
            # resolution_categorisation.
            parts = []
            resolutions = data.get("resolution_categorisation", [])
            in_scope = data.get("in_scope_resolutions", [])
            title_by_res = {r.get("resolution_number"): r.get("title", "") for r in resolutions}
            category_by_res = {r.get("resolution_number"): r.get("category", "") for r in resolutions}
            parts.append(f"Total resolutions found: {len(resolutions)}")
            parts.append(f"In-scope resolutions assessed: {len(in_scope)}")
            for res in in_scope:
                res_num = res.get("resolution_number", "?")
                parts.append(f"\n  Resolution {res_num}: {title_by_res.get(res_num, 'N/A')}")
                result = res.get("ai_result", "")
                if result:
                    parts.append(f"  Result: {result}")
                category = category_by_res.get(res_num, "")
                if category:
                    parts.append(f"  Category: {category}")
            return "\n".join(parts)
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def _call_llm_text(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_content: str,
    logger=None,
    fallback_model: Optional[str] = None,
    max_retries: int = 3,
    call_name: str = "llm_call",
    supporting_data_dir: Optional[Path] = None,
) -> str:
    """Send prompt to LLM and return raw text response with fallback."""
    call_start = time.time()

    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "timeout": 120.0
    }

    primary_error = None
    for attempt in range(max_retries + 1):
        try:
            _token_bucket.take()
            response = client.chat.completions.create(**request_kwargs)
            choices = getattr(response, "choices", None)
            if not choices:
                raise RuntimeError("No choices returned from LLM")

            content = response.choices[0].message.content
            if not content or not content.strip():
                raise RuntimeError("LLM returned empty content")

            content = content.strip()
            if logger:
                logger.info(f"[{call_name}] success: model={model} elapsed={round(time.time() - call_start, 1)}s")
            return content

        except Exception as err:
            primary_error = err
            if attempt < max_retries:
                if logger:
                    logger.warning(f"[{call_name}] error (attempt {attempt + 1}/{max_retries + 1}): {str(err)[:300]}")
                time.sleep(2 ** attempt)
                continue

            if not fallback_model:
                break

            if logger:
                logger.info(f"[{call_name}] Falling back to {fallback_model}")
            try:
                _token_bucket.take()
                fb_response = client.chat.completions.create(
                    model=fallback_model,
                    messages=request_kwargs["messages"],
                    timeout=120.0
                )
                content = fb_response.choices[0].message.content
                if content and content.strip():
                    if logger:
                        logger.info(f"[{call_name}] fallback success: model={fallback_model}")
                    return content.strip()
            except Exception as fb_err:
                if logger:
                    logger.error(f"[{call_name}] fallback failed: {str(fb_err)[:300]}")

    if logger:
        logger.error(f"[{call_name}] all attempts failed")
    raise RuntimeError(f"All LLM attempts failed: {primary_error}")


def _build_board_questions_payload(
    workspace_dir: Path,
    ticker: str,
    year: int,
    company_name: str,
    logger: TechnicalLogger,
    supporting_data_dir: Optional[Path] = None,
) -> str:
    """Build the payload for the board questions LLM call."""
    parts = []
    docs_supplied = []

    parts.append("# Board Questions Generation\n")
    parts.append(f"COMPANY: {ticker}")
    parts.append(f"FULL COMPANY NAME: {company_name}")
    parts.append(f"FINANCIAL YEAR: {year}\n")

    # Document 1: Annual Report (current)
    ar_text, ar_retrieval = _extract_ar_text(
        workspace_dir, ticker, year, logger=logger,
        supporting_data_dir=supporting_data_dir,
    )
    if ar_text:
        docs_supplied.append("1. Current Annual Report")
        parts.append("=== DOCUMENT 1: Current Annual Report (retrieved sections) ===\n")
        if ar_retrieval is not None:
            parts.append(ar_retrieval.coverage_note())
            parts.append("\n")
        parts.append(ar_text)
        parts.append("\n")
    else:
        parts.append("=== DOCUMENT 1: Current Annual Report ===\n*Not available*\n\n")

    # Document 2: Corporate Governance Statement
    # CGS is typically within the AR - note if we have the AR
    if ar_text:
        docs_supplied.append("2. Corporate Governance Statement (within AR)")
        parts.append("=== DOCUMENT 2: Corporate Governance Statement ===\n")
        parts.append("Included within the Annual Report text above.\n\n")
    else:
        parts.append("=== DOCUMENT 2: Corporate Governance Statement ===\n*Not available*\n\n")

    # Document 3: Remuneration Assessment output
    rem_md = _load_remuneration_markdown(workspace_dir, ticker, year)
    rem_json = _load_remuneration_json(workspace_dir, ticker, year)
    if rem_md:
        docs_supplied.append("3. Remuneration Assessment output")
        parts.append("=== DOCUMENT 3: Remuneration Assessment Output ===\n")
        parts.append(rem_md[:20000])
        parts.append("\n\n")
    elif rem_json and rem_json.get("raw_content"):
        docs_supplied.append("3. Remuneration Assessment output")
        parts.append("=== DOCUMENT 3: Remuneration Assessment Output ===\n")
        parts.append(rem_json["raw_content"][:20000])
        parts.append("\n\n")
    else:
        parts.append("=== DOCUMENT 3: Remuneration Assessment Output ===\n*Not available*\n\n")

    # Document 4: Other Resolutions Assessment output
    other_text = _load_other_resolutions(workspace_dir)
    if other_text:
        docs_supplied.append("4. Other Resolutions Assessment output")
        parts.append("=== DOCUMENT 4: Other Resolutions Assessment Output ===\n")
        parts.append(other_text[:10000])
        parts.append("\n\n")
    else:
        parts.append("=== DOCUMENT 4: Other Resolutions Assessment Output ===\n*Not available*\n\n")

    # Document 5: News Search Results
    news_block = load_news_block(workspace_dir)
    if has_news_results(workspace_dir):
        docs_supplied.append("5. News Search Results (pre-fetched)")
        parts.append("=== DOCUMENT 5: News Search Results (pre-fetched) ===\n")
        parts.append(news_block)
        parts.append("\n\n")
    else:
        parts.append("=== DOCUMENT 5: News Search Results (pre-fetched) ===\n")
        parts.append("No news results were available for this company.\n\n")

    # Document 6: Prior year VI Report (optional). Only *_VIR.pdf counts:
    # AGM/EGM reports share this directory and are not VI material.
    vi_dir = workspace_dir / "vi_reports"
    if vi_dir.exists() and list(vi_dir.glob("*_VIR.pdf")):
        docs_supplied.append("6. Prior year VI Report (available)")
    else:
        parts.append("=== DOCUMENT 6: Prior year ASA Voting Intentions Report ===\n*Not available*\n\n")

    # Document 7: Prior year AGM Report (optional)
    # Check in vi_reports or a separate directory
    agm_report_found = any(vi_dir.glob("*AGM*")) if vi_dir.exists() else False
    if agm_report_found:
        docs_supplied.append("7. Prior year AGM Report (available)")
    else:
        parts.append("=== DOCUMENT 7: Prior year ASA AGM Report ===\n*Not available*\n\n")

    # Document inventory
    parts.append("=== DOCUMENT INVENTORY ===\n")
    parts.append(f"Documents supplied: {', '.join(docs_supplied) if docs_supplied else 'none'}")
    if len(docs_supplied) < 5:
        parts.append(f"Documents not supplied: {[str(i) for i in range(1, 8) if i not in [int(d.split('.')[0].strip()[-1]) for d in docs_supplied]]}")

    return "\n".join(parts)


def run_board_questions(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None,
) -> bool:
    """
    Run Stage 9 - Board Questions Generator.

    Consumes all upstream assessment outputs and the news search results
    to generate 10 targeted questions for the Board at the AGM.

    Returns True if successful.
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
        ticker = workspace_name.split("_")[0]
        year = int(workspace_name.split("_")[1]) if len(workspace_name.split("_")) > 1 else datetime.now().year

    logger.stage_start(f"Stage 9: Board Questions for {ticker} FY{year}")

    # Extract company name from NoM JSON
    company_name = _extract_company_name(workspace_dir)
    if not company_name:
        company_name = ticker
        logger.warning(f"Could not extract company name from NoM JSON for {ticker}, using ticker")

    logger.info(f"Company name: {company_name}")

    # Load the board questions prompt
    try:
        prompt_text = _load_prompt("board_questions.txt")
    except FileNotFoundError as e:
        logger.error(f"Could not load prompt: {e}")
        print(f"[Stage 9] Prompt file not found: {e}")
        return False

    # Build the payload with all upstream data
    payload = _build_board_questions_payload(
        workspace_dir, ticker, year, company_name, logger,
        supporting_data_dir=supporting_data_dir,
    )
    logger.info(f"Payload size: {len(payload)} chars")

    # Save the payload for debugging
    write_supporting_text(supporting_data_dir, "stage9_payload.txt", payload)

    # Call LLM
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    logger.info(f"Calling LLM with primary model: {BOARD_QUESTIONS_MODEL}")

    llm_output = _call_llm_text(
        client=client,
        model=BOARD_QUESTIONS_MODEL,
        system_prompt=prompt_text,
        user_content=payload,
        logger=logger,
        fallback_model=BOARD_QUESTIONS_FALLBACK_MODEL,
        max_retries=3,
        call_name="board_questions",
        supporting_data_dir=supporting_data_dir,
    )

    # Save the output as Markdown
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Board_Questions.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(llm_output, encoding="utf-8")

    # Also save a copy in supporting data
    write_supporting_text(supporting_data_dir, "board_questions.md", llm_output)

    # Save structured metadata
    metadata = {
        "ticker": ticker,
        "year": year,
        "company_name": company_name,
        "generated_at": datetime.now().isoformat(),
        "model_used": BOARD_QUESTIONS_MODEL,
        "payload_size": len(payload),
        "output_size": len(llm_output),
    }
    write_supporting_json(supporting_data_dir, "board_questions_metadata.json", metadata)

    logger.stage_end(
        f"Stage 9: Board Questions for {ticker} FY{year}",
        True,
        summary=f"Generated {len(llm_output)} chars, {llm_output.count('Category')} question categories"
    )

    return True


# Alias for orchestrator compatibility
run_pipeline = run_board_questions


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        workspace = Path(sys.argv[1])
        supporting = workspace / "supporting_data" / "stage9_board_questions"
        result = run_board_questions(workspace, supporting)
        print(f"Result: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/board_questions.py <workspace_dir>")
        sys.exit(1)
