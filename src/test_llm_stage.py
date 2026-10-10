#!/usr/bin/env python3
"""
LLM Stage Test Harness

Runs a pipeline stage against cached AR reports and validates LLM output.
Usage:
  cd src && python test_llm_stage.py --stage 5 --model qwen/qwen3.8-flash --companies SUN,NAB,RIO
  cd src && python test_llm_stage.py --stage 5 --model openai/gpt-4.1-mini --companies SUN,NAB,RIO
  
Environment:
  LLM_TEST_MODEL - override model for testing
  LLM_TEST_FALLBACK - optional fallback model
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

# Add src to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from openai import OpenAI
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env")

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)

CACHE_DIR = ROOT / "data" / "_cache" / "annual_report_text"
PROMPTS_DIR = ROOT / "prompts"
REFERENCE_DIR = ROOT / "data" / "_reference"


def load_cached_ar(ticker: str, year: int) -> List[Dict]:
    """Load cached AR pages for a company/year."""
    cache_file = CACHE_DIR / f"{ticker}_{year}.json"
    if not cache_file.exists():
        return []
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    return data.get("pages", [])


def load_prompt(filename: str) -> str:
    """Load system prompt."""
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file missing: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


def call_llm(model: str, system_prompt: str, user_content: str, temperature: float = 0.0) -> Optional[str]:
    """Call LLM and return content or None on error."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            temperature=temperature,
            max_tokens=4000,
            timeout=60.0,  # 60 second timeout
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"  ERROR calling {model}: {e}")
        return None


def validate_json_schema(content: str, schema: Dict) -> tuple[bool, List[str]]:
    """Validate JSON against schema. Returns (passed, errors)."""
    try:
        import jsonschema
    except ImportError:
        return True, ["jsonschema not installed, skipping schema validation"]
    
    try:
        data = json.loads(content)
        jsonschema.validate(data, schema)
        return True, []
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON: {e}"]
    except jsonschema.ValidationError as e:
        return False, [f"Schema validation failed: {e.message} at {'.'.join(map(str, e.path))}"]


