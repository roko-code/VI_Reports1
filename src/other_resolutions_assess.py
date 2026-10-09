"""
Other Resolutions Assessment Module - Stage 7

Assesses non-director, non-remuneration resolutions against ASA governance,
transparency and capital management guidelines.

Uses source documents: Notice of Meeting, Annual Report,
optional resolution-specific documents (SPP booklets, prospectuses, etc.).
"""

import os
import json
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text
from ar_text_cache import get_ar_pages
from ar_retrieval import STAGE7_SPECS, build_context, write_manifest
from ar_file_lookup import find_ar_file as _find_ar_file_lookup
from llm_json import parse_llm_json
from news_search_results import load_news_block
from llm_config import other_resolutions_models

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

OTHER_RESOLUTIONS_PRIMARY_MODEL, OTHER_RESOLUTIONS_FALLBACK_MODEL = other_resolutions_models()

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"

# Character budget for retrieved Annual Report sections. Verified in
# src/retrieval_selftest.py against all 14 cached reports.
AR_BUDGET_CHARS = int(os.getenv("AR_BUDGET_CHARS", "250000"))

# Token bucket rate limiter: 20 RPM = 3s between calls
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

_token_bucket = TokenBucket(20)

# Response format capability cache
_RESPONSE_FORMAT_SUPPORT = {}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "header": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "company": {"type": "string"},
                "financial_year": {"type": "string"},
                "documents_supplied": {"type": "array", "items": {"type": "string"}},
                "documents_missing": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["company", "financial_year", "documents_supplied", "documents_missing"]
        },
        "resolution_categorisation": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "resolution_number": {"type": "string"},
                    "title": {"type": "string"},
                    "category": {"type": "string", "enum": ["director_election", "remuneration", "in_scope", "no_vote_business"]}
                },
                "required": ["resolution_number", "title", "category"]
            }
        },
        "in_scope_resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "resolution_number": {"type": "string"},
                    "rules_applied": {"type": "array", "items": {"type": "string"}},
                    "ai_result": {"type": "string", "enum": ["P", "Pt", "F", "U", "N/A"]},
                    "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "evidence_comparison_action": {"type": "string"},
                    "missing_document_note": {"type": ["string", "null"]}
                },
                "required": ["resolution_number", "rules_applied", "ai_result", "confidence", "evidence_comparison_action", "missing_document_note"]
            }
        },
        "governance_context_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "rule": {"type": "string"},
                    "ai_result": {"type": "string"},
                    "confidence": {"type": "string"},
                    "evidence_comparison_action": {"type": "string"}
                },
                "required": ["rule", "ai_result", "confidence", "evidence_comparison_action"]
            }
        },
        "section_13_self_check": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "item": {"type": "string"},
                    "completed": {"type": "boolean"},
                    "note": {"type": "string"}
                },
                "required": ["item", "completed", "note"]
            }
        }
    },
    "required": ["header", "resolution_categorisation", "in_scope_resolutions", "governance_context_notes", "section_13_self_check"]
}


def _load_prompt(filename: str) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _find_nom_json(workspace_dir: Path) -> Optional[Path]:
    """Find NoM JSON in notices directory (checking both active and archived)."""
    search_dirs = [
        workspace_dir / "extracted_json" / "notices",
        workspace_dir / "extracted_json" / "notices_archived",
        workspace_dir / "notices",
        workspace_dir / "notices" / "processed",
    ]
    for search_dir in search_dirs:
        if search_dir.exists():
            jsons = list(search_dir.glob("*.json"))
            if jsons:
                return jsons[0]
    return None


