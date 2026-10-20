import os
import json
import re
import time
from pathlib import Path
from datetime import datetime
import pdfplumber
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text
from currency_anchor import (anchored_figures, contains_aud_million,
                            near_miss_currencies, reporting_currency_for)
from disclosure_check import disclosed_realised_figures
from ar_text_cache import get_ar_pages
from ar_retrieval import (build_context, write_manifest,
                         STAGE4_FINANCIAL_SPECS, STAGE4_REMUNERATION_SPECS)
from pipeline_db import init_run_db
from llm_config import performance_models
from rate_limiter import get_bucket, rate_limited_call, RateLimitExceeded

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

PERFORMANCE_MODEL, PERFORMANCE_FALLBACK_MODEL = performance_models()
BASE_DIR = Path(__file__).resolve().parent.parent

# Shared rate limiter bucket for Stage 4
_rate_bucket = get_bucket("stage4_performance_table")

# JSON Schemas for LLM response validation
PROMPT_1A_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "asx_code": {"type": "string"},
        "table_found": {"type": "boolean"},
        "extraction_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "years": {
            "type": "object",
            "patternProperties": {
                "^\\d{4}$": {
                    "type": "object",
                    "properties": {
                        "npat": {"type": ["number", "string", "null"]},
                        "npat_source_page": {"type": ["integer", "string", "null"]},
                        "underlying_npat": {"type": ["number", "string", "null"]},
                        "underlying_npat_source_page": {"type": ["integer", "string", "null"]},
                        "underlying_npat_basis_note": {"type": "string"},
                        "share_price": {"type": ["number", "string", "null"]},
                        "share_price_source_page": {"type": ["integer", "string", "null"]},
                        "share_price_restated": {"type": "boolean"},
                        "share_price_restatement_note": {"type": "string"},
                        "dividend": {"type": ["number", "string", "null"]},
                        "dividend_source_page": {"type": ["integer", "string", "null"]},
                        "dividend_paid": {"type": ["number", "string", "null"]},
                        "dividend_paid_source_page": {"type": ["integer", "string", "null"]},
                        "simple_tsr": {"type": ["number", "string", "null"]},
                        "simple_tsr_source_page": {"type": ["integer", "string", "null"]},
                        "tsr_mismatch": {"type": "boolean"},
                        "eps": {"type": ["number", "string", "null"]},
                        "eps_source_page": {"type": ["integer", "string", "null"]},
                        "underlying_eps": {"type": ["number", "string", "null"]},
                        "underlying_eps_source_page": {"type": ["integer", "string", "null"]},
                        "revenue": {"type": ["number", "string", "null"]},
                        "revenue_source_page": {"type": ["integer", "string", "null"]},
                        "net_assets": {"type": ["number", "string", "null"]},
                        "net_assets_source_page": {"type": ["integer", "string", "null"]},
                        "roe": {"type": ["number", "string", "null"]},
                        "roe_source_page": {"type": ["integer", "string", "null"]},
                        "roce": {"type": ["number", "string", "null"]},
                        "roce_source_page": {"type": ["integer", "string", "null"]},
                        "repeated_value_flag": {"type": "boolean"},
                        "extracted_from_latest_ar": {"type": "boolean"},
                        "missing_metrics": {"type": "array", "items": {"type": "string"}}
                    },
                    "additionalProperties": False
                }
            },
            "additionalProperties": False
        }
    },
    "required": ["company", "asx_code", "table_found", "extraction_confidence", "years"]
}

PROMPT_1B_SCHEMA = {
    "type": "object",
    "properties": {
        "share_price_overrides": {
            "type": "object",
            "patternProperties": {
                "^\\d{4}$": {
                    "type": "object",
                    "properties": {
                        "share_price": {"type": ["number", "string", "null"]},
                        "share_price_source_page": {"type": ["integer", "string", "null"]},
                        "share_price_restated": {"type": "boolean"},
                        "share_price_restatement_note": {"type": "string"}
                    },
                    "additionalProperties": False
                }
            },
            "additionalProperties": False
        }
    },
    "required": ["share_price_overrides"]
}

PROMPT_2_SCHEMA = {
    "type": "object",
    "properties": {
        "years": {
            "type": "object",
            "patternProperties": {
                "^\\d{4}$": {
                    "type": "object",
                    "properties": {
                        "npat": {"type": ["number", "string", "null"]},
                        "npat_source_page": {"type": ["integer", "string", "null"]},
                        "underlying_npat": {"type": ["number", "string", "null"]},
                        "underlying_npat_source_page": {"type": ["integer", "string", "null"]},
                        "underlying_npat_basis_note": {"type": "string"},
                        "share_price": {"type": ["number", "string", "null"]},
                        "share_price_source_page": {"type": ["integer", "string", "null"]},
                        "share_price_restated": {"type": "boolean"},
                        "share_price_restatement_note": {"type": "string"},
                        "dividend": {"type": ["number", "string", "null"]},
                        "dividend_source_page": {"type": ["integer", "string", "null"]},
                        "dividend_paid": {"type": ["number", "string", "null"]},
                        "dividend_paid_source_page": {"type": ["integer", "string", "null"]},
                        "simple_tsr": {"type": ["number", "string", "null"]},
                        "simple_tsr_source_page": {"type": ["integer", "string", "null"]},
                        "tsr_mismatch": {"type": "boolean"},
                        "eps": {"type": ["number", "string", "null"]},
                        "eps_source_page": {"type": ["integer", "string", "null"]},
                        "underlying_eps": {"type": ["number", "string", "null"]},
                        "underlying_eps_source_page": {"type": ["integer", "string", "null"]},
                        "revenue": {"type": ["number", "string", "null"]},
                        "revenue_source_page": {"type": ["integer", "string", "null"]},
                        "net_assets": {"type": ["number", "string", "null"]},
                        "net_assets_source_page": {"type": ["integer", "string", "null"]},
                        "roe": {"type": ["number", "string", "null"]},
                        "roe_source_page": {"type": ["integer", "string", "null"]},
                        "roce": {"type": ["number", "string", "null"]},
                        "roce_source_page": {"type": ["integer", "string", "null"]},
                        "repeated_value_flag": {"type": "boolean"},
                        "extracted_from_latest_ar": {"type": "boolean"},
                        "missing_metrics": {"type": "array", "items": {"type": "string"}}
                    },
                    "additionalProperties": False
                }
            },
            "additionalProperties": False
        }
    },
    "required": ["years"]
}

PROMPT_3_SCHEMA = {
    "type": "object",
    "properties": {
        "ceo_name": {"type": "string"},
        "target_fy": {"type": "string"},
        "source_method": {"type": "string", "enum": ["disclosed", "calculated", "not_found"]},
        "basis": {"type": ["string", "null"]},
        "disclosure_checked": {"type": "boolean"},
        "disclosed_realised_rem": {"type": ["number", "string", "null"]},
        "disclosed_realised_source_page": {"type": ["integer", "string", "null"]},
        "disclosed_realised_quote": {"type": "string"},
        "ceo_realised_rem": {"type": ["number", "string", "null"]},
        "ceo_realised_rem_page": {"type": ["integer", "string", "null"]},
        "currency": {"type": ["string", "null"]},
        "ceo_realised_rem_aud": {"type": ["number", "string", "null"]},
        "fx_rate_used": {"type": ["number", "string", "null"]},
        "fx_rate_source_page": {"type": ["integer", "string", "null"]},
        "figure_fy": {"type": ["string", "null"]},
        "not_disclosed_reason": {
            "type": ["string", "null"],
            "enum": [None, "no_realised_disclosure", "non_aud_currency", "year_mismatch"]
        },
        "ceo_served_part_year": {"type": ["boolean", "null"]},
        "cross_check_calculated_m": {"type": ["number", "string", "null"]},
        "cross_check_delta_m": {"type": ["number", "string", "null"]},
        "cross_check_explained_by": {"type": ["string", "null"]},
        "cross_check_explanation": {"type": "string"},
        "cross_check_source_page": {"type": ["integer", "string", "null"]},
        "lti_status": {"type": ["string", "null"]},
        "cash_non_cash_salary": {"type": ["number", "string", "null"]},
        "cash_source_page": {"type": ["integer", "string", "null"]},
        "sti_dollars_disclosed": {"type": ["number", "string", "null"]},
        "sti_source_page": {"type": ["integer", "string", "null"]},
        "sti_max_opportunity": {"type": ["number", "string", "null"]},
        "sti_pct_of_max": {"type": ["number", "string", "null"]},
        "sti_value": {"type": ["number", "string", "null"]},
        "lti_grant_years_tested": {"type": "array", "items": {"type": ["string", "integer"]}},
        "lti_vesting_pct": {"type": ["number", "string", "null"]},
        "lti_units": {"type": ["number", "string", "null"]},
        "lti_price_used": {"type": ["number", "string", "null"]},
        "lti_price_basis": {"type": ["string", "null"]},
        "lti_fx_rate_applied": {"type": ["number", "string", "null"]},
        "lti_value": {"type": ["number", "string", "null"]},
        "lti_source_page": {"type": ["integer", "string", "null"]},
        "context_notes": {"type": "array", "items": {"type": "string"}},
        "disclosed_statutory_rem": {"type": ["number", "string", "null"]},
        "disclosed_statutory_source_page": {"type": ["integer", "string", "null"]},
        "disclosed_statutory_quote": {"type": "string"},
        "ceo_statutory_rem": {"type": ["number", "string", "null"]},
        "ceo_statutory_rem_page": {"type": ["integer", "string", "null"]},
        "statutory_currency": {"type": ["string", "null"]},
        "ceo_statutory_rem_aud": {"type": ["number", "string", "null"]},
        "statutory_basis": {"type": ["string", "null"], "enum": ["disclosed_statutory", "not_disclosed", None]},
        "statutory_not_disclosed_reason": {"type": ["string", "null"], "enum": [None, "year_mismatch"]},
    },
    "required": ["ceo_name", "target_fy", "source_method", "ceo_realised_rem", "ceo_statutory_rem"]
}