def check_required_fields(content: str, required: List[str]) -> tuple[bool, List[str]]:
    """Check that required top-level fields exist and are non-null."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False, ["Invalid JSON"]
    
    errors = []
    for field in required:
        if field not in data:
            errors.append(f"Missing required field: {field}")
        elif data[field] is None:
            errors.append(f"Required field is null: {field}")
    
    return len(errors) == 0, errors


def check_citations(content: str, cached_pages: List[Dict]) -> tuple[bool, List[str]]:
    """Check that [p.N] citations refer to pages that exist in cache."""
    import re
    citations = re.findall(r'\[p\.(\d+)\]', content)
    if not citations:
        return True, []
    
    cached_page_nums = {p["page_number"] for p in cached_pages}
    errors = []
    for cite in citations:
        if int(cite) not in cached_page_nums:
            errors.append(f"Citation [p.{cite}] refers to page not in cached AR")
    
    return len(errors) == 0, errors


# Stage-specific test implementations
def validate_markdown_structure(content: str, required_sections: List[str]) -> tuple[bool, List[str]]:
    """Check that Markdown contains required sections."""
    errors = []
    for section in required_sections:
        if section.lower() not in content.lower():
            errors.append(f"Missing required section: {section}")
    return len(errors) == 0, errors


def test_stage5_remuneration(model: str, fallback: str, companies: List[tuple[str, int]]) -> Dict:
    """Test Stage 5: Remuneration Assessment (Markdown output)."""
    system_prompt = load_prompt("rem_assess.txt")
    
    # Required Markdown sections (flexible matching)
    required_sections = [
        "Document Inventory",
        "CEO remuneration major guidelines",
        "CEO remuneration secondary guidelines",
        "remuneration calculations",  # More flexible
        "Other remuneration-related resolutions"
    ]
    
    results = {}
    for ticker, year in companies:
        print(f"\n--- Testing Stage 5: {ticker} FY{year} with {model} ---")
        
        pages = load_cached_ar(ticker, year)
        if not pages:
            results[f"{ticker}_{year}"] = {"status": "SKIP", "reason": "No cached AR"}
            continue
        
        ar_text = "\n".join([f"--- Page {p['page_number']} ---\n{p.get('text', '')[:2000]}" 
                             for p in pages[:30]])
        
        user_content = f"Company: {ticker}, FY{year}\n\nAnnual Report excerpts:\n{ar_text}\n\nAssess remuneration per the prompt."
        
        content = call_llm(model, system_prompt, user_content)
        used_model = model
        
        if content is None and fallback:
            print(f"  Primary failed, trying fallback: {fallback}")
            content = call_llm(fallback, system_prompt, user_content)
            used_model = fallback
        
        if content is None:
            results[f"{ticker}_{year}"] = {"status": "FAIL", "reason": "Both models failed"}
            continue
        
        # Validate Markdown structure
        sections_ok, section_errs = validate_markdown_structure(content, required_sections)
        cites_ok, cite_errs = check_citations(content, pages)
        
        # Check for basic quality indicators
        quality_checks = []
        if len(content) < 500:
            quality_checks.append("Output too short (< 500 chars)")
        if "Uncertain" not in content and "Pass" not in content and "Fail" not in content:
            quality_checks.append("No assessment results found (Pass/Fail/Uncertain)")
        
        passed = sections_ok and cites_ok and len(quality_checks) == 0
        errors = section_errs + cite_errs + quality_checks
        
        results[f"{ticker}_{year}"] = {
            "status": "PASS" if passed else "FAIL",
            "model_used": used_model,
            "errors": errors,
            "output_length": len(content),
            "output_preview": content[:2000] + "..." if len(content) > 2000 else content
        }
        
        if passed:
            print(f"  PASS (model: {used_model}, {len(content)} chars)")
        else:
            print(f"  FAIL: {errors}")
    
    return results


def test_stage6_director(model: str, fallback: str, companies: List[tuple[str, int]]) -> Dict:
    """Test Stage 6: Director Assessment."""
    system_prompt = load_prompt("director_assess.txt")
    
    required_fields = [
        "director_name", "recommendation", "confidence", "rationale",
        "independence", "board_committees", "tenure_years"
    ]
    
    results = {}
    for ticker, year in companies:
        print(f"\n--- Testing Stage 6: {ticker} FY{year} with {model} ---")
        
        pages = load_cached_ar(ticker, year)
        if not pages:
            results[f"{ticker}_{year}"] = {"status": "SKIP", "reason": "No cached AR"}
            continue
        
        ar_text = "\n".join([f"--- Page {p['page_number']} ---\n{p.get('text', '')[:2000]}" 
                             for p in pages[:30]])
        
        user_content = f"Company: {ticker}, FY{year}\n\nAnnual Report excerpts:\n{ar_text}\n\nAssess director elections per the prompt."
        
        content = call_llm(model, system_prompt, user_content)
        used_model = model
        
        if content is None and fallback:
            content = call_llm(fallback, system_prompt, user_content)
            used_model = fallback
        
        if content is None:
            results[f"{ticker}_{year}"] = {"status": "FAIL", "reason": "Both models failed"}
            continue
        
        schema_ok, schema_errs = validate_json_schema(content, {})
        fields_ok, field_errs = check_required_fields(content, required_fields)
        cites_ok, cite_errs = check_citations(content, pages)
        
        passed = schema_ok and fields_ok and cites_ok
        errors = schema_errs + field_errs + cite_errs
        
        results[f"{ticker}_{year}"] = {
            "status": "PASS" if passed else "FAIL",
            "model_used": used_model,
            "errors": errors,
            "output_preview": content[:500] + "..." if len(content) > 500 else content
        }
        
        if passed:
            print(f"  PASS (model: {used_model})")
        else:
            print(f"  FAIL: {errors}")
    
    return results


def test_stage8_text_summary(model: str, fallback: str, companies: List[tuple[str, int]]) -> Dict:
    """Test Stage 8: Text Summaries (Markdown output)."""
    system_prompt = load_prompt("text_summaries.txt")
    
    required_fields = ["company", "ticker", "year", "financial_summary", "remuneration_summary"]
    
    results = {}
    for ticker, year in companies:
        print(f"\n--- Testing Stage 8: {ticker} FY{year} with {model} ---")
        
        # Stage 8 needs upstream outputs - skip if not available
        results[f"{ticker}_{year}"] = {"status": "SKIP", "reason": "Requires upstream outputs"}
        continue
    
    return results


STAGE_TESTS = {
    5: ("Remuneration Assessment", test_stage5_remuneration),
    6: ("Director Assessment", test_stage6_director),
    8: ("Text Summaries", test_stage8_text_summary),
}


def main():
    parser = argparse.ArgumentParser(description="Test LLM stage against cached ARs")
    parser.add_argument("--stage", type=int, required=True, choices=[5, 6, 8],
                        help="Pipeline stage to test")
    parser.add_argument("--model", type=str, 
                        default=os.getenv("LLM_TEST_MODEL", "qwen/qwen3.8-flash"),
                        help="Primary model to test")
    parser.add_argument("--fallback", type=str,
                        default=os.getenv("LLM_TEST_FALLBACK", ""),
                        help="Fallback model")
    parser.add_argument("--companies", type=str, default="SUN,NAB,RIO",
                        help="Comma-separated list of tickers (with optional year: TICKER:YEAR)")
    parser.add_argument("--output", type=str, help="Output JSON file for results")
    
    args = parser.parse_args()
    
    # Parse companies
    companies = []
    for c in args.companies.split(","):
        c = c.strip()
        if ":" in c:
            ticker, year = c.split(":")
            companies.append((ticker.strip().upper(), int(year.strip())))
        else:
            companies.append((c.strip().upper(), 2026))
    
    print(f"Testing Stage {args.stage} with model: {args.model}")
    print(f"Fallback: {args.fallback or 'none'}")
    print(f"Companies: {companies}")
    print()
    
    stage_name, test_fn = STAGE_TESTS[args.stage]
    print(f"Running {stage_name} tests...")
    
    start = time.time()
    results = test_fn(args.model, args.fallback, companies)
    elapsed = time.time() - start
    
    # Summary
    passed = sum(1 for r in results.values() if r.get("status") == "PASS")
    failed = sum(1 for r in results.values() if r.get("status") == "FAIL")
    skipped = sum(1 for r in results.values() if r.get("status") == "SKIP")
    
    print(f"\n{'='*60}")
    print(f"SUMMARY: {passed} PASS, {failed} FAIL, {skipped} SKIP")
    print(f"Time: {elapsed:.1f}s")
    print(f"{'='*60}")
    
    # Save results if requested
    if args.output:
        output_data = {
            "stage": args.stage,
            "stage_name": stage_name,
            "model": args.model,
            "fallback": args.fallback,
            "companies": companies,
            "elapsed_seconds": elapsed,
            "summary": {"passed": passed, "failed": failed, "skipped": skipped},
            "results": results
        }
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"Results saved to {args.output}")
    
    # Exit code
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()