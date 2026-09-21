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
from ar_text_cache import get_ar_pages

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

PERFORMANCE_MODEL = os.getenv("PERFORMANCE_MODEL", "openai/gpt-4.1-mini")
PERFORMANCE_FALLBACK_MODEL = os.getenv("PERFORMANCE_FALLBACK_MODEL", "google/gemini-3.1-flash-lite")
BASE_DIR = Path(__file__).resolve().parent.parent

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
                        "simple_tsr": {"type": ["number", "string", "null"]},
                        "simple_tsr_source_page": {"type": ["integer", "string", "null"]},
                        "tsr_mismatch": {"type": "boolean"},
                        "eps": {"type": ["number", "string", "null"]},
                        "eps_source_page": {"type": ["integer", "string", "null"]},
                        "revenue": {"type": ["number", "string", "null"]},
                        "net_assets": {"type": ["number", "string", "null"]},
                        "roe": {"type": ["number", "string", "null"]},
                        "roce": {"type": ["number", "string", "null"]},
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
                        "simple_tsr": {"type": ["number", "string", "null"]},
                        "simple_tsr_source_page": {"type": ["integer", "string", "null"]},
                        "tsr_mismatch": {"type": "boolean"},
                        "eps": {"type": ["number", "string", "null"]},
                        "eps_source_page": {"type": ["integer", "string", "null"]},
                        "revenue": {"type": ["number", "string", "null"]},
                        "net_assets": {"type": ["number", "string", "null"]},
                        "roe": {"type": ["number", "string", "null"]},
                        "roce": {"type": ["number", "string", "null"]},
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
        "source_method": {"type": "string", "enum": ["disclosed", "calculated"]},
        "cash_non_cash_salary": {"type": ["number", "string", "null"]},
        "cash_source_page": {"type": ["integer", "string", "null"]},
        "sti_max_opportunity": {"type": ["number", "string", "null"]},
        "sti_pct_of_max": {"type": ["number", "string", "null"]},
        "sti_value": {"type": ["number", "string", "null"]},
        "sti_source_page": {"type": ["integer", "string", "null"]},
        "lti_grant_years_tested": {"type": "array", "items": {"type": "string"}},
        "lti_vesting_pct": {"type": ["number", "string", "null"]},
        "lti_units": {"type": ["number", "string", "null"]},
        "lti_price_used": {"type": ["number", "string", "null"]},
        "lti_price_basis": {"type": "string", "enum": ["vesting_date", "fy_end"]},
        "lti_fx_rate_applied": {"type": ["number", "string", "null"]},
        "lti_value": {"type": ["number", "string", "null"]},
        "lti_source_page": {"type": ["integer", "string", "null"]},
        "ceo_realised_rem": {"type": ["number", "string", "null"]}
    },
    "required": ["ceo_name", "target_fy", "source_method", "ceo_realised_rem"]
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

# Token bucket rate limiter: 20 RPM = 3s between calls
class TokenBucket:
    def __init__(self, rate_per_minute: int = 18):
        self.rate = rate_per_minute / 60.0  # requests per second
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
    max_retries: int = 3,
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

    # Rate limit: wait for token bucket
    wait = _token_bucket.take()
    if wait > 0:
        metadata["rate_limit_wait_seconds"] = wait
        if logger:
            logger.info(f"[{call_name}] Rate limiting: waiting {wait:.1f}s")
        time.sleep(wait)

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
            
        except Exception as err1:
            status_code = _log_error(err1, attempt, model, is_fallback=False)
            primary_error = err1
            error_str = str(err1)
            is_rate_limit = "429" in error_str or "rate limit" in error_str.lower()
            
            if is_rate_limit and attempt < max_retries:
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                if logger:
                    logger.warning(f"[{call_name}] Rate limit hit (attempt {attempt + 1}/{max_retries + 1}), waiting {wait_time}s: {err1}")
                print(f"  [LLM] Rate limit hit, waiting {wait_time}s before retry...")
                time.sleep(wait_time)
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
    latest_ar_pages = get_ar_pages(ticker, latest_year, latest_ar_path, logger)
    
    print(f" Latest AR: {latest_ar_path.name} (FY{latest_year})")
    if logger:
        logger.info(f"Latest AR: {latest_ar_path.name} (FY{latest_year})")
    
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY
    )
    
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
        "audit_notes": []
    }
    