PROMPT_4_SCHEMA = {
    "type": "object",
    "properties": {
        "validation_passed": {"type": "boolean"},
        "validation_notes": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["validation_passed", "validation_notes"]
}

SCHEMA_MAP = {
    "prompt_1a": "PROMPT_1A_SCHEMA",
    "prompt_1b": "PROMPT_1B_SCHEMA",
    "prompt_2_fy": "PROMPT_2_SCHEMA",
    "prompt_3_fy": "PROMPT_3_SCHEMA",
    "prompt_4": "PROMPT_4_SCHEMA",
}

PROMPTS_DIR = BASE_DIR / "prompts"

# Response format capability cache
_RESPONSE_FORMAT_SUPPORT = {}

def _get_schema_for_call(call_name: str):
    """Get schema for call_name, handling prompt_2_fyXXX and prompt_3_fyXXX patterns."""
    if call_name in SCHEMA_MAP:
        return globals()[SCHEMA_MAP[call_name]]
    if call_name.startswith("prompt_2_fy"):
        return PROMPT_2_SCHEMA
    if call_name.startswith("prompt_3_fy"):
        return PROMPT_3_SCHEMA
    return None

def _check_response_format_support(model: str) -> bool:
    """Check if model supports response_format via OpenRouter."""
    if model in _RESPONSE_FORMAT_SUPPORT:
        return _RESPONSE_FORMAT_SUPPORT[model]
    
    # For now, only enable strict mode for models we know support it with simple schemas
    # The complex nested schemas with patternProperties don't work in strict mode
    supports = model.startswith("openai/gpt-4.1") or model.startswith("openai/gpt-4o")
    _RESPONSE_FORMAT_SUPPORT[model] = supports
    return supports

def _schema_supports_strict_mode(call_name: str) -> bool:
    """Check if the schema for this call_name supports strict mode."""
    # Only prompt_4 has a simple enough schema for strict mode
    return call_name == "prompt_4"

def _validate_against_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    """Validate data against JSON schema. Returns (is_valid, errors)."""
    try:
        import jsonschema
        jsonschema.validate(instance=data, schema=schema)
        return True, []
    except ImportError:
        # If jsonschema not available, do basic validation
        return _basic_validate(data, schema)
    except Exception as e:
        return False, [str(e)]

def _basic_validate(data: dict, schema: dict) -> tuple[bool, list[str]]:
    """Basic validation without jsonschema library."""
    errors = []
    required = schema.get("required", [])
    for field in required:
        if field not in data:
            errors.append(f"Missing required field: {field}")
    
    props = schema.get("properties", {})
    for key, value in data.items():
        if key in props:
            prop_schema = props[key]
            expected_type = prop_schema.get("type")
            if expected_type:
                if isinstance(expected_type, list):
                    if not any(_check_type(value, t) for t in expected_type):
                        errors.append(f"Field '{key}': expected one of {expected_type}, got {type(value).__name__}")
                elif not _check_type(value, expected_type):
                    errors.append(f"Field '{key}': expected {expected_type}, got {type(value).__name__}")
    
    return len(errors) == 0, errors

def _check_type(value, expected_type: str) -> bool:
    """Check if value matches expected type."""
    type_map = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    expected = type_map.get(expected_type)
    if expected is None:
        return True
    return isinstance(value, expected)
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _load_prompt(filename: str) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber."""
    text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Page {idx} ---\n" + page_text
    return text


def _find_section_pages_fast(pdf_path: Path, keywords: list, max_pages: int = None, page_range: tuple = None, logger=None) -> list:
    """Extract text page-by-page and return page numbers matching keywords."""
    pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total_pages = len(pdf.pages)
        
        if page_range:
            start, end = page_range
            start = max(1, start)
            end = min(total_pages, end)
            pages_to_check = end - start + 1
        else:
            pages_to_check = total_pages if max_pages is None else min(max_pages, total_pages)
        
        if logger:
            logger.info(f"Scanning up to {pages_to_check} pages in {pdf_path.name} for: {keywords}")
        
        for idx in range(pages_to_check):
            page_num = idx + 1
            if page_range:
                page_num = page_range[0] + idx
            
            if page_num > total_pages:
                break
                
            page = pdf.pages[page_num - 1]
            page_text = page.extract_text() or ""
            if page_text and any(kw.lower() in page_text.lower() for kw in keywords):
                pages.append(page_num)
            
            if logger and (idx + 1) % 5 == 0:
                logger.info(f"  PDF scan progress: {idx + 1}/{pages_to_check} pages...")
    
    if logger:
        logger.info(f"Found {len(pages)} matching pages in {pdf_path.name}")
    return pages


def _slice_cached_text(pages: list[dict], page_numbers: list, logger=None) -> str:
    """Extract text from specific pages of cached page text."""
    text = ""
    for idx, page_num in enumerate(page_numbers):
        if page_num <= 0 or page_num > len(pages):
            continue
        page = pages[page_num - 1]
        page_text = page.get("text", "")
        if page_text:
            text += f"\n--- Page {page_num} ---\n" + page_text
        if logger and (idx + 1) % 5 == 0:
            logger.info(f"  Slicing progress: {idx + 1}/{len(page_numbers)} pages...")
    if logger:
        logger.info(f"Slicing complete: {len(text)} chars extracted from {len(page_numbers)} pages")
    return text


def _page_count_cached(pages: list[dict]) -> int:
    """Return the page count from cached pages."""
    return len(pages)


def _find_section_pages_cached(pages: list[dict], keywords: list, logger=None) -> list:
    """Search cached page text and return page numbers matching keywords."""
    matches = []
    total_pages = len(pages)
    
    if logger:
        logger.info(f"Scanning up to {total_pages} cached pages for: {keywords}")
    
    for idx, page in enumerate(pages):
        page_num = page["page_number"]
        page_text = page.get("text", "") or ""
        if page_text and any(kw.lower() in page_text.lower() for kw in keywords):
            matches.append(page_num)
        
        if logger and (idx + 1) % 5 == 0:
            logger.info(f"  Cache scan progress: {idx + 1}/{total_pages} pages...")
    
    if logger:
        logger.info(f"Found {len(matches)} matching pages in cached text")
    return matches


def _slice_pdf_text(pdf_path: Path, page_numbers: list, logger=None) -> str:
    """Extract text from specific pages of a PDF (legacy, for backward compatibility)."""
    text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page_num in enumerate(page_numbers):
            if page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Page {page_num} ---\n" + page_text
            if logger and (idx + 1) % 5 == 0:
                logger.info(f"  Slicing progress: {idx + 1}/{len(page_numbers)} pages...")
    if logger:
        logger.info(f"Slicing complete: {len(text)} chars extracted from {len(page_numbers)} pages")
    return text


def _pdf_page_count(pdf_path: Path) -> int:
    """Return the physical page count of a PDF (legacy, for backward compatibility)."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        return len(pdf.pages)


def _expand_pages(pages: list, total_pages: int, radius: int = 1) -> list:
    """Add neighbouring physical pages around keyword matches."""
    expanded = set()
    for page_num in pages:
        start = max(1, page_num - radius)
        end = min(total_pages, page_num + radius)
        expanded.update(range(start, end + 1))
    return sorted(expanded)


def _cluster_pages(pages: list, gap: int = 3) -> list:
    """Group nearby pages so broad keyword scans can be ranked by section."""
    clusters = []
    for page_num in sorted(set(pages)):
        if not clusters or page_num > clusters[-1][-1] + gap:
            clusters.append([page_num])
        else:
            clusters[-1].append(page_num)
    return clusters


def _select_section_pages(pages: list[dict], keywords: list, logger=None, max_pages: int = 40, radius: int = 1) -> list:
    """Find relevant pages anywhere in cached page text and select the strongest page clusters."""
    matches = _find_section_pages_cached(pages, keywords, logger=logger)
    total_pages = _page_count_cached(pages)
    expanded = _expand_pages(matches, total_pages, radius)

    if not expanded:
        if logger:
            logger.info(f"No matching pages found in cached text for: {keywords}")
        return []

    if len(expanded) <= max_pages:
        selected = expanded
    else:
        clusters = _cluster_pages(expanded, gap=3)
        clusters.sort(key=lambda cluster: (len(cluster), -cluster[0]), reverse=True)
        selected = []
        for cluster in clusters:
            for page_num in cluster:
                if len(selected) >= max_pages:
                    break
                selected.append(page_num)
            if len(selected) >= max_pages:
                break
        selected = sorted(selected)

    if logger:
        logger.info(
            f"Selected {len(selected)} pages from {len(matches)} keyword matches "
            f"across {total_pages} physical pages in cached text: {selected}"
        )
    return selected


def _extract_error_status(err: Exception) -> int | None:
    """Extract HTTP status code from an OpenAI/OpenRouter error."""
    if hasattr(err, "status_code") and err.status_code:
        return int(err.status_code)
    if hasattr(err, "response") and err.response is not None:
        if hasattr(err.response, "status_code") and err.response.status_code:
            return int(err.response.status_code)
    # Try to extract from error message
    import re
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
    import re
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", call_name)
    path = supporting_data_dir / f"llm_call_{safe_name}.json"
    try:
        write_supporting_json(supporting_data_dir, path.name, metadata)
    except Exception:
        pass  # Best effort


def _call_llm(
    client: OpenAI, 
    model: str, 
    prompt_text: str, 
    user_content: str, 
    logger=None, 
    fallback_model: str = None, 
    max_retries: int = 5,
    call_name: str = "llm_call",
    supporting_data_dir: Path = None,
    json_validation_retries: int = 2
) -> dict:
    """Send a prompt to the LLM and return parsed JSON response with validation retries."""
    call_started = datetime.now().isoformat()
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
    
    def _log_success(response, used_model, attempt_idx, is_fallback):
        elapsed = round(time.time() - call_start, 2)
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("LLM returned no text content")
        content = content.strip()
        if not content:
            raise RuntimeError("LLM returned empty text content")
        
        attempt_meta = {
            "attempt": attempt_idx,
            "model": used_model,
            "started_at": datetime.now().isoformat(),
            "elapsed_seconds": elapsed,
            "response_id": getattr(response, "id", None),
            "response_model": getattr(response, "model", None),
            "usage": _usage_to_dict(getattr(response, "usage", None)),
            "status": "success",
            "is_fallback": is_fallback,
        }
        metadata["attempts"].append(attempt_meta)
        metadata["status"] = "success"
        metadata["model_used"] = used_model
        metadata["fallback_used"] = is_fallback
        _write_metadata()
        
        if logger:
            logger.info(
                f"[{call_name}] success: model={used_model} elapsed={elapsed:.1f}s "
                f"response_id={attempt_meta['response_id']} "
                f"tokens={attempt_meta['usage'].get('total_tokens') if attempt_meta['usage'] else 'N/A'} "
                f"cost={attempt_meta['usage'].get('cost') if attempt_meta['usage'] else 'N/A'}"
            )
        return content
    
    def _log_error(err, attempt_idx, used_model, is_fallback):
        status_code = _extract_error_status(err)
        error_body = _extract_error_body(err)
        
        attempt_meta = {
            "attempt": attempt_idx,
            "model": used_model,
            "started_at": datetime.now().isoformat(),
            "elapsed_seconds": round(time.time() - call_start, 2),
            "status": "error",
            "is_fallback": is_fallback,
            "error": str(err),
            "error_status": status_code,
            "error_body": error_body,
        }
        metadata["attempts"].append(attempt_meta)
        _write_metadata()
        
        if logger:
            logger.warning(
                f"[{call_name}] error: model={used_model} status={status_code} "
                f"error={str(err)[:500]}"
            )
        return status_code
    
    def _validate_and_parse(content: str, schema: dict) -> dict:
        """Parse and validate content against schema."""
        parsed = _parse_json_from_response(content)
        if schema:
            is_valid, errors = _validate_against_schema(parsed, schema)
            if not is_valid:
                raise ValueError(f"JSON validation failed: {'; '.join(errors)}")
        return parsed
    
    schema = _get_schema_for_call(call_name)
    use_strict_json = schema and _check_response_format_support(model) and _schema_supports_strict_mode(call_name)
    
    # Build request kwargs
    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": user_content}
        ],
        "timeout": 120.0
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
    elif schema and _check_response_format_support(model):
        # Non-strict JSON hint for schemas too complex for strict mode
        # (prompt_2/prompt_3 use arrays/nested nulls). This makes the provider
        # force a JSON object so the model cannot fall back to prose - which is
        # exactly what "LLM returned str, expected JSON object" was.
        request_kwargs["response_format"] = {"type": "json_object"}
    
    primary_error = None
    for attempt in range(max_retries + 1):
        try:
            def make_call():
                return client.chat.completions.create(**request_kwargs)
            
            response = rate_limited_call(
                bucket_name="stage4_performance_table",
                call_fn=make_call,
                max_retries=max_retries - attempt,
                logger=logger
            )
            
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
            
            # Validate against schema (either strict mode already enforced, or manual validation)
            if schema and not use_strict_json:
                parsed = _validate_and_parse(content, schema)
            else:
                parsed = _parse_json_from_response(content)
            
            # Success - log and return
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
                    f"[{call_name}] success: model={model} elapsed={round(time.time() - call_start, 2):.1f}s "
                    f"response_id={attempt_meta['response_id']} "
                    f"tokens={attempt_meta['usage'].get('total_tokens') if attempt_meta['usage'] else 'N/A'}"
                )
            return parsed
            
        except RateLimitExceeded as rle:
            # Daily limit exceeded - don't retry
            if logger:
                logger.error(f"[{call_name}] Daily rate limit exceeded: {rle}")
            raise
        
        except Exception as err1:
            status_code = _log_error(err1, attempt, model, is_fallback=False)
            primary_error = err1
            error_str = str(err1)
            is_rate_limit = "429" in error_str or "rate limit" in error_str.lower()
            
            if is_rate_limit and attempt < max_retries:
                # rate_limited_call handles 429 retry with backoff
                if logger:
                    logger.warning(f"[{call_name}] Rate limit hit (attempt {attempt + 1}/{max_retries + 1}), will retry")
                continue
            
            # JSON validation failure - retry with re-ask
            is_json_error = (
                "JSON validation failed" in error_str 
                or "LLM returned" in error_str 
                or "expected JSON object" in error_str
                or "Expecting value" in error_str
                or "Extra data" in error_str
                or "JSONDecodeError" in type(err1).__name__
            )
            if is_json_error:
                if attempt < json_validation_retries:
                    if logger:
                        logger.warning(f"[{call_name}] JSON validation failed, retrying (attempt {attempt + 1}/{json_validation_retries})")
                    print(f"  [LLM] JSON validation failed, retrying...")
                    
                    # Add correction message and retry
                    correction_msg = (
                        f"Your previous response failed validation: {error_str}. "
                        f"Please return ONLY valid JSON matching the required schema. "
                        f"Previous response: {content[:500] if 'content' in locals() else 'N/A'}"
                    )
                    request_kwargs["messages"] = [
                        {"role": "system", "content": prompt_text},
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": content if 'content' in locals() else ""},
                        {"role": "user", "content": correction_msg}
                    ]
                    time.sleep(1)
                    continue
            
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
            
            # Try fallback model
            fallback_request_kwargs = request_kwargs.copy()
            fallback_request_kwargs["model"] = fallback_model
            use_strict_json_fallback = _check_response_format_support(fallback_model) and schema
            if use_strict_json_fallback:
                fallback_request_kwargs["response_format"] = request_kwargs.get("response_format")
            
            try:
                def make_fallback_call():
                    return client.chat.completions.create(**fallback_request_kwargs)
                
                response = rate_limited_call(
                    bucket_name="stage4_performance_table",
                    call_fn=make_fallback_call,
                    max_retries=max_retries - attempt,
                    logger=logger
                )
                
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
                
                if schema and not use_strict_json_fallback:
                    parsed = _validate_and_parse(content, schema)
                else:
                    parsed = _parse_json_from_response(content)
                
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
                return parsed
                
            except RateLimitExceeded as rle:
                if logger:
                    logger.error(f"[{call_name}] Fallback model also rate limited: {rle}")
                raise
            
            except Exception as err2:
                _log_error(err2, attempt, fallback_model, is_fallback=True)
                if "429" in str(err2) or "rate limit" in str(err2).lower():
                    if logger:
                        logger.error(f"[{call_name}] Fallback model also rate limited: {err2}")
                    raise RuntimeError(f"Both primary and fallback models rate limited: {err2}")
                metadata["status"] = "failed"
                metadata["final_error"] = f"Primary: {primary_error}; Fallback: {err2}"
                _write_metadata()
                raise
    
    # If we get here, something went wrong
    metadata["status"] = "failed"
    metadata["final_error"] = "Unexpected end of function"
    _write_metadata()
    raise RuntimeError("Unexpected end of _call_llm")

