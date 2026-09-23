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

OTHER_RESOLUTIONS_PRIMARY_MODEL = os.getenv("OTHER_RESOLUTIONS_PRIMARY_MODEL", "openai/gpt-4.1-mini")
OTHER_RESOLUTIONS_FALLBACK_MODEL = os.getenv("OTHER_RESOLUTIONS_FALLBACK_MODEL", "google/gemini-3.1-flash-lite")

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"

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


def _load_prompt(filename: str) -> str:
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _extract_text_from_pdf(pdf_path: Path) -> str:
    text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Page {idx} ---\n" + page_text
    return text[:200000]  # Cap at 200k chars to fit context window


def _find_nom_json(workspace_dir: Path) -> Optional[Path]:
    """Find NoM JSON in notices directory (checking both active and archived)."""
    nom_dir = workspace_dir / "extracted_json" / "notices"
    nom_archived = workspace_dir / "extracted_json" / "notices_archived"
    
    for search_dir in [nom_dir, nom_archived]:
        if search_dir.exists():
            jsons = list(search_dir.glob("*.json"))
            if jsons:
                return jsons[0]
    return None


def _load_nom_json(nom_json_path: Path) -> dict:
    with open(nom_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_ar_file(workspace_dir: Path, year: int) -> Optional[Path]:
    annual_reports_dir = workspace_dir / "annual_reports"
    for f in annual_reports_dir.glob("*.pdf"):
        match = re.search(r"(\d{4})", f.stem)
        if match and int(match.group(1)) == year:
            return f
    return None


def _get_schema_for_call(call_name: str) -> dict | None:
    return None  # No specific schemas for other resolutions assessment yet


def _check_response_format_support(model: str) -> bool:
    if model in _RESPONSE_FORMAT_SUPPORT:
        return _RESPONSE_FORMAT_SUPPORT[model]
    supports = model.startswith("openai/gpt-4.1") or model.startswith("openai/gpt-4o")
    _RESPONSE_FORMAT_SUPPORT[model] = supports
    return supports


def _validate_against_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    try:
        import jsonschema
        jsonschema.validate(instance=data, schema=schema)
        return True, []
    except ImportError:
        return _basic_validate(data, schema)
    except Exception as e:
        return False, [str(e)]


def _basic_validate(data: dict, schema: dict) -> tuple[bool, list[str]]:
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


def _extract_error_status(err: Exception) -> int | None:
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


def _parse_json_from_response(content: str) -> dict:
    if content.startswith("```json"):
        content = content[7:-3].strip()
    elif content.startswith("```"):
        content = content[3:-3].strip()
    
    start = content.find('{')
    end = content.rfind('}')
    if start >= 0 and end > start:
        content = content[start:end+1]
    
    content = content.replace("'", '"')
    content = re.sub(r',(\s*[}\]])', r'\1', content)
    
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
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
    
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM returned {type(parsed).__name__}, expected JSON object")
    
    return parsed


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
            logger.warning(f"[{call_name}] error: model={used_model} status={status_code} error={str(err)[:500]}")
        return status_code

    wait = _token_bucket.take()
    if wait > 0:
        metadata["rate_limit_wait_seconds"] = wait
        if logger:
            logger.info(f"[{call_name}] Rate limiting: waiting {wait:.1f}s")
        time.sleep(wait)

    primary_error = None
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(model=model, messages=[
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": user_content}
            ], timeout=240.0)
            choices = getattr(response, "choices", None)
            if not choices:
                raise RuntimeError("No choices returned from LLM")
            
            content = response.choices[0].message.content
            if content is None or not content.strip():
                raise RuntimeError("LLM returned no text content")
            
            parsed = _parse_json_from_response(content.strip())
            
            attempt_meta = {
                "attempt": attempt, "model": model,
                "status": "success", "is_fallback": False,
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
                logger.info(f"[{call_name}] success: model={model} elapsed={attempt_meta['elapsed_seconds']:.1f}s")
            return parsed
            
        except Exception as err1:
            _log_error(err1, attempt, model, is_fallback=False)
            primary_error = err1
            error_str = str(err1)
            is_rate_limit = "429" in error_str or "rate limit" in error_str.lower()
            
            if is_rate_limit and attempt < max_retries:
                wait_time = 2 ** attempt
                if logger:
                    logger.warning(f"[{call_name}] Rate limit, waiting {wait_time}s: {err1}")
                time.sleep(wait_time)
                continue
            
            is_json_error = (
                "JSON validation failed" in error_str or "LLM returned" in error_str
                or "expected JSON object" in error_str or "Expecting value" in error_str
                or "Extra data" in error_str or "JSONDecodeError" in type(err1).__name__
            )
            if is_json_error and attempt < json_validation_retries:
                if logger:
                    logger.warning(f"[{call_name}] JSON failed, retrying ({attempt + 1}/{json_validation_retries})")
                time.sleep(1)
                continue
            
            if not fallback_model:
                metadata["status"] = "failed"
                metadata["final_error"] = str(err1)
                _write_metadata()
                raise
            
            if logger:
                logger.info(f"[{call_name}] Falling back to {fallback_model}")
            try:
                response = client.chat.completions.create(model=fallback_model, messages=[
                    {"role": "system", "content": prompt_text},
                    {"role": "user", "content": user_content}
                ], timeout=240.0)
                choices = getattr(response, "choices", None)
                if not choices:
                    raise RuntimeError("Fallback returned no choices")
                content = response.choices[0].message.content
                if content is None or not content.strip():
                    raise RuntimeError("Fallback returned no text")
                parsed = _parse_json_from_response(content.strip())
                
                attempt_meta = {
                    "attempt": attempt, "model": fallback_model,
                    "status": "success", "is_fallback": True,
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
                _log_error(err2, attempt, fallback_model, is_fallback=True)
                if "429" in str(err2) or "rate limit" in str(err2).lower():
                    raise RuntimeError("Both models rate limited")
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
        print(f"[Stage 7] No NoM JSON found in {workspace_dir / 'extracted_json' / 'notices'} or archived directory")
        if logger:
            logger.error("No NoM JSON found for other resolutions assessment")
        return False
    
    if not ar_path:
        print(f"[Stage 7] No Annual Report found for FY{year} in {workspace_dir / 'annual_reports'}")
        if logger:
            logger.error(f"No Annual Report found for FY{year}")
        return False
    
    # Load documents
    nom_data = _load_nom_json(nom_json_path)
    ar_text = _extract_text_from_pdf(ar_path)
    
    # Load prompt
    prompt_text = _load_prompt("other_resolutions_assess.txt")
    
    # Build user content with source documents
    user_content = f"""
COMPANY: {ticker}
FINANCIAL_YEAR: {year}

=== INPUT 1: NOTICE OF MEETING EXTRACTED JSON ===
{json.dumps(nom_data, indent=2)}

=== INPUT 2: ANNUAL REPORT ===
{ar_text}
"""
    
    # Call LLM for assessment
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )
    
    result = _call_llm(
        client=client,
        model=OTHER_RESOLUTIONS_PRIMARY_MODEL,
        prompt_text=prompt_text,
        user_content=user_content,
        logger=logger,
        fallback_model=OTHER_RESOLUTIONS_FALLBACK_MODEL,
        call_name="other_resolutions",
        supporting_data_dir=supporting_data_dir
    )
    
    # Save result
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Other_Resolutions_Assessment.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    
    # Also save raw supporting data
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