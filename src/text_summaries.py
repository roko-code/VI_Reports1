"""
Stage 8 - Text Summaries

Generates human-readable text summaries of all extracted data for a company:
- Financial performance summary (from performance_history_5yr.json)
- Remuneration summary (from Remuneration_Assessment.md and .json)
- Director assessment summary (from Director_Evaluation.md)
- Other resolutions summary (from Other_Resolutions_Assessment.json)
- News search summary (from news_prefetch Stage 9.5 output)

Output is a single Markdown report saved to:
  reports/{TICKER}_{YEAR}_Analysis_Section.md
  extracted_json/{TICKER}_{YEAR}_Text_Summaries.json
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
from news_search_results import load_news_block
from llm_config import text_summary_models

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

TEXT_SUMMARY_MODEL, TEXT_SUMMARY_FALLBACK_MODEL = text_summary_models()

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"


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

    for attempt in range(max_retries + 1):
        try:
            _token_bucket.take()
            response = client.chat.completions.create(**request_kwargs)
            choices = getattr(response, "choices", None)
            if not choices:
                raise RuntimeError(f"No choices returned from LLM (possible rate limit)")

            content = response.choices[0].message.content
            if not content or not content.strip():
                raise RuntimeError("LLM returned empty content")

            content = content.strip()
            if logger:
                logger.info(f"[{call_name}] success: model={model} elapsed={round(time.time() - call_start, 1)}s")
            return content

        except Exception as err:
            if attempt < max_retries:
                if logger:
                    logger.warning(f"[{call_name}] error (attempt {attempt + 1}/{max_retries + 1}): {str(err)[:300]}")
                time.sleep(2 ** attempt)
                continue

            if fallback_model and attempt >= max_retries:
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
                        logger.error(f"[{call_name}] fallback also failed: {str(fb_err)[:300]}")

            if logger:
                logger.error(f"[{call_name}] all attempts failed")
            raise




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


def _load_performance_data(workspace_dir: Path) -> Dict[str, Any]:
    """Load performance history JSON."""
    perf_path = workspace_dir / "extracted_json" / "performance_history_5yr.json"
    if perf_path.exists():
        try:
            with open(perf_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_remuneration_data(workspace_dir: Path) -> Dict[str, Any]:
    """Load remuneration assessment JSON."""
    rem_json_path = workspace_dir / "extracted_json" / f"{workspace_dir.name}_Remuneration_Assessment.json"
    if not rem_json_path.exists():
        # Try wildcard
        json_files = list((workspace_dir / "extracted_json").glob("*Remuneration_Assessment.json"))
        if json_files:
            rem_json_path = json_files[0]
    
    if rem_json_path and rem_json_path.exists():
        try:
            with open(rem_json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_director_data(workspace_dir: Path) -> str:
    """Load director evaluation markdown."""
    reports_dir = workspace_dir / "reports"
    md_files = list(reports_dir.glob("*Director_Evaluation*"))
    if md_files:
        try:
            return md_files[0].read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _load_other_resolutions_data(workspace_dir: Path) -> Dict[str, Any]:
    """Load other resolutions assessment JSON."""
    reports_dir = workspace_dir / "reports"
    json_files = list(reports_dir.glob("*Other_Resolutions_Assessment*"))
    if json_files:
        try:
            with open(json_files[0], "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _build_summary_payload(
    workspace_dir: Path,
    ticker: str,
    year: int,
    company_name: str,
    logger: TechnicalLogger,
) -> str:
    """Build the payload for the text summary LLM call."""
    parts = []

    # 1. Company identification
    parts.append(f"## Company Information\n")
    parts.append(f"- Ticker: {ticker}")
    parts.append(f"- Year: {year}")
    parts.append(f"- Company Name: {company_name}")
    parts.append("")

    # 2. Performance data
    perf_data = _load_performance_data(workspace_dir)
    if perf_data:
        parts.append("## Financial Performance (from performance_history_5yr.json)\n")
        extraction_status = perf_data.get("extraction_status", "unknown")
        parts.append(f"Extraction status: {extraction_status}")
        years = perf_data.get("years", {})
        if years:
            parts.append(f"Number of years with data: {len(years)}")
            for fy, fy_data in sorted(years.items()):
                parts.append(f"\n### FY{fy}:")
                for metric in ["npat", "underlying_npat", "revenue", "share_price", "dividend", "eps", "net_assets", "roe", "roce", "simple_tsr"]:
                    val = fy_data.get(metric)
                    source_page = fy_data.get(f"{metric}_source_page")
                    if val is not None:
                        source_str = f" (p.{source_page})" if source_page else ""
                        parts.append(f"  - {metric}: {val}{source_str}")
                missing = fy_data.get("missing_metrics", [])
                if missing:
                    parts.append(f"  - Missing: {', '.join(missing)}")

        ceo_pay = perf_data.get("ceo_realised_pay", {})
        if ceo_pay:
            parts.append("\n### CEO Realised Pay:")
            for fy, pay_data in sorted(ceo_pay.items()):
                parts.append(f"\n  FY{fy}: ${pay_data.get('value_m', 0)}m")
                parts.append(f"    - Method: {pay_data.get('source_method', 'N/A')}")
                parts.append(f"    - Page: {pay_data.get('source_page', 'N/A')}")
                notes = pay_data.get("notes", "")
                if notes:
                    parts.append(f"    - Notes: {notes}")
        parts.append("")
    else:
        parts.append("## Financial Performance\n*Not available*\n")

    # 3. Remuneration assessment
    rem_data = _load_remuneration_data(workspace_dir)
    if rem_data:
        parts.append("## Remuneration Assessment\n")
        docs = rem_data.get("documents_supplied", {})
        parts.append(f"Documents supplied: {json.dumps(docs)}")
        parts.append(f"Content type: {rem_data.get('content_type', 'unknown')}")
        raw_content = rem_data.get("raw_content", "")
        if raw_content:
            # Append the raw markdown content (truncated to keep within limits)
            max_rem_chars = 15000
            if len(raw_content) > max_rem_chars:
                parts.append(f"\n--- Remuneration Assessment Output (truncated to {max_rem_chars} chars) ---\n")
                parts.append(raw_content[:max_rem_chars])
                parts.append("\n...[truncated]...")
            else:
                parts.append(f"\n--- Remuneration Assessment Output ---\n")
                parts.append(raw_content)
        parts.append("")
    else:
        parts.append("## Remuneration Assessment\n*Not available*\n")

    # 4. Director assessment
    dir_text = _load_director_data(workspace_dir)
    if dir_text:
        parts.append("## Director Election Assessment\n")
        max_dir_chars = 15000
        if len(dir_text) > max_dir_chars:
            parts.append(f"\n--- Director Evaluation (truncated to {max_dir_chars} chars) ---\n")
            parts.append(dir_text[:max_dir_chars])
            parts.append("\n...[truncated]...")
        else:
            parts.append(f"\n--- Director Evaluation ---\n")
            parts.append(dir_text)
        parts.append("")
    else:
        parts.append("## Director Election Assessment\n*Not available*\n")

    # 5. Other resolutions
    other_data = _load_other_resolutions_data(workspace_dir)
    if other_data:
        parts.append("## Other Resolutions Assessment\n")
        resolutions = other_data.get("resolution_categorisation", [])
        in_scope = other_data.get("in_scope_resolutions", [])
        # Stage 7's in_scope_resolutions items carry resolution_number +
        # ai_result (strict schema); titles/categories live on
        # resolution_categorisation.
        title_by_res = {r.get("resolution_number"): r.get("title", "") for r in resolutions}
        category_by_res = {r.get("resolution_number"): r.get("category", "") for r in resolutions}
        parts.append(f"Total resolutions found: {len(resolutions)}")
        parts.append(f"In-scope resolutions assessed: {len(in_scope)}")
        for res in in_scope[:10]:  # limit to avoid huge prompts
            res_num = res.get("resolution_number", "?")
            parts.append(f"\n  - Resolution {res_num}: {title_by_res.get(res_num, 'N/A')}")
            parts.append(f"    - Category: {category_by_res.get(res_num, 'N/A')}")
            result = res.get("ai_result", "")
            if result:
                parts.append(f"    - Result: {result}")
        parts.append("")
    else:
        parts.append("## Other Resolutions Assessment\n*Not available*\n")

    # 6. News search results
    news_block = load_news_block(workspace_dir)
    parts.append("## News Search Results\n")
    parts.append(f"\n--- Begin News Search Results Block ---\n")
    parts.append(news_block)
    parts.append(f"\n--- End News Search Results Block ---\n")

    # 7-9. Folder-driven reference sources (2026-10-10): latest ASA guidelines,
    # standalone CGS, and prior ASA VI/AGM report. Reference context only;
    # truncated to keep the summary prompt bounded.
    from input_sources import (
        load_asa_guidelines, load_cgs_text, load_latest_meeting_text,
        load_latest_vi_text,
    )
    asa_text = load_asa_guidelines()
    cgs_text = load_cgs_text(workspace_dir)
    prior_vi_text = load_latest_vi_text(workspace_dir)
    prior_meeting_text = load_latest_meeting_text(workspace_dir)

    parts.append("\n## ASA Guidelines (latest, reference)\n")
    parts.append(asa_text[:8000] if asa_text else "*Not available*")
    if cgs_text:
        parts.append("\n## Corporate Governance Statement (standalone CGS)\n")
        parts.append(cgs_text[:8000])
    if prior_vi_text:
        parts.append("\n## Prior ASA Voting Intentions Report\n")
        parts.append(prior_vi_text[:5000])
    if prior_meeting_text:
        parts.append("\n## Prior ASA AGM/EGM Meeting Report\n")
        parts.append(prior_meeting_text[:3000])

    return "\n".join(parts)


def run_text_summaries(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None,
) -> bool:
    """
    Run Stage 8 - Text Summaries generation.

    Reads all extracted data from previous stages and calls the LLM to
    generate a human-readable analysis section summarising the company's
    governance and financial highlights.

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

    logger.stage_start(f"Stage 8: Text Summaries for {ticker} FY{year}")

    # Extract company name from NoM JSON
    company_name = _extract_company_name(workspace_dir)
    if not company_name:
        company_name = ticker
        logger.warning(f"Could not extract company name from NoM JSON for {ticker}, using ticker")

    logger.info(f"Company name: {company_name}")

    # Load the analysis section prompt
    try:
        prompt_text = _load_prompt("analysis_section.txt")
    except FileNotFoundError as e:
        logger.error(f"Could not load prompt: {e}")
        print(f"[Stage 8] Prompt file not found: {e}")
        return False

    # Build the payload with all extracted data
    payload = _build_summary_payload(workspace_dir, ticker, year, company_name, logger)
    logger.info(f"Payload size: {len(payload)} chars")

    # Save the payload for debugging
    write_supporting_text(supporting_data_dir, "stage8_payload.txt", payload)

    # Call LLM
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    logger.info(f"Calling LLM with primary model: {TEXT_SUMMARY_MODEL}")

    llm_output = _call_llm_text(
        client=client,
        model=TEXT_SUMMARY_MODEL,
        system_prompt=prompt_text,
        user_content=payload,
        logger=logger,
        fallback_model=TEXT_SUMMARY_FALLBACK_MODEL,
        max_retries=3,
        call_name="text_summaries",
        supporting_data_dir=supporting_data_dir,
    )

    # Save the output as Markdown
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Analysis_Section.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(llm_output, encoding="utf-8")

    # Also save a copy in supporting data
    write_supporting_text(supporting_data_dir, "analysis_section.md", llm_output)

    # Save structured metadata
    metadata = {
        "ticker": ticker,
        "year": year,
        "company_name": company_name,
        "generated_at": datetime.now().isoformat(),
        "model_used": TEXT_SUMMARY_MODEL,
        "payload_size": len(payload),
        "output_size": len(llm_output),
    }
    write_supporting_json(supporting_data_dir, "text_summaries_metadata.json", metadata)

    logger.stage_end(
        f"Stage 8: Text Summaries for {ticker} FY{year}",
        True,
        summary=f"Generated {len(llm_output)} chars of analysis"
    )

    return True


# Alias for orchestrator compatibility
run_pipeline = run_text_summaries


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        workspace = Path(sys.argv[1])
        supporting = workspace / "supporting_data" / "stage8_text_summaries"
        result = run_text_summaries(workspace, supporting)
        print(f"Result: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/text_summaries.py <workspace_dir>")
        sys.exit(1)