def _validate_and_parse(content: str, schema: dict) -> dict:
    """Parse and validate content against schema."""
    parsed = _parse_json_from_response(content)
    if schema:
        is_valid, errors = _validate_against_schema(parsed, schema)
        if not is_valid:
            raise ValueError(f"JSON validation failed: {'; '.join(errors)}")
    return parsed


def _normalize_year_key(yr_str: str) -> str:
    """Normalize year strings like 'FY26', '2026', 'FY2026' to '2026'."""
    yr_str = yr_str.strip().upper()
    if yr_str.startswith("FY"):
        yr_str = yr_str[2:]
    if len(yr_str) == 2:
        yr_str = "20" + yr_str
    return yr_str


def _parse_json_from_response(content: str) -> dict:
    """Strip markdown code blocks and parse JSON from LLM response. Ensures result is a dict."""
    if content.startswith("```json"):
        content = content[7:-3].strip()
    elif content.startswith("```"):
        content = content[3:-3].strip()
    
    # Find JSON object in response (handle extra text before/after)
    start = content.find('{')
    end = content.rfind('}')
    if start >= 0 and end > start:
        content = content[start:end+1]
    
    # Fix common JSON issues
    content = content.replace("'", '"')  # single quotes to double
    content = re.sub(r',(\s*[}\]])', r'\1', content)  # trailing commas
    
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        # Try to extract first valid JSON object
        import json as json_mod
        decoder = json_mod.JSONDecoder()
        idx = 0
        while idx < len(content):
            try:
                obj, end_idx = decoder.raw_decode(content[idx:])
                parsed = obj
                break
            except json_mod.JSONDecodeError:
                idx += 1
        else:
            raise
    
    # Ensure we got a dict, not a string/number/array
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM returned {type(parsed).__name__}, expected JSON object")
    
    return parsed


def _validate_ceo_pay_semantics(data: dict) -> list[str]:
    """Semantic plausibility checks that do not need page text.

    A part-year CEO total is expected to be smaller than a full-year one, so the
    0.8m floor is waived when `ceo_served_part_year` is set. The flag is a
    qualifier on a real figure, not a reason to withhold one - NAB FY2024's
    answer is $1.74m with `ceo_served_part_year` set, not a non-disclosure.
    """
    problems: list[str] = []
    value_m = data.get("ceo_realised_rem")
    if value_m is None:
        return problems
    try:
        value_m = float(value_m)
    except (TypeError, ValueError):
        return problems
    part_year = bool(data.get("ceo_served_part_year"))
    if not part_year and value_m < 0.8:
        problems.append(
            f"implausibly low: {value_m}m is below the CEO-total band floor (0.8m)"
        )
    return problems


# A cross-check reason that reconciles a gap between two figures must be backed
# by the number it reconciles. Values describing a case with nothing to
# reconcile (unrelated / not_comparable / unexplained) are not in this set.
_RECONCILE_MECHANISMS = ("fx", "deferral_timing", "accounting_basis", "vesting_basis")


def _reconcile_ceo_value(result: dict) -> dict:
    """Repair a scale-slip on the primary realised-pay field using the model's
    own cross-check.

    The model computes `cross_check_calculated_m` by reconstructing the total
    from disclosed components, and records it independently of
    `ceo_realised_rem`. A scale slip (e.g. Macquarie FY2026 `$26,526,819`
    written as `0.021` instead of `26.53`) leaves the primary field implausibly
    low while the cross-check is correct. When that happens and the figure is a
    full-year one, prefer the cross-check - it is the number the model itself
    derived from the disclosed components and it already passed the page-level
    currency/scale anchor. Part-year figures are left alone; a part-year total
    is legitimately small and must not be inflated to a full-year cross-check.
    """
    res = dict(result)
    cross = res.get("cross_check_calculated_m")
    if cross in (None, ""):
        return res
    try:
        primary = float(res.get("ceo_realised_rem"))
    except (TypeError, ValueError):
        return res
    try:
        cross = float(cross)
    except (TypeError, ValueError):
        return res
    if bool(res.get("ceo_served_part_year")):
        return res
    if primary < 0.8 and cross >= 0.8:
        res["ceo_realised_rem"] = cross
        res["disclosed_realised_rem"] = cross
        res["reconciled_from_cross_check"] = True
    return res


