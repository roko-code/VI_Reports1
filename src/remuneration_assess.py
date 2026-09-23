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

import pdfplumber
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text
from ar_text_cache import get_ar_pages

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

REM_ASSESS_PRIMARY_MODEL = os.getenv("REM_ASSESS_PRIMARY_MODEL", "openai/gpt-4.1-mini")
REM_ASSESS_FALLBACK_MODEL = os.getenv("REM_ASSESS_FALLBACK_MODEL", "google/gemini-3.1-flash-lite")
BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
REFERENCE_DIR = BASE_DIR / "data" / "_reference"

# Comparator CSV path
COMPARATOR_CSV_PATH = REFERENCE_DIR / "ceo_kmp_comparator.csv"


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


def _load_prompt(filename: str) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _load_comparator_csv() -> List[Dict[str, Any]]:
    """Load comparator CSV and filter to CEO rows with latest year per company."""
    if not COMPARATOR_CSV_PATH.exists():
        return []
    
    rows = []
    with open(COMPARATOR_CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Csuite Job Title", "").lower() == "chief executive officer":
                rows.append(row)
    
    # Keep latest year per company
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


def _extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber."""
    text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Page {idx} ---\n" + page_text
    return text


def _extract_nom_text(nom_path: Path) -> str:
    """Extract text from NoM PDF."""
    return _extract_text_from_pdf(nom_path)


def _extract_ar_text(ar_path: Path) -> str:
    """Extract text from Annual Report PDF."""
    return _extract_text_from_pdf(ar_path)


def _find_ar_file(workspace_dir: Path, year: int) -> Optional[Path]:
    """Find AR PDF for a specific year in workspace."""
    annual_reports_dir = workspace_dir / "annual_reports"
    for f in annual_reports_dir.glob("*.pdf"):
        match = re.search(r"(\d{4})", f.stem)
        if match and int(match.group(1)) == year:
            return f
    return None


def _find_nom_file(workspace_dir: Path) -> Optional[Path]:
    """Find NoM PDF in workspace."""
    notices_dir = workspace_dir / "notices"
    pdfs = list(notices_dir.glob("*.pdf"))
    if pdfs:
        return pdfs[0]
    return None


def _parse_nom_resolutions(nom_text: str) -> List[Dict[str, Any]]:
    """Extract remuneration-related resolutions from NoM text."""
    # This is a simplified parser - in practice would need more sophisticated parsing
    resolutions = []
    
    # Look for resolution patterns in NoM text
    # This is a placeholder - real implementation would parse NoM structure
    return resolutions


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
    use_strict_json = schema and _check_response_format_support(model)
    
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
            
            if logger:
                logger.info(f"[{call_name}] Raw response (first 1000 chars): {content[:1000]}")
            
            # Parse JSON
            try:
                if schema and not use_strict_json:
                    parsed = _validate_and_parse(content, schema)
                else:
                    parsed = _parse_json_from_response(content)
            except Exception as parse_err:
                if logger:
                    logger.error(f"[{call_name}] JSON parse failed: {parse_err}. Raw content: {content[:1000]}")
                raise
                
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
    return _parse_json_from_response(content)


def run_remuneration_assessment(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None
) -> bool:
    """
    Run remuneration assessment for a company/year.
    
    Returns True if assessment completed successfully.
    """
    workspace_dir = Path(workspace_dir)
    supporting_data_dir = Path(supporting_data_dir)
    supporting_data_dir.mkdir(parents=True, exist_ok=True)
    
    # Extract ticker and year from workspace directory name (e.g., "RIO_2026")
    workspace_name = workspace_dir.name
    match = re.match(r"([A-Z]+)_(\d{4})", workspace_name)
    if match:
        ticker = match.group(1)
        year = int(match.group(2))
    else:
        raise ValueError(f"Workspace directory name must be in format TICKER_YEAR, got: {workspace_name}")
    
    ticker = ticker.upper()
    year = int(year)
    
    supporting_data_dir.mkdir(parents=True, exist_ok=True)
    
    if logger:
        logger.stage_start(f"Stage 5: Remuneration Assessment for {ticker} FY{year}")
    
    # Find source documents
    nom_path = _find_nom_file(workspace_dir)
    ar_path = _find_ar_file(workspace_dir, year)
    prior_ar_path = _find_ar_file(workspace_dir, year - 1)
    
    # Load documents
    nom_text = _extract_nom_text(nom_path) if nom_path else ""
    ar_text = _extract_ar_text(ar_path) if ar_path else ""
    prior_ar_text = _extract_ar_text(prior_ar_path) if prior_ar_path else ""
    
    # Load comparator CSV
    comparator_data = _load_comparator_csv()
    
    # Document inventory
    docs_supplied = {
        "current_ar": bool(ar_text),
        "nom": bool(nom_text),
        "prior_ar": bool(prior_ar_text),
        "prior_asa_report": False,  # Not implemented yet
        "peer_reports": False,  # Not implemented yet
        "comparator_csv": len(_load_comparator_csv()) > 0,
    }
    
    if logger:
        logger.info(f"Documents supplied: {docs_supplied}")
    
    # Load prompt
    prompt_text = _load_prompt("rem_assess.txt")
    
    # Build user content with all source documents
    user_content = f"""
COMPANY: {ticker}
FINANCIAL_YEAR: {year}

=== DOCUMENT 1: CURRENT ANNUAL REPORT ===
{ar_text[:50000]}...

=== DOCUMENT 2: NOTICE OF MEETING ===
{nom_text[:30000]}...

=== DOCUMENT 3: PRIOR YEAR ANNUAL REPORT (if available) ===
{prior_ar_text[:50000] if prior_ar_text else "NOT SUPPLIED"}...

=== DOCUMENT 7: COMPARATOR CSV (CEO/KMP data) ===
Available companies: {len(_load_comparator_csv())} CEO rows
"""
    
    # Call LLM for assessment
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        http_client=None  # Avoid proxies issue
    )
    
    # This is a simplified single-call approach
    # In production, would break into multiple calls per section
    result = _call_llm(
        client=client,
        model="openai/gpt-4.1-mini",
        prompt_text=prompt_text,
        user_content=user_content,
        logger=logger,
        fallback_model="google/gemini-3.1-flash-lite",
        call_name="rem_assess",
        supporting_data_dir=supporting_data_dir
    )
    
    # Save result
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Remuneration_Assessment.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    
    if logger:
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
        result = run_remuneration_assessment(ticker, year, workspace, supporting)
        print(f"Result: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/remuneration_assess.py <ticker> <year> [workspace_dir]")
        sys.exit(1)


# ============================================================
# Helper functions for _call_llm
# ============================================================

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


def _extract_error_status(err: Exception) -> int | None:
    """Extract HTTP status code from an OpenAI/OpenRouter error."""
    if hasattr(err, "status_code") and err.status_code:
        return int(err.status_code)
    if hasattr(err, "response") and err.response is not None:
        if hasattr(err.response, "status_code") and err.response.status_code:
            return int(err.response.status_code)
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


def _get_schema_for_call(call_name: str) -> dict | None:
    """Get schema for call_name."""
    # No specific schemas for remuneration assessment yet
    return None


def _check_response_format_support(model: str) -> bool:
    """Check if model supports response_format via OpenRouter."""
    if model in _RESPONSE_FORMAT_SUPPORT:
        return _RESPONSE_FORMAT_SUPPORT[model]
    
    # For now, assume gpt-4.1-mini supports it, others don't
    supports = model.startswith("openai/gpt-4.1") or model.startswith("openai/gpt-4o")
    _RESPONSE_FORMAT_SUPPORT[model] = supports
    return supports


def _schema_supports_strict_mode(call_name: str) -> bool:
    """Check if the schema for this call_name supports strict mode."""
    return False  # No schemas defined for remuneration assessment yet


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


_RESPONSE_FORMAT_SUPPORT = {}


def _check_response_format_support(model: str) -> bool:
    """Check if model supports response_format via OpenRouter."""
    if model in _RESPONSE_FORMAT_SUPPORT:
        return _RESPONSE_FORMAT_SUPPORT[model]
    
    # For now, assume gpt-4.1-mini supports it, others don't
    supports = model.startswith("openai/gpt-4.1") or model.startswith("openai/gpt-4o")
    _RESPONSE_FORMAT_SUPPORT[model] = supports
    return supports


def _schema_supports_strict_mode(call_name: str) -> bool:
    """Check if the schema for this call_name supports strict mode."""
    return False  # No schemas defined for remuneration assessment yet


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


def _schema_supports_strict_mode(call_name: str) -> bool:
    """Check if the schema for this call_name supports strict mode."""
    return False  # No schemas defined for remuneration assessment yet