# ============================================================
    # Step 1: Extract 5-year table from latest AR (Prompt 1a)
    # ============================================================
    if logger:
        logger.stage_start("Stage 4a: Extract 5-Year Table from Latest AR")
    
    t0 = time.time()
    table_keywords = [
        "performance summary", "five year financial", "5 year financial", 
        "financial summary", "key financial data", "financial highlights", 
        "financial performance", "financial history", "5-year summary",
        "five year summary", "financial review", "statutory performance"
    ]
    rem_keywords = [
        "remuneration", "statutory performance", "remuneration-performance",
        "remuneration report", "key management personnel", "executive remuneration",
        "realised pay", "realized pay", "short-term incentive", "long-term incentive",
        "STI outcomes", "LTI outcomes", "equity awards"
    ]
    table_pages = _select_section_pages(latest_ar_pages, table_keywords, logger=logger, max_pages=20, radius=1)
    rem_pages = _select_section_pages(latest_ar_pages, rem_keywords, logger=logger, max_pages=30, radius=1)
    t1 = time.time()
    if logger:
        logger.info(f"PDF scanning completed in {t1-t0:.1f}s")

    prompt_1a = _load_prompt("5yr_perf_hist_1a.txt")
    prompt_1b = _load_prompt("5yr_perf_hist_1b.txt")
    
    # Log prompt sizes
    if logger:
        logger.info(f"Prompt 1a size: {len(prompt_1a)} chars")
        logger.info(f"Prompt 1b size: {len(prompt_1b)} chars")

    result_1a = None
    if table_pages:
        if logger:
            logger.info(f"Found 5-year table on pages: {table_pages}")
        t_slice = time.time()
        table_slice = _slice_cached_text(latest_ar_pages, table_pages)
        t_slice_end = time.time()
        if logger:
            logger.info(f"PDF slicing: {t_slice_end - t_slice:.1f}s, slice size: {len(table_slice)} chars")
        user_content_1a = (
            f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
            f"CURRENT_FY: {latest_year}\n\n"
            f"LATEST_AR_5YR_SLICE:\n{table_slice}"
        )
        if logger:
            logger.info(f"User content 1a size: {len(user_content_1a)} chars")
        try:
            t_llm = time.time()
            result_1a = _call_llm(
                client, PERFORMANCE_MODEL, prompt_1a, user_content_1a, logger, 
                PERFORMANCE_FALLBACK_MODEL, call_name="prompt_1a", supporting_data_dir=supporting_data_dir
            )
            t_llm_end = time.time()
            if logger:
                logger.info(f"LLM call 1a took {t_llm_end - t_llm:.1f}s")
            if supporting_data_dir:
                # Raw response is saved in metadata by _call_llm
                pass
            if result_1a.get("table_found"):
                compiled["table_found"] = True
                compiled["extraction_confidence"] = result_1a.get("extraction_confidence", "medium")
                for fy, data in result_1a.get("years", {}).items():
                    normalized_fy = _normalize_year_key(fy)
                    compiled["years"][normalized_fy] = data
                if logger:
                    logger.info(f"Prompt 1a: table found, {len(result_1a.get('years', {}))} years extracted")
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
    if result_1a and result_1a.get("table_found") and rem_pages:
        if logger:
            logger.info(f"Found remuneration-performance pages: {rem_pages}")
        rem_slice = _slice_cached_text(latest_ar_pages, rem_pages)
        user_content_1b = (
            f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
            f"CURRENT_FY: {latest_year}\n\n"
            f"LATEST_AR_REM_PERFORMANCE_SLICE:\n{rem_slice}\n\n"
            f"PROMPT_1A_OUTPUT:\n{json.dumps(result_1a, indent=2)}"
        )
        try:
            result_1b = _call_llm(
                client, PERFORMANCE_MODEL, prompt_1b, user_content_1b, logger, 
                PERFORMANCE_FALLBACK_MODEL, call_name="prompt_1b", supporting_data_dir=supporting_data_dir
            )
            if supporting_data_dir:
                # Raw response is saved in metadata by _call_llm
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
            missing_metrics = [m for m in ["npat", "underlying_npat", "share_price", "dividend", "simple_tsr", "eps", "revenue", "net_assets", "roe", "roce"] if year_data.get(m) is None]
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
            ar_file = None
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
            ar_pages = get_ar_pages(ticker, yr, ar_file, logger)
            
            if yr_str in compiled["years"] and compiled["years"][yr_str]:
                missing_metrics = compiled["years"][yr_str].get("missing_metrics", [])
            else:
                missing_metrics = ["npat", "underlying_npat", "share_price", "dividend", "simple_tsr", "eps", "revenue", "net_assets", "roe", "roce"]
            
            if not missing_metrics:
                if logger:
                    logger.info(f"Prompt 2: FY{yr} has no missing metrics, skipping")
                continue
            
            t0 = time.time()
            gap_keywords = [
                "financial review", "notes to the accounts", "statutory performance", 
                "key financial data", "five year", "5 year", "financial statements",
                "income statement", "statement of financial performance", 
                "performance summary", "earnings per share", "total shareholder return",
                "NPAT", "revenue", "return on equity", "ROCE", "share price", "dividend"
            ]
            relevant_pages = _select_section_pages(ar_pages, gap_keywords, logger=logger, max_pages=50, radius=1)
            if not relevant_pages:
                if logger:
                    logger.warning(f"Prompt 2: FY{yr} - no keyword matches found in annual report")
                continue
            
            ar_slice = _slice_cached_text(ar_pages, relevant_pages, logger=logger)
            t1 = time.time()
            if logger:
                logger.info(f"Prompt 2: FY{yr} PDF slice extracted in {t1-t0:.1f}s ({len(ar_slice)} chars)")
            
            user_content_2 = (
                f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
                f"TARGET_FY: {yr}\n"
                f"MISSING_METRICS: {', '.join(missing_metrics)}\n\n"
                f"YEAR_AR_SLICE:\n{ar_slice}"
            )
            
            try:
                result_2 = _call_llm(
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
        yr = int(file_year_match.group(1))
        yr_str = str(yr)
        
        if logger:
            logger.info(f"Prompt 3: Starting CEO pay extraction for FY{yr}")
        
        # Get cached pages for this AR
        ar_pages = get_ar_pages(ticker, yr, ar_file, logger)
        
        ceo_keywords = [
            "remuneration report", "remuneration", "key management personnel", 
            "KMP", "executive remuneration", "STI outcomes", "LTI outcomes", 
            "equity awards", "realised pay", "realized pay", "short-term incentive",
            "long-term incentive", "realised remuneration", "realized remuneration"
        ]
        rem_pages = _select_section_pages(ar_pages, ceo_keywords, logger=logger, max_pages=40, radius=1)
        if not rem_pages:
            if logger:
                logger.warning(f"No remuneration pages found in {ar_file.name}")
            continue
        
        t0 = time.time()
        rem_slice = _slice_cached_text(ar_pages, rem_pages, logger=logger)
        t1 = time.time()
        if logger:
            logger.info(f"Prompt 3: FY{yr} remuneration slice extracted in {t1-t0:.1f}s ({len(rem_slice)} chars)")
        
        user_content_3 = (
            f"COMPANY: {workspace_dir.name.split('_')[0]}\n"
            f"TARGET_FY: {yr}\n\n"
            f"YEAR_AR_REM_SLICE:\n{rem_slice}"
        )
        
        try:
            result_3 = _call_llm(
                client, PERFORMANCE_MODEL, prompt_3, user_content_3, logger, 
                PERFORMANCE_FALLBACK_MODEL, call_name=f"prompt_3_fy{yr}", supporting_data_dir=supporting_data_dir
            )
            
            if result_3.get("ceo_realised_rem") is not None:
                ceo_rem = result_3["ceo_realised_rem"]
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
                    "source_method": result_3.get("source_method", "calculated"),
                    "source_page": result_3.get("sti_source_page", "") or result_3.get("lti_source_page", ""),
                    "notes": f"CEO: {result_3.get('ceo_name', 'Unknown')}"
                }
                prompt_3_success = True
                if logger:
                    logger.info(f"Prompt 3: FY{yr} CEO realised pay = ${ceo_rem:.2f}m")
        except Exception as e:
            print(f" Error in Prompt 3 for FY{yr}: {e}")
            if logger:
                logger.error(f"Prompt 3 failed for FY{yr}: {e}")
    
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
        result_4 = _call_llm(
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
    
    metrics = ["npat", "underlying_npat", "share_price", "dividend", "simple_tsr", "eps", "revenue", "net_assets", "roe", "roce"]
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
                    elif metric in ["dividend", "eps"]:
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