def _make_ceo_pay_validator(pages_by_number: dict, company: str = None):
    """Build a closure that validates a CEO realised-pay claim against pages.

    The digit-only path is gone: a `$m` claim is now anchored to currency and
    scale per row. Rio Tinto FY24 p.131 prints `8,450` under a `(\u00a3'000)`
    heading, so a claim of 8.45m citing that page is rejected as GBP, not
    silently accepted. A bare `$` is resolved against the issuer's reporting
    currency (AUD for every ASX filer today) rather than assumed.

    `not_disclosed` is a terminal state and must be provable: it is rejected
    when a reason is missing, and when a figure was supplied alongside it.
    """
    bare_currency = reporting_currency_for(company) if company else "AUD"

    def _validator(data: dict) -> list[str]:
        errors: list[str] = []
        basis = data.get("basis")
        page_no = data.get("ceo_realised_rem_page")
        value_m = data.get("ceo_realised_rem")
        currency = data.get("currency")
        figure_fy = data.get("figure_fy")
        target_fy = data.get("target_fy")
        not_disclosed_reason = data.get("not_disclosed_reason")

        # not_disclosed is an escape hatch and must be earned: it is
        # rejected when a reason is missing, when a figure was supplied
        # alongside it, and when the pages actually publish a realised CEO
        # row. NAB FY24 p.145 carries one and the model called it
        # `no_realised_disclosure` live; that is a false negative, and the
        # rejection redirects at the part-year flag rather than at the
        # disclosure itself.
        if basis == "not_disclosed":
            if not not_disclosed_reason:
                errors.append(
                    "not_disclosed_reason must be populated when basis is not_disclosed"
                )
            if value_m is not None:
                errors.append(
                    "contradictory: a figure was supplied but basis is not_disclosed"
                )
            disclosed = disclosed_realised_figures(pages_by_number)
            if disclosed:
                first = disclosed[0]
                pg = f"p.{first.page}" if first.page is not None else "the cited page"
                errors.append(
                    f"{pg} DO disclose realised remuneration: "
                    f"{first.text[:120]!r}. The page publishes a CEO realised "
                    f"total, so 'not_disclosed' is not provable here. If the "
                    f"CEO served a part year, set ceo_served_part_year on the "
                    f"figure rather than withholding it."
                )
            return errors

        # Cross-check reconciliation: a mechanism that explains a gap between
        # two figures must be backed by the number it reconciles. "unrelated",
        # "not_comparable" and "unexplained" describe cases with nothing to
        # reconcile and so need no calculation behind them.
        explained_by = data.get("cross_check_explained_by")
        calc_m = data.get("cross_check_calculated_m")
        if explained_by in _RECONCILE_MECHANISMS and calc_m in (None, ""):
            errors.append("cross_check_calculated_m is absent")

        if page_no is None:
            errors.append("ceo_realised_rem_page required for a disclosed figure")
            return errors

        if not currency:
            errors.append("no currency was declared for the figure")
        elif currency != "AUD":
            errors.append(
                f"non_aud_currency: figure currency is {currency}, expected AUD"
            )

        if figure_fy and target_fy and figure_fy != target_fy:
            errors.append(
                f"year_mismatch: figure_fy={figure_fy} != target_fy={target_fy}"
            )

        if value_m is None:
            errors.append("ceo_realised_rem must be populated for a disclosed figure")
            return errors

        try:
            value_m = float(value_m)
        except (TypeError, ValueError):
            errors.append(f"ceo_realised_rem is not a number: {value_m!r}")
            return errors

        # Currency- and scale-anchored match, resolved BY VALUE across the
        # supplied pages rather than by the cited page number. The model is
        # instructed to cite the PRINTED page number, which (a) can differ from
        # the PDF index the pages are keyed under and (b) often comes back as a
        # string. Looking the number up as a key fails on both; scanning for the
        # value still rejects a wrong figure or a wrong currency.
        page_texts = [t for t in pages_by_number.values() if t]
        found = any(
            contains_aud_million(t, value_m, page_no, bare_currency=bare_currency)
            for t in page_texts
        )
        # A bare figure (no inline $) in an otherwise single-currency table is
        # the issuer's own currency, e.g. Macquarie's "Total 25,277,224" with no
        # currency mark. Accept it only when the same value is not ALSO present
        # in an explicit non-AUD currency; a GBP figure named as AUD still fails.
        if not found:
            for t in page_texts:
                for f in anchored_figures(t, page_no, bare_currency=bare_currency):
                    if (f.value_m is not None and abs(f.value_m - value_m) <= 0.011
                            and f.currency is None):
                        found = True
                        break
                if found:
                    break
        if not found:
            near = []
            for t in page_texts:
                near.extend(
                    near_miss_currencies(t, value_m, page_no, bare_currency=bare_currency)
                )
            if near:
                cur = ",".join(sorted({f.currency for f in near if f.currency}))
                errors.append(
                    f"currency_mismatch: {value_m}m is in the supplied pages but as "
                    f"{cur}, not AUD"
                )
            else:
                errors.append(
                    f"value_not_found: {value_m}m not found in the supplied pages"
                )

        if value_m < 0.8 and not data.get("ceo_served_part_year"):
            errors.append(
                f"implausibly low: {value_m}m is below the CEO-total band floor (0.8m)"
            )

        return errors

    return _validator


def _parse_call_name(call_name: str) -> tuple[str, int | None]:
    """Split a call_name into (extractor, fiscal_year) for checkpoint keys.

    prompt_1a / prompt_1b / prompt_4 carry no year and are keyed to 0; the
    per-year prompts (prompt_2_fy2022, prompt_3_fy2026) carry the year they
    were run for, which is what makes a partial run resumable.
    """
    m = re.search(r"fy(\d{4})", call_name, re.IGNORECASE)
    fiscal_year = int(m.group(1)) if m else None
    if call_name.startswith("prompt_1a"):
        extractor = "prompt_1a"
    elif call_name.startswith("prompt_1b"):
        extractor = "prompt_1b"
    elif call_name.startswith("prompt_2"):
        extractor = "prompt_2"
    elif call_name.startswith("prompt_3"):
        extractor = "prompt_3"
    elif call_name.startswith("prompt_4"):
        extractor = "prompt_4"
    else:
        extractor = call_name
    return extractor, fiscal_year


def _make_checkpointed_call(db, run_id: str, logger, original_call_llm):
    """Return a _call_llm-compatible wrapper backed by SQLite checkpointing.

    The wrapper is a drop-in for `_call_llm`: same signature, same return
    value. It adds per-(extractor, fiscal_year) resume so a run interrupted by
    a rate limit does not redo completed work, and it logs each attempt to
    call_log. The original `_call_llm` is called unchanged, so all its retry,
    fallback and metadata behaviour is preserved.
    """
    import threading

    _sem = threading.Semaphore(2)

    def _ensure_checkpoint(extractor: str, ck_year: int) -> None:
        """Idempotently create a pending checkpoint row if one is missing.

        start_checkpoint updates an existing row; without this a checkpoint
        that was never registered in init_checkpoints would silently no-op,
        and resume would not work for ad-hoc call names.
        """
        conn = db._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO extraction_checkpoints "
            "(run_id, extractor, fiscal_year, status, attempt) "
            "VALUES (?, ?, ?, 'pending', 0)",
            (run_id, extractor, ck_year),
        )

    def _wrapped(client, model, prompt_text, user_content, logger=None,
                 fallback_model=None, max_retries=3, call_name="llm_call",
                 supporting_data_dir=None, json_validation_retries=2):
        extractor, fiscal_year = _parse_call_name(call_name)
        ck_year = fiscal_year if fiscal_year is not None else 0

        # Resume: a completed checkpoint returns its stored result verbatim.
        cp = db.get_checkpoint_status(run_id, extractor, ck_year)
        if cp and cp.get("status") == "completed" and cp.get("result_json"):
            if logger:
                logger.info(f"[{call_name}] checkpoint completed, skipping")
            return json.loads(cp["result_json"])

        _ensure_checkpoint(extractor, ck_year)
        db.start_checkpoint(run_id, extractor, ck_year)

        with _sem:
            # Double-check inside the semaphore: another worker may have
            # completed it while we were waiting.
            cp = db.get_checkpoint_status(run_id, extractor, ck_year)
            if cp and cp.get("status") == "completed" and cp.get("result_json"):
                if logger:
                    logger.info(f"[{call_name}] checkpoint completed by another worker, skipping")
                return json.loads(cp["result_json"])

            start = time.time()
            try:
                result = original_call_llm(
                    client, model, prompt_text, user_content, logger,
                    fallback_model, max_retries, call_name,
                    supporting_data_dir, json_validation_retries,
                )
                db.log_call(
                    run_id, extractor, fiscal_year, model, 0, 0,
                    int((time.time() - start) * 1000), "success", None,
                )
                db.complete_checkpoint(
                    run_id, extractor, ck_year, result=result,
                )
                return result
            except Exception as err:
                db.log_call(
                    run_id, extractor, fiscal_year, model, 0, 0,
                    int((time.time() - start) * 1000), "error", str(err)[:500],
                )
                db.complete_checkpoint(
                    run_id, extractor, ck_year, error=str(err)[:500],
                )
                raise

    return _wrapped


# The AR text cache is keyed by ticker and fiscal year, so the same PDF
# requested under two fiscal years is extracted twice - once for the AR that
# reports the year and once for the AR that shows it as a comparative.  A
# 170-page PDF takes a while to extract, and Stage 4 needs several ARs, so the
# pages are memoised by file path for the lifetime of the process.
_AR_PAGE_MEMO: dict = {}


def _ar_pages(ticker: str, fy: int, ar_file: Path, logger=None):
    """Cached AR pages, memoised by file path within this process."""
    try:
        key = str(Path(ar_file).resolve()).lower()
    except OSError:
        key = str(ar_file).lower()
    if key not in _AR_PAGE_MEMO:
        _AR_PAGE_MEMO[key] = get_ar_pages(ticker, fy, ar_file, logger)
    return _AR_PAGE_MEMO[key]


def _normalize_eps_units(compiled: dict, logger=None) -> None:
    """Normalise EPS values that are stated in cents rather than dollars.

    The performance table reports EPS in cents per share.  Some issuers state
    the figure in cents as a plain number ("5.3"), others as a scaled number
    ("530"), and the heuristic below tells them apart: if most years exceed
    100 the values are scaled cents and are divided by 100.
    """
    for key in ("eps", "underlying_eps"):
        values = []
        for fy, yd in compiled.get("years", {}).items():
            if yd.get(key) is not None:
                try:
                    values.append((fy, float(yd[key])))
                except (ValueError, TypeError):
                    pass
        if not values:
            continue
        cents_count = sum(1 for _, v in values if v > 100)
        if cents_count < len(values) * 0.5:
            continue
        if logger:
            logger.info(f"Normalizing {key} from scaled cents for {len(values)} years")
        for fy, yd in compiled.get("years", {}).items():
            if yd.get(key) is None:
                continue
            try:
                value = float(yd[key])
            except (ValueError, TypeError):
                continue
            if value > 100:
                yd[key] = round(value / 100.0, 2)