def _load_nom_json(nom_json_path: Path) -> dict:
    with open(nom_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_ar_file(workspace_dir: Path, year: int, *, fallback: bool = True, logger=None) -> Optional[Path]:
    """Find the AR PDF for `year` (delegates to ar_file_lookup).

    Falls back to the most recent available AR when the target year is absent,
    so Stage 7's supplementary-document lookup still resolves for late/
    unreported issuers.
    """
    lookup = _find_ar_file_lookup(workspace_dir, year, fallback=fallback, logger=logger)
    return lookup.path if lookup else None


def _check_response_format_support(model: str) -> bool:
    if model in _RESPONSE_FORMAT_SUPPORT:
        return _RESPONSE_FORMAT_SUPPORT[model]
    supports = model.startswith("openai/gpt-4.1") or model.startswith("openai/gpt-4o")
    _RESPONSE_FORMAT_SUPPORT[model] = supports
    return supports


def _parse_json_from_response(content: str) -> dict:
    """Parse JSON from an LLM response (delegates to the shared hardened parser)."""
    return parse_llm_json(content)


def _extract_error_status(err: Exception) -> Optional[int]:
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
    if usage is None:
        return None
    result = {}
    for attr in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = getattr(usage, attr, None)
        if val is not None:
            result[attr] = val
    cost = getattr(usage, "cost", None)
    if cost is not None:
        result["cost"] = cost
    return result


def _write_llm_call_metadata(supporting_data_dir: Path, call_name: str, metadata: dict) -> None:
    if not supporting_data_dir:
        return
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", call_name)
    path = supporting_data_dir / f"llm_call_{safe_name}.json"
    try:
        write_supporting_json(supporting_data_dir, path.name, metadata)
    except Exception:
        pass


def _call_llm(
    client: OpenAI,
    model: str,
    prompt_text: str,
    user_content: str,
    logger=None,
    fallback_model: str = None,
    max_retries: int = 3,
    call_name: str = "llm_call",
    supporting_data_dir: Path = None,
    json_validation_retries: int = 2,
    schema: dict = None,
) -> dict:
    """Send a prompt to the LLM and return parsed JSON response with validation retries."""
    call_start = time.time()

    metadata = {
        "call_name": call_name,
        "started_at": datetime.now().isoformat(),
        "primary_model": model,
        "fallback_model": fallback_model,
        "max_retries": max_retries,
        "json_validation_retries": json_validation_retries,
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

    use_strict_json = schema and _check_response_format_support(model)

    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": user_content}
        ],
        "timeout": 240.0
    }
    if use_strict_json and schema:
        request_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": call_name.replace(".", "_"),
                "strict": True,
                "schema": schema
            }
        }
    elif schema:
        # Use simple json_object response format (not schema-based) for non-Strict models
        request_kwargs["response_format"] = {
            "type": "json_object"
        }

    primary_error = None
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(**request_kwargs)
            choices = getattr(response, "choices", None)
            if not choices:
                raise RuntimeError("No choices returned from LLM")

            content = response.choices[0].message.content
            if content is None or not content.strip():
                raise RuntimeError("LLM returned no text content")

            # Save raw response for debugging
            if supporting_data_dir and attempt == 0:
                try:
                    write_supporting_text(supporting_data_dir, f"{call_name}_raw_response.txt", content)
                except Exception:
                    pass

            parsed = _parse_json_from_response(content.strip())

            attempt_meta = {
                "attempt": attempt,
                "model": model,
                "status": "success",
                "is_fallback": False,
                "usage": _usage_to_dict(getattr(response, "usage", None)),
                "response_id": getattr(response, "id", None),
                "elapsed_seconds": round(time.time() - call_start, 2),
            }
            metadata["attempts"].append(attempt_meta)
            metadata["status"] = "success"
            metadata["model_used"] = model
            metadata["fallback_used"] = False
            _write_metadata()

            if logger:
                logger.info(
                    f"[{call_name}] success: model={model} elapsed={attempt_meta['elapsed_seconds']:.1f}s"
                )
            return parsed

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
                    logger.warning(f"[{call_name}] Rate limit, waiting {wait_time}s: {err1}")
                print(f"  [LLM] Rate limit hit, waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue

            is_json_error = (
                "Expected JSON object" in str(err1)
                or "expected JSON object" in str(err1)
                or "Expecting value" in str(err1)
                or "Extra data" in str(err1)
                or "JSONDecodeError" in type(err1).__name__
            )
            if is_json_error and attempt < json_validation_retries:
                if logger:
                    logger.warning(f"[{call_name}] JSON parse failed, retrying ({attempt + 1}/{json_validation_retries})")
                print(f"  [LLM] JSON parse failed, retrying...")

                # Get the raw content from the last response for context
                raw_content = ""
                if attempt_meta.get("error"):
                    raw_content = ""  # We don't have raw content here, but the correction message helps

                correction_msg = (
                    f"Your previous response failed JSON parsing. "
                    f"Error: {str(err1)}. "
                    f"Please return ONLY valid JSON matching the exact schema described in the system prompt. "
                    f"Do not include markdown formatting, code blocks, or any explanatory text."
                )
                request_kwargs["messages"] = [
                    {"role": "system", "content": prompt_text},
                    {"role": "user", "content": user_content},
                    {"role": "user", "content": correction_msg}
                ]
                time.sleep(1)
                continue

            primary_error = err1

            if not fallback_model:
                metadata["status"] = "failed"
                metadata["final_error"] = str(err1)
                _write_metadata()
                raise

            if logger:
                logger.info(f"[{call_name}] Falling back to {fallback_model}")
            print(f"  [LLM] Falling back to model: {fallback_model}")

            try:
                fb_wait = _token_bucket.take()
                if fb_wait > 0:
                    time.sleep(fb_wait)

                fallback_request_kwargs = {
                    "model": fallback_model,
                    "messages": request_kwargs["messages"],
                    "timeout": 240.0
                }
                use_strict_fallback = _check_response_format_support(fallback_model)
                if use_strict_fallback and schema:
                    fallback_request_kwargs["response_format"] = request_kwargs.get("response_format")
                elif schema:
                    fallback_request_kwargs["response_format"] = {"type": "json_object"}

                response = client.chat.completions.create(**fallback_request_kwargs)
                choices = getattr(response, "choices", None)
                if not choices:
                    raise RuntimeError("Fallback returned no choices")
                content = response.choices[0].message.content
                if content is None or not content.strip():
                    raise RuntimeError("Fallback returned no text")

                parsed = _parse_json_from_response(content.strip())

                attempt_meta = {
                    "attempt": attempt,
                    "model": fallback_model,
                    "status": "success",
                    "is_fallback": True,
                    "usage": _usage_to_dict(getattr(response, "usage", None)),
                    "response_id": getattr(response, "id", None),
                    "elapsed_seconds": round(time.time() - call_start, 2),
                }
                metadata["attempts"].append(attempt_meta)
                metadata["status"] = "success"
                metadata["model_used"] = fallback_model
                metadata["fallback_used"] = True
                _write_metadata()
                return parsed

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
                if status_code == 429 or (fb_status == 429) or "rate limit" in str(err2).lower():
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
    raise RuntimeError("Unexpected end of _call_llm")


def run_other_resolutions_assessment(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None
) -> bool:
    """
    Run other resolutions assessment for a company/year.

    Returns True if assessment completed successfully.
    """
    if logger is None:
        from logging_utils import TechnicalLogger
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
        raise ValueError(f"Workspace directory name must be TICKER_YEAR format, got: {workspace_name}")

    if logger:
        logger.stage_start(f"Stage 7: Other Resolutions Assessment for {ticker} FY{year}")

    # Find source documents
    nom_json_path = _find_nom_json(workspace_dir)
    ar_path = _find_ar_file(workspace_dir, year)

    if not nom_json_path:
        print(f"[Stage 7] No NoM JSON found in {workspace_dir}")
        if logger:
            logger.error("No NoM JSON found for other resolutions assessment")
        return False

    if not ar_path:
        print(f" [Stage 7] No Annual Report found for FY{year}")
        if logger:
            logger.error(f"No Annual Report found for FY{year}")
        return False

    # Load documents
    nom_data = _load_nom_json(nom_json_path)

    # Use cached AR pages if available
    # If using a fallback AR (different year than target), use the AR's actual year for cache key
    ar_year = year
    ar_match = re.search(r'(\d{4})', ar_path.stem)
    if ar_match:
        ar_year = int(ar_match.group(1))
    ar_pages = get_ar_pages(ticker, ar_year, ar_path, logger=logger)
    ar_retrieval = build_context(
        ar_pages,
        STAGE7_SPECS,
        budget_chars=AR_BUDGET_CHARS,
        logger=logger,
    )
    ar_text = ar_retrieval.text
    if supporting_data_dir:
        write_manifest(
            ar_retrieval, supporting_data_dir, "stage7", ticker, year, logger=logger
        )
    if logger and ar_retrieval.missing_mandatory:
        logger.warning(
            "Stage 7 could not locate mandatory sections: "
            + ", ".join(ar_retrieval.missing_mandatory)
            + ". Rules depending on these will be assessed as Uncertain."
        )

    # Pre-fetched news (Stage 7.5). Supplementary context that may bear on
    # specific resolutions; the prompt verifies company match and treats
    # items as unverified leads.
    news_block = load_news_block(workspace_dir)
    if logger:
        logger.info(f"Stage 7 news context: {'supplied' if news_block.strip() else 'not available'}")

    # Load prompt
    prompt_text = _load_prompt("other_resolutions_assess.txt")

    # Build user content with source documents
    user_content = f"""
COMPANY: {ticker}
FINANCIAL_YEAR: {year}

{ar_retrieval.coverage_note()}

=== INPUT 1: NOTICE OF MEETING EXTRACTED JSON ===
{json.dumps(nom_data, indent=2)}

=== INPUT 2: ANNUAL REPORT (retrieved sections) ===
{ar_text}

=== DOCUMENT 4: NEWS SEARCH RESULTS (pre-fetched, ~12 months) ===
{news_block}
"""

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    logger.info(f"Calling LLM with primary model: {OTHER_RESOLUTIONS_PRIMARY_MODEL}")
    result = _call_llm(
        client=client,
        model=OTHER_RESOLUTIONS_PRIMARY_MODEL,
        prompt_text=prompt_text,
        user_content=user_content,
        logger=logger,
        fallback_model=OTHER_RESOLUTIONS_FALLBACK_MODEL,
        call_name="other_resolutions",
        supporting_data_dir=supporting_data_dir,
        schema=SCHEMA,
    )

    # Save result
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Other_Resolutions_Assessment.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    if supporting_data_dir:
        write_supporting_json(supporting_data_dir, "other_resolutions_result.json", result)

    if logger:
        logger.stage_end("Stage 7: Other Resolutions Assessment", True)

    return True




# Alias for orchestrator compatibility
run_pipeline = run_other_resolutions_assessment


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        workspace = Path(sys.argv[1])
        supporting = workspace / "supporting_data" / "stage7_other_resolutions"
        result = run_other_resolutions_assessment(workspace, supporting)
        print(f"Result: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/other_resolutions_assess.py <workspace_dir>")
        sys.exit(1)