def run_pipeline(workspace_dir: Path, supporting_data_dir: Path = None, logger=None) -> bool:
    """
    Extract 5-year financial performance history from annual reports.
    
    Strategy:
    1. Try latest AR 5-year summary table (Prompt 1a)
    2. Cross-check share prices with remuneration-performance link (Prompt 1b)
    3. Fill gaps from individual ARs (Prompt 2 placeholder)
    4. Extract CEO realised pay from each AR (Prompt 3 placeholder)
    5. Compile and validate (Prompt 4 placeholder)
    
    Returns True if at least some data was extracted, False otherwise.
    """
    import time
    
    annual_reports_dir = workspace_dir / "annual_reports"
    ar_files = sorted(annual_reports_dir.glob("*.pdf"))
    
    if not ar_files:
        print(" No annual reports found.")
        if logger:
            logger.warning("No annual reports found for performance table extraction")
        return False
    
    # Sort by year descending - latest first
    ar_files_sorted = sorted(ar_files, key=lambda p: int(re.search(r"(\d{4})", p.stem).group(1)), reverse=True)
    latest_ar_path = ar_files_sorted[0]
    latest_year = int(re.search(r"(\d{4})", latest_ar_path.stem).group(1))
    ticker = workspace_dir.name.split("_")[0]

    # Get cached pages for latest AR
    latest_ar_pages = _ar_pages(ticker, latest_year, latest_ar_path, logger)
    
    print(f" Latest AR: {latest_ar_path.name} (FY{latest_year})")
    if logger:
        logger.info(f"Latest AR: {latest_ar_path.name} (FY{latest_year})")
    
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY
    )

    # Checkpointing: wrap _call_llm so each (extractor, fiscal_year) call is
    # resumable. A run interrupted by a rate limit resumes from the last
    # completed checkpoint instead of redoing work. The wrapper is local to
    # this function, so every call site below picks it up unchanged.
    _llm_call = _call_llm
    if supporting_data_dir is not None:
        # run_id is deterministic (the workspace name, e.g. "SUN_2026") so a
        # re-run resumes completed checkpoints instead of starting fresh. A
        # fresh uuid per invocation would defeat the whole point of resuming
        # after a rate-limit interruption.
        db, run_id = init_run_db(supporting_data_dir, run_id=workspace_dir.name)
        years = list(range(latest_year - 4, latest_year + 1))
        db.init_checkpoints(
            run_id,
            ["prompt_1a", "prompt_1b", "prompt_2", "prompt_3", "prompt_4"],
            years,
        )
        _llm_call = _make_checkpointed_call(db, run_id, logger, _call_llm)
        if logger:
            logger.info(f"Checkpointing enabled: run_id={run_id}")
    else:
        run_id = None

    # Initialize compiled result
    compiled = {
        "company": "",
        "asx_code": ticker,
        "current_fy": str(latest_year),
        "extracted_at": datetime.now().isoformat(),
        "table_found": False,
        "extraction_confidence": None,
        "years": {},
        "ceo_realised_pay": {},
        "ceo_statutory_pay": {},
        "audit_notes": []
    }
    
# ============================================================
    # Step 1: Extract 5-year table from latest AR (Prompt 1a)
    # ============================================================
    if logger:
        logger.stage_start("Stage 4a: Extract 5-Year Table from Latest AR")
    
    t0 = time.time()
    # Use retrieval specs for targeted section extraction
    ar_budget = int(os.getenv("AR_BUDGET_CHARS", "250000"))
    financial_result = build_context(latest_ar_pages, STAGE4_FINANCIAL_SPECS, budget_chars=ar_budget, logger=logger)
    if supporting_data_dir:
        write_manifest(financial_result, supporting_data_dir, "stage4_financial", ticker, latest_year, logger=logger)
    if logger:
        logger.info(f"Financial retrieval: {financial_result.coverage_note()}")
    
    # Also get remuneration-specific sections
    remuneration_result = build_context(latest_ar_pages, STAGE4_REMUNERATION_SPECS, budget_chars=ar_budget, logger=logger)
    if supporting_data_dir:
        write_manifest(remuneration_result, supporting_data_dir, "stage4_remuneration", ticker, latest_year, logger=logger)
    if logger:
        logger.info(f"Remuneration retrieval: {remuneration_result.coverage_note()}")
    
    t1 = time.time()
    if logger:
        logger.info(f"PDF retrieval completed in {t1-t0:.1f}s")
    
    prompt_1a = _load_prompt("5yr_perf_hist_1a.txt")
    prompt_1b = _load_prompt("5yr_perf_hist_1b.txt")
    
    # Log prompt sizes
    if logger:
        logger.info(f"Prompt 1a size: {len(prompt_1a)} chars")
        logger.info(f"Prompt 1b size: {len(prompt_1b)} chars")

    result_1a = None
    # Use combined financial text for prompt 1a
    combined_1a_slice = financial_result.text
    if combined_1a_slice:
        if logger:
            logger.info(f"Found 5-year table slice: {len(combined_1a_slice)} chars")
        t_slice = time.time()
        t_slice_end = time.time()
        if logger:
            logger.info(f"Slice prep: {t_slice_end - t_slice:.1f}s, slice size: {len(combined_1a_slice)} chars")
        user_content_1a = (
            f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
            f"CURRENT_FY: {latest_year}\n\n"
            f"LATEST_AR_5YR_SLICE:\n{combined_1a_slice}"
        )
        if logger:
            logger.info(f"User content 1a size: {len(user_content_1a)} chars")
        try:
            t_llm = time.time()
            result_1a = _llm_call(
                client, PERFORMANCE_MODEL, prompt_1a, user_content_1a, logger, 
                PERFORMANCE_FALLBACK_MODEL, call_name="prompt_1a", supporting_data_dir=supporting_data_dir
            )
            t_llm_end = time.time()
            if logger:
                logger.info(f"LLM call 1a took {t_llm_end - t_llm:.1f}s")
            if supporting_data_dir:
                pass
            if result_1a.get("table_found"):
                compiled["table_found"] = True
                compiled["extraction_confidence"] = result_1a.get("extraction_confidence", "medium")
                for fy, data in result_1a.get("years", {}).items():
                    normalized_fy = _normalize_year_key(fy)
                    compiled["years"][normalized_fy] = data
                # The five-year summary normally reports dividends PAID during
                # the year, which is what a total shareholder return includes.
                # Keep that row: it is overwritten further down by the
                # dividends DECLARED for the FY, and both are needed.
                for fy, data in compiled["years"].items():
                    if not isinstance(data, dict):
                        continue
                    if data.get("dividend_paid") is None and data.get("dividend") is not None:
                        data["dividend_paid"] = data["dividend"]
                        data["dividend_paid_source_page"] = data.get("dividend_source_page")
                        data["dividend_paid_note"] = (
                            "Five-year summary dividend row, taken as dividends paid "
                            "during the year (the summary footnote says so where the "
                            "issuer distinguishes paid from declared)."
                        )
                if logger:
                    logger.info(f"Prompt 1a: table found, {len(result_1a.get('years', {}))} years extracted")
                # Derive latest financial year from the actual 5-year summary data,
                # not from the AR filename (AMP_YYYY_AR.pdf covers FY(YYYY-1)).
                if compiled["years"]:
                    latest_year = max(int(y) for y in compiled["years"].keys())
                    compiled["current_fy"] = str(latest_year)
            else:
                if logger:
                    logger.warning("Prompt 1a: 5-year table not found in latest AR")
        except Exception as e:
            print(f" Error in Prompt 1a: {e}")
            if logger:
                logger.error(f"Prompt 1a failed: {e}")
    else:
        print(" No 5-year table pages found in latest AR.")
        if logger:
            logger.warning("No 5-year table pages found in latest AR")
        result_1a = {"table_found": False}
    
    if logger:
        logger.stage_end("Stage 4a: Extract 5-Year Table from Latest AR", result_1a is not None)
    
    # ============================================================
    # Step 2: Cross-check share prices (Prompt 1b)
    # ============================================================
    if logger:
        logger.stage_start("Stage 4b: Cross-Check Share Prices (Remuneration Table)")
    
    result_1b = None
    # Use combined remuneration text for prompt 1b
    combined_1b_slice = remuneration_result.text
    if result_1a and result_1a.get("table_found") and combined_1b_slice:
        if logger:
            logger.info(f"Found remuneration-performance slice: {len(combined_1b_slice)} chars")
        user_content_1b = (
            f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
            f"CURRENT_FY: {latest_year}\n\n"
            f"LATEST_AR_REM_PERFORMANCE_SLICE:\n{combined_1b_slice}\n\n"
            f"PROMPT_1A_OUTPUT:\n{json.dumps(result_1a, indent=2)}"
        )
        try:
            result_1b = _llm_call(
                client, PERFORMANCE_MODEL, prompt_1b, user_content_1b, logger, 
                PERFORMANCE_FALLBACK_MODEL, call_name="prompt_1b", supporting_data_dir=supporting_data_dir
            )
            if supporting_data_dir:
                pass
            
            if result_1b.get("overrides"):
                for fy, override in result_1b["overrides"].items():
                    if override.get("override_applied") and fy in compiled["years"]:
                        compiled["years"][fy]["share_price"] = override["share_price_final"]
                        compiled["years"][fy]["share_price_source_page"] = override["source_page"]
                        compiled["audit_notes"].append(
                            f"FY{fy} share_price overridden from {override['share_price_from_5yr_table']} "
                            f"to {override['share_price_final']} per remuneration-performance table (page {override['source_page']})"
                        )
            if logger:
                logger.info(f"Prompt 1b: {len(result_1b.get('overrides', {}))} overrides applied")
        except Exception as e:
            print(f" Error in Prompt 1b: {e}")
            if logger:
                logger.error(f"Prompt 1b failed: {e}")
    
    if logger:
        logger.stage_end("Stage 4b: Cross-Check Share Prices", result_1b is not None)
    
    # ============================================================
    # Step 3: Fill gaps from individual ARs (Prompt 2)
    # ============================================================
    if logger:
        logger.stage_start("Stage 4c: Fill Gaps from Individual ARs")
    
    prompt_2 = _load_prompt("5yr_perf_hist_2.txt")
    missing_years = []
    for yr in range(latest_year - 4, latest_year + 1):
        yr_str = str(yr)
        if yr_str not in compiled["years"]:
            missing_years.append(yr)
        elif compiled["years"][yr_str]:
            year_data = compiled["years"][yr_str]
            missing_metrics = [m for m in ["npat", "underlying_npat", "share_price", "simple_tsr", "revenue", "net_assets", "roe", "roce"] if year_data.get(m) is None]
            # Dividend and EPS are always re-extracted from individual ARs because
            # the 5-year summary shows "dividends paid during the year" not
            # "dividends declared for the FY", and EPS may be misattributed across
            # year columns. Individual ARs disclose the correct per-FY values.
            missing_metrics.append("dividend")
            missing_metrics.append("eps")
            if missing_metrics:
                compiled["years"][yr_str]["missing_metrics"] = missing_metrics
    
    for yr_str, year_data in compiled["years"].items():
        if year_data and year_data.get("missing_metrics"):
            yr = int(yr_str)
            if yr not in missing_years:
                missing_years.append(yr)
    
    prompt_2_success = False
    if missing_years:
        print(f" [NOTICE] Missing years requiring individual AR extraction: {sorted(missing_years)}")
        if logger:
            logger.warning(f"Missing years requiring individual AR extraction: {sorted(missing_years)}")
        
        for yr in sorted(missing_years):
            yr_str = str(yr)
            if logger:
                logger.info(f"Prompt 2: Starting gap-fill for FY{yr}")
            # ASX annual reports are typically published in the year AFTER the
            # financial year ends. AMP_YYYY_AR.pdf covers FY(YYYY-1). So for FY Y,
            # try AMP_{Y+1}_AR.pdf first, then fall back to AMP_Y_AR.pdf.
            ar_file = None
            for f in ar_files_sorted:
                file_year_match = re.search(r"(\d{4})", f.stem)
                if file_year_match and int(file_year_match.group(1)) == yr + 1:
                    ar_file = f
                    break
            if not ar_file:
                for f in ar_files_sorted:
                    file_year_match = re.search(r"(\d{4})", f.stem)
                    if file_year_match and int(file_year_match.group(1)) == yr:
                        ar_file = f
                        break
            
            if not ar_file:
                if logger:
                    logger.warning(f"No AR file found for FY{yr}")
                continue
            
            # Get cached pages for this AR
            ar_pages = _ar_pages(ticker, yr, ar_file, logger)
            
            if yr_str in compiled["years"] and compiled["years"][yr_str]:
                missing_metrics = compiled["years"][yr_str].get("missing_metrics", [])
            else:
                missing_metrics = ["npat", "underlying_npat", "share_price", "simple_tsr", "revenue", "net_assets", "roe", "roce", "dividend", "eps"]
            
            if not missing_metrics:
                if logger:
                    logger.info(f"Prompt 2: FY{yr} has no missing metrics, skipping")
                continue
            
            t0 = time.time()
            # Use retrieval specs for targeted section extraction
            financial_result = build_context(ar_pages, STAGE4_FINANCIAL_SPECS, budget_chars=ar_budget, logger=logger)
            if supporting_data_dir:
                write_manifest(financial_result, supporting_data_dir, "stage4_financial", ticker, yr, logger=logger)
            if logger:
                logger.info(f"Financial retrieval FY{yr}: {financial_result.coverage_note()}")
            
            # Use combined financial text
            ar_slice = financial_result.text
            t1 = time.time()
            if logger:
                logger.info(f"Prompt 2: FY{yr} retrieval slice extracted in {t1-t0:.1f}s ({len(ar_slice)} chars)")
            
            user_content_2 = (
                f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
                f"TARGET_FY: {yr}\n"
                f"MISSING_METRICS: {', '.join(missing_metrics)}\n\n"
                f"YEAR_AR_SLICE:\n{ar_slice}"
            )
            
            try:
                result_2 = _llm_call(
                    client, PERFORMANCE_MODEL, prompt_2, user_content_2, logger, 
                    PERFORMANCE_FALLBACK_MODEL, call_name=f"prompt_2_fy{yr}", supporting_data_dir=supporting_data_dir
                )
                
                if result_2.get("years") and yr_str in result_2["years"]:
                    year_data = result_2["years"][yr_str]
                    if yr_str not in compiled["years"]:
                        compiled["years"][yr_str] = {"extracted_from_latest_ar": False}
                    for key, value in year_data.items():
                        if key != "missing_metrics" and value is not None:
                            compiled["years"][yr_str][key] = value
                    compiled["years"][yr_str]["missing_metrics"] = [
                        m for m in missing_metrics if compiled["years"][yr_str].get(m) is None
                    ]
                    prompt_2_success = True
                    if logger:
                        logger.info(f"Prompt 2: FY{yr} gap-filled successfully")
            except Exception as e:
                print(f" Error in Prompt 2 for FY{yr}: {e}")
                if logger:
                    logger.error(f"Prompt 2 failed for FY{yr}: {e}")
    else:
        if logger:
            logger.info("No missing years — Prompt 2 not needed")
    
    if logger:
        logger.stage_end("Stage 4c: Fill Gaps from Individual ARs", prompt_2_success)
    
    # ============================================================
    # Step 4: Extract CEO realised pay (Prompt 3)
    # ============================================================
    if logger:
        logger.stage_start("Stage 4d: Extract CEO Realised Pay")
    
    prompt_3 = _load_prompt("5yr_perf_hist_3.txt")
    prompt_3_success = False
    
    for ar_file in ar_files_sorted:
        file_year_match = re.search(r"(\d{4})", ar_file.stem)
        if not file_year_match:
            continue
        # ASX annual reports are typically published the year after the
        # financial year ends. AMP_YYYY_AR.pdf covers FY(YYYY-1).
        yr = int(file_year_match.group(1)) - 1
        yr_str = str(yr)
        
        if logger:
            logger.info(f"Prompt 3: Starting CEO pay extraction for FY{yr}")
        
        # Get cached pages for this AR
            ar_pages = _ar_pages(ticker, yr, ar_file, logger)

        
        t0 = time.time()
        # Use retrieval specs for targeted section extraction
        remuneration_result = build_context(ar_pages, STAGE4_REMUNERATION_SPECS, budget_chars=ar_budget, logger=logger)
        if supporting_data_dir:
            write_manifest(remuneration_result, supporting_data_dir, "stage4_remuneration", ticker, yr, logger=logger)
        if logger:
            logger.info(f"Remuneration retrieval FY{yr}: {remuneration_result.coverage_note()}")
        
        # Use combined remuneration text
        rem_slice = remuneration_result.text
        t1 = time.time()
        if logger:
            logger.info(f"Prompt 3: FY{yr} remuneration slice extracted in {t1-t0:.1f}s ({len(rem_slice)} chars)")
        
        user_content_3 = (
            f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
            f"TARGET_FY: {yr}\n\n"
            f"YEAR_AR_REM_SLICE:\n{rem_slice}"
        )
        
        try:
            result_3 = _llm_call(
                client, PERFORMANCE_MODEL, prompt_3, user_content_3, logger, 
                PERFORMANCE_FALLBACK_MODEL, call_name=f"prompt_3_fy{yr}", supporting_data_dir=supporting_data_dir
            )
            
            # Currency- and scale-anchored validation against the cited page.
            # A digit-only match would pass a $m claim against a GBP table, so
            # the validator resolves currency per row and rejects near-misses
            # with the offending currency named rather than as a bare absence.
            pages_by_number = {p["page_number"]: p.get("text") or "" for p in ar_pages}
            validator = _make_ceo_pay_validator(pages_by_number, ticker)
            claim = dict(result_3)
            claim.setdefault("target_fy", str(yr))
            # Repair a scale-slip before validation: when the model recorded an
            # implausibly low primary figure but its own component cross-check
            # came back plausible, adopt the cross-check (it is the number the
            # model derived from the disclosed components). See
            # docs/validator_traps.md and tests/test_measured_defects.py for why
            # the primary field cannot be trusted when the two disagree.
            claim = _reconcile_ceo_value(claim)
            v_errors = validator(claim)
            if v_errors:
                if logger:
                    logger.warning(
                        f"Prompt 3: FY{yr} CEO pay claim rejected by validator: "
                        + "; ".join(v_errors)
                    )
                compiled.setdefault("audit_notes", []).append(
                    f"FY{yr} CEO realised pay rejected by validator: "
                    + "; ".join(v_errors)
                )
                continue

            if claim.get("ceo_realised_rem") is not None:
                ceo_rem = claim["ceo_realised_rem"]
                # Safety check: if value > 1000000, it's likely in raw dollars
                # If value > 1000 and <= 1000000, it's likely in thousands
                # CEO pay in $millions should typically be < 1000 (i.e., < $1B)
                if isinstance(ceo_rem, (int, float)):
                    if ceo_rem > 1000000:
                        if logger:
                            logger.warning(f"CEO pay value {ceo_rem} appears to be in raw dollars, converting to $millions")
                        ceo_rem = ceo_rem / 1000000.0
                    elif ceo_rem > 1000:
                        if logger:
                            logger.warning(f"CEO pay value {ceo_rem} appears to be in thousands, converting to $millions")
                        ceo_rem = ceo_rem / 1000.0
                
                compiled["ceo_realised_pay"][yr_str] = {
                    "value_m": ceo_rem,
                    "source_method": claim.get("source_method", "calculated"),
                    "source_page": claim.get("sti_source_page", "") or claim.get("lti_source_page", ""),
                    "notes": f"CEO: {claim.get('ceo_name', 'Unknown')}"
                }
                if claim.get("reconciled_from_cross_check"):
                    compiled["ceo_realised_pay"][yr_str]["notes"] += " (reconciled from cross-check)"
                    if logger:
                        logger.warning(
                            f"Prompt 3: FY{yr} primary figure was implausibly low; "
                            f"reconciled to cross-check value ${ceo_rem:.2f}m"
                        )
                prompt_3_success = True
                if logger:
                    logger.info(f"Prompt 3: FY{yr} CEO realised pay = ${ceo_rem:.2f}m")
            
            # Also extract and store statutory pay if present
            if claim.get("ceo_statutory_rem") is not None:
                statutory_rem = claim["ceo_statutory_rem"]
                if isinstance(statutory_rem, (int, float)):
                    if statutory_rem > 1000000:
                        if logger:
                            logger.warning(f"CEO statutory pay value {statutory_rem} appears to be in raw dollars, converting to $millions")
                        statutory_rem = statutory_rem / 1000000.0
                    elif statutory_rem > 1000:
                        if logger:
                            logger.warning(f"CEO statutory pay value {statutory_rem} appears to be in thousands, converting to $millions")
                        statutory_rem = statutory_rem / 1000.0
                
                compiled.setdefault("ceo_statutory_pay", {})[yr_str] = {
                    "value_m": statutory_rem,
                    "source_method": claim.get("statutory_basis", "disclosed_statutory"),
                    "source_page": claim.get("disclosed_statutory_source_page", ""),
                    "notes": f"CEO: {claim.get('ceo_name', 'Unknown')}"
                }
                if logger:
                    logger.info(f"Prompt 3: FY{yr} CEO statutory pay = ${statutory_rem:.2f}m")
        except Exception as e:
            print(f" Error in Prompt 3 for FY{yr}: {e}")
            if logger:
                logger.error(f"Prompt 3 failed for FY{yr}: {e}")

    # ============================================================
    # Fallback: CEO realised pay from the Annual Report itself.
    #
    # Prompt 3 is a model call per year and the free-tier models drop some of
    # them; it also answers "not disclosed" when the realised-pay table is in
    # the AR but outside the retrieved slice.  The issuer's voluntary
    # "actual remuneration received" table is read directly as a backstop.
    # ============================================================
    try:
        from remuneration_framework import (detect_ceo_name, extract_ceo_realised_pay,
                                            extract_ceo_statutory_pay)
    except Exception:
        detect_ceo_name = None
        extract_ceo_realised_pay = None
        extract_ceo_statutory_pay = None

    if extract_ceo_realised_pay:
        ceo_names = []
        for row in compiled.get("ceo_realised_pay", {}).values():
            notes = (row or {}).get("notes") or ""
            match = re.search(r"CEO:\s*(.+)$", notes)
            if match:
                ceo_names.append(match.group(1).strip())
        ceo_name = ceo_names[0] if ceo_names else ""
        if not ceo_name:
            for row in compiled.get("ceo_statutory_pay", {}).values():
                notes = (row or {}).get("notes") or ""
                match = re.search(r"CEO:\s*(.+)$", notes)
                if match:
                    ceo_name = match.group(1).strip()
                    break
        if not ceo_name and detect_ceo_name:
            for ar_file in ar_files_sorted:
                ar_pages = _ar_pages(ticker, int(re.search(r"(\d{4})", ar_file.stem).group(1)) - 1,
                                      ar_file, logger)
                ceo_name = detect_ceo_name(ar_pages, ticker)
                if ceo_name:
                    if logger:
                        logger.info(f"CEO name detected from the AR: {ceo_name}")
                    break

        if ceo_name:
            for ar_file in ar_files_sorted:
                file_year_match = re.search(r"(\d{4})", ar_file.stem)
                if not file_year_match:
                    continue
                yr = int(file_year_match.group(1)) - 1
                yr_str = str(yr)
                ar_pages = _ar_pages(ticker, yr, ar_file, logger)
                found = extract_ceo_realised_pay(ar_pages, ceo_name, target_fy=yr, logger=logger)
                if found:
                    existing = compiled.get("ceo_realised_pay", {}).get(yr_str)
                    if existing:
                        previous = existing.get("value_m")
                        if previous is not None and abs(previous - found["value_m"]) > 0.005:
                            compiled.setdefault("audit_notes", []).append(
                                f"FY{yr} CEO realised pay: the Annual Report's own table "
                                f"gives ${found['value_m']:.3f}m (p.{found['source_page']}); the "
                                f"model reported ${previous:.3f}m. The AR figure is used."
                            )
                    compiled.setdefault("ceo_realised_pay", {})[yr_str] = found
                    if logger:
                        logger.info(
                            f"CEO realised pay from AR: FY{yr} = ${found['value_m']:.3f}m "
                            f"(page {found['source_page']})"
                        )

                if extract_ceo_statutory_pay:
                    statutory = extract_ceo_statutory_pay(ar_pages, ceo_name, target_fy=yr,
                                                         logger=logger)
                    if statutory:
                        compiled.setdefault("ceo_statutory_pay", {})[yr_str] = statutory

    if logger:
        logger.stage_end("Stage 4d: Extract CEO Realised Pay", prompt_3_success)
    
    # ============================================================
    # Step 5: Compile and validate (Prompt 4 placeholder)
    # ============================================================
    if logger:
        logger.stage_start("Stage 4e: Compile and Validate Performance Table")
    
    prompt_4 = _load_prompt("5yr_perf_hist_4.txt")
    
    user_content_4 = (
        f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
        f"CURRENT_FY: {latest_year}\n\n"
        f"COMPILED_DATA:\n{json.dumps(compiled, indent=2)}"
    )
    
    try:
        result_4 = _llm_call(
            client, PERFORMANCE_MODEL, prompt_4, user_content_4, logger, 
            PERFORMANCE_FALLBACK_MODEL, call_name="prompt_4", supporting_data_dir=supporting_data_dir
        )
        if logger:
            logger.info("Prompt 4: Validation complete")
    except Exception as e:
        print(f" Error in Prompt 4: {e}")
        if logger:
            logger.error(f"Prompt 4 failed: {e}")
    
    t0 = time.time()
    # Backfill share_price + simple_tsr from Yahoo's public chart endpoint.
    # Many ARs (Macquarie is the clear case) publish no multi-year share-price
    # history, so these two metrics are genuinely not extractable from the AR.
    # Historical FY-end closes are deterministic market facts, cached per ticker.
    # The closes are kept so the TSR recomputation below can use them for any
    # year whose price came from the backfill rather than from the AR.
    market_closes: dict = {}
    backfilled_years: set = set()
    try:
        from share_prices import (compute_simple_tsr, detect_fy_end_month_day,
                                  get_fy_closes, yahoo_symbol)

        cover_text = ""
        for p in latest_ar_pages[:3]:
            cover_text += (p.get("text") or "") + "\n"
        fy_end = detect_fy_end_month_day(cover_text)
        if fy_end is not None:
            month, day = fy_end
            years_desc = sorted(int(y) for y in compiled.get("years", {}).keys())
            closes = get_fy_closes(ticker, month, day, years_desc)
            market_closes = dict(closes)
            tsr_by_year = compute_simple_tsr(closes)
            for y in years_desc:
                y_str = str(y)
                yd = compiled["years"].get(y_str)
                if not isinstance(yd, dict):
                    continue
                yd.setdefault("missing_metrics", [])
                if yd.get("share_price") in (None, "") and closes.get(y) is not None:
                    yd["share_price"] = round(closes[y], 2)
                    backfilled_years.add(y)
                    if "share_price" in yd["missing_metrics"]:
                        yd["missing_metrics"].remove("share_price")
                if yd.get("simple_tsr") in (None, "") and tsr_by_year.get(y) is not None:
                    yd["simple_tsr"] = tsr_by_year[y]
                    if "simple_tsr" in yd["missing_metrics"]:
                        yd["missing_metrics"].remove("simple_tsr")
            if logger:
                logger.info(
                    f"Share-price backfill: {len(years_desc)} years from "
                    f"{yahoo_symbol(ticker)} (FY-end {month:02d}/{day:02d})"
                )
    except Exception as e:
        if logger:
            logger.warning(f"Share-price backfill could not run: {e}")

    # Normalize EPS units once every EPS source has been consulted - see
    # _normalize_eps_units below, called after the EPS post-processing.

    # ============================================================
    # Post-process: extract dividends declared per FY from individual ARs
    # The 5-year summary table shows "dividends PAID during the year" which is
    # a different metric from "dividends DECLARED for the FY". Individual ARs'
    # Appendix 4E (typically page 2) discloses interim + final dividends
    # declared for each FY. Override any 5-year summary dividend values with
    # the correct per-FY declared totals.
    # ============================================================
    import re as _re

    def _parse_dividend_amount(after_dividend: str):
        """Extract the first dividend amount (cents) from text after 'dividend'.
        Skips percentages, footnote references, times, dates, and non-dividend numbers."""
        tokens = after_dividend.split()
        for token in tokens:
            # Skip percentages
            if '%' in token:
                continue
            clean = token.strip('.,;:()')
            # Skip footnote references (contain commas)
            if ',' in clean:
                continue
            # Skip time expressions (contain colon)
            if ':' in clean:
                continue
            try:
                val = float(clean)
                # Dividend amounts are typically 0-100 cents; skip dates/years
                if val > 0 and val <= 100:
                    return val
            except ValueError:
                continue
        return None

    for fy_str, fy_data in compiled["years"].items():
        fy = int(fy_str)
        for ar_file in ar_files_sorted:
            file_year_match = _re.search(r"(\d{4})", ar_file.stem)
            if not file_year_match:
                continue
            pub_year = int(file_year_match.group(1))
            if pub_year - 1 != fy:
                continue
            ar_pages = _ar_pages(ticker, fy, ar_file, logger)
            total_dividend = 0.0
            dividend_found = False
            dividend_source_page = None
            for p in ar_pages:
                text = p.get("text") or ""
                page_num = p.get("page_number", 0)
                page_total = 0.0
                page_found = False
                for line in text.split('\n'):
                    line_lower = line.lower()
                    if 'dividend' not in line_lower:
                        continue
                    # Skip non-dividend-amount lines (record dates, DRP, etc.)
                    if any(kw in line_lower for kw in [
                        'record date', 'payable date', 'drp', 'dividend reinvestment',
                        'participation', 'entitlement', 'conduit foreign',
                        'franking rate', 'dividend franking',
                    ]):
                        continue
                    # Check for "final dividend" or "interim dividend" on this line
                    is_final = 'final' in line_lower and 'dividend' in line_lower
                    is_interim = 'interim' in line_lower and 'dividend' in line_lower
                    if not (is_final or is_interim):
                        continue
                    idx = line_lower.find('dividend')
                    amount = _parse_dividend_amount(line[idx + len('dividend'):])
                    if amount is not None:
                        page_total += amount
                        page_found = True
                if page_found:
                    total_dividend = round(page_total, 1)
                    dividend_found = True
                    dividend_source_page = page_num
                    break
            if not dividend_found:
                for p in ar_pages:
                    text = (p.get("text") or "").lower()
                    if ("no dividend" in text or "not to pay a dividend" in text
                            or ("not declare" in text and "dividend" in text)):
                        total_dividend = 0.0
                        dividend_found = True
                        dividend_source_page = p.get("page_number", 0)
                        break
            if dividend_found:
                fy_data["dividend"] = total_dividend
                fy_data["dividend_source_page"] = dividend_source_page
                if "dividend" in fy_data.get("missing_metrics", []):
                    fy_data["missing_metrics"].remove("dividend")
                if logger:
                    logger.info(
                        f"Dividend post-process: FY{fy} = {total_dividend}c "
                        f"(page {dividend_source_page})"
                    )
            break

    # ============================================================
    # Post-process: EPS from the individual ARs.
    #
    # A model's EPS can be attributed to the wrong year: an AR prints the
    # current year and the comparative side by side, and reading the columns
    # loosely reports the comparative under the current year.  AMP FY2024 was
    # reported as 9.3c, which is FY2023's number - FY2024's reported EPS is
    # 5.7c and its underlying EPS is 9.0c.  The deterministic extractor keys
    # off the column header, so it wins where it finds a figure; the AR that
    # reports the year is preferred over the next AR, which shows it only as
    # a comparative.
    # ============================================================
    try:
        from eps_extraction import extract_eps
    except Exception:
        extract_eps = None

    if extract_eps is not None:
        for fy_str, fy_data in compiled["years"].items():
            fy = int(fy_str)
            candidates = []
            for ar_file in ar_files_sorted:
                file_year_match = _re.search(r"(\d{4})", ar_file.stem)
                if not file_year_match:
                    continue
                pub_year = int(file_year_match.group(1))
                if pub_year - 1 == fy:
                    candidates.append((0, ar_file))
                elif pub_year - 2 == fy:
                    candidates.append((1, ar_file))
            candidates.sort(key=lambda item: item[0])

            best = {}
            for _, ar_file in candidates:
                ar_pages = _ar_pages(ticker, fy, ar_file, logger)
                found = extract_eps(ar_pages, fy)
                if not found:
                    continue
                for key in ("eps", "underlying_eps"):
                    if found.get(key) is None:
                        continue
                    if key not in best:
                        best[key] = found
                if "eps" in best and "underlying_eps" in best:
                    break

            for key in ("eps", "underlying_eps"):
                found = best.get(key)
                if not found or found.get(key) is None:
                    continue
                previous = fy_data.get(key)
                fy_data[key] = found[key]
                fy_data[f"{key}_source_page"] = found.get(f"{key}_source_page")
                if key in fy_data.get("missing_metrics", []):
                    fy_data["missing_metrics"].remove(key)
                if logger:
                    note = ""
                    if previous is not None and previous != found[key]:
                        note = f" (replaced model's {previous})"
                    logger.info(
                        f"EPS post-process: FY{fy} {key} = {found[key]}c "
                        f"(page {found.get(f'{key}_source_page')}){note}"
                    )

    _normalize_eps_units(compiled, logger)

    # ============================================================
    # Recompute simple TSR as a total shareholder return.
    #
    # A TSR includes the dividends received over the year, not just the
    # share price move.  Issuers define it that way themselves - AMP's 2025
    # annual report (p.173) defines TSR as "the changes in market value of
    # AMP shares, plus the value of any dividends paid and capital returns".
    # The share prices used are the ones shown in the table, so the figure is
    # reproducible from the report itself.
    # ============================================================
    try:
        tsr_years = sorted(int(y) for y in compiled.get("years", {}).keys())
        prices = {}
        for fy in tsr_years:
            raw = compiled["years"][str(fy)].get("share_price")
            try:
                prices[fy] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                prices[fy] = None

        dividends_paid = {}
        for fy in tsr_years:
            # Only the dividends PAID during the year belong in a total
            # shareholder return.  The declared dividend is a different figure
            # and must not be substituted for it: the declared dividend for
            # FY Y is mostly paid in FY Y+1.
            raw = compiled["years"][str(fy)].get("dividend_paid")
            try:
                dividends_paid[fy] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                dividends_paid[fy] = None

        for idx in range(1, len(tsr_years)):
            fy = tsr_years[idx]
            prev_fy = tsr_years[idx - 1]
            # Use the prices shown in the table so the TSR is reproducible
            # from the report.  If either endpoint's price came from the
            # market-data backfill, use the market closes for both ends so a
            # single year's return is not computed from mixed sources.
            if fy in backfilled_years or prev_fy in backfilled_years:
                start = market_closes.get(prev_fy)
                end = market_closes.get(fy)
            else:
                start = prices.get(prev_fy)
                end = prices.get(fy)
            if not start or not end:
                continue
            div_cps = dividends_paid.get(fy) or 0.0
            div = div_cps / 100.0  # cents per share -> dollars per share
            tsr = round(((end - start + div) / start) * 100.0, 2)
            yd = compiled["years"][str(fy)]
            yd["simple_tsr"] = tsr
            yd["simple_tsr_source_page"] = yd.get("share_price_source_page")
            yd["simple_tsr_basis_note"] = (
                f"({end:g} - {start:g} + {div_cps:g}c dividends paid) / {start:g}"
            )
            if "simple_tsr" in yd.get("missing_metrics", []):
                yd["missing_metrics"].remove("simple_tsr")
            if logger:
                logger.info(f"Simple TSR: FY{fy} = {tsr}% (incl. {div_cps}c dividends paid)")
    except Exception as e:
        if logger:
            logger.warning(f"Simple TSR recomputation failed: {e}")

    # Save compiled JSON
    compiled_path = workspace_dir / "extracted_json" / "performance_history_5yr.json"
    compiled_path.parent.mkdir(parents=True, exist_ok=True)
    with open(compiled_path, "w", encoding="utf-8") as f:
        json.dump(compiled, f, indent=2, ensure_ascii=False)
    
    if supporting_data_dir:
        write_supporting_json(supporting_data_dir, "performance_history_5yr_compiled.json", compiled)
    
    # Generate Markdown summary for human review
    md_lines = [
        f"# Performance History Table — {workspace_dir.name}",
        f"",
        f"**Extracted:** {compiled['extracted_at']}",
        f"**Table found in latest AR:** {'Yes' if compiled['table_found'] else 'No'}",
        f"**Confidence:** {compiled['extraction_confidence'] or 'N/A'}",
        f"",
        f"| Metric | {' | '.join(compiled['years'].keys())} |",
        f"|--------|{'|'.join(['------'] * len(compiled['years']))}|"
    ]
    
    metrics = ["npat", "underlying_npat", "share_price", "dividend", "dividend_paid",
               "simple_tsr", "eps", "underlying_eps", "revenue", "net_assets", "roe", "roce"]
    for metric in metrics:
        row = [metric.replace("_", " ").title()]
        for fy in compiled["years"]:
            val = compiled["years"][fy].get(metric)
            if val is None:
                row.append("—")
            else:
                try:
                    num_val = float(val)
                except (ValueError, TypeError):
                    num_val = None

                if num_val is not None:
                    if metric in ["npat", "underlying_npat", "revenue", "net_assets"]:
                        row.append(f"${num_val:,.0f}m")
                    elif metric in ["share_price"]:
                        row.append(f"${num_val:.2f}")
                    elif metric in ["dividend", "dividend_paid", "eps", "underlying_eps"]:
                        row.append(f"{num_val:.1f}c")
                    elif metric in ["simple_tsr", "roe", "roce"]:
                        row.append(f"{num_val:.1f}%")
                    else:
                        row.append(str(val))
                else:
                    row.append(str(val))
        md_lines.append(f"| {' | '.join(row)} |")
    
    if compiled["ceo_realised_pay"]:
        md_lines.append("")
        md_lines.append("## CEO Realised Pay")
        md_lines.append("")
        md_lines.append("| FY | CEO Realised Pay ($m) | Source Method | Source Page |")
        md_lines.append("|----|----------------------|---------------|-------------|")
        for fy in sorted(compiled["ceo_realised_pay"].keys()):
            pay_data = compiled["ceo_realised_pay"][fy]
            md_lines.append(f"| {fy} | ${pay_data['value_m']:.2f}m | {pay_data['source_method']} | {pay_data['source_page']} |")
    
    if compiled["audit_notes"]:
        md_lines.append("")
        md_lines.append("## Audit Notes")
        for note in compiled["audit_notes"]:
            md_lines.append(f"- {note}")
    
    md_path = workspace_dir / "reports" / "performance_table.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    t1 = time.time()
    print(f" Performance table saved -> {md_path}")
    if logger:
        logger.info(f"Performance table Markdown saved: {md_path.name} (in {t1-t0:.1f}s)")
    
    # Determine success based on extracted data quality
    core_metrics = ["npat", "underlying_npat", "share_price", "dividend", "eps"]
    years_with_core = 0
    for fy, data in compiled["years"].items():
        if any(data.get(m) is not None for m in core_metrics):
            years_with_core += 1
    
    total_years = len(compiled["years"])
    has_ceo_pay = bool(compiled["ceo_realised_pay"])
    
    # Success requires at least some core financial data extracted
    success = years_with_core > 0 or has_ceo_pay
    
    extraction_status = "complete" if total_years >= 5 and years_with_core >= 4 else                        "partial" if success else "failed"
    compiled["extraction_status"] = extraction_status
    compiled["years_with_core_data"] = years_with_core
    compiled["total_years_extracted"] = total_years
    
    # Update compiled JSON with status
    compiled_path = workspace_dir / "extracted_json" / "performance_history_5yr.json"
    compiled_path.parent.mkdir(parents=True, exist_ok=True)
    with open(compiled_path, "w", encoding="utf-8") as f:
        json.dump(compiled, f, indent=2, ensure_ascii=False)
    
    if supporting_data_dir:
        write_supporting_json(supporting_data_dir, "performance_history_5yr_compiled.json", compiled)
    
    if logger:
        logger.stage_end("Stage 4e: Compile and Validate Performance Table", success, 
            f"status={extraction_status} years={total_years} core_years={years_with_core} ceo_pay={has_ceo_pay}")
    
    return success


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        workspace = Path(sys.argv[1])
    else:
        workspace = BASE_DIR / "data" / "companies" / "SUN_2026"
    
    success = run_pipeline(workspace)
    if success:
        print("\nDone: Performance table extracted.")
    else:
        print("\nDone: No performance data extracted.")
