"""
Stage 7 Remediation - Targeted Data Correction

Takes audit findings and sends focused verification requests to an LLM,
verifying flagged values against source documents before applying corrections.
"""

import os
import json
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

import pdfplumber
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text
from ar_text_cache import get_ar_pages

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")

REMEDIATION_PRIMARY_MODEL = os.getenv("REMEDIATION_PRIMARY_MODEL", "openai/gpt-4.1-mini")
REMEDIATION_FALLBACK_MODEL = os.getenv("REMEDIATION_FALLBACK_MODEL", "google/gemini-3.1-flash-lite")
AUDIT_PASS_THRESHOLD = int(os.getenv("AUDIT_PASS_THRESHOLD", "75"))

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_cached_ar_text(ticker: str, fy: int) -> Optional[List[Dict]]:
    """Load cached AR text for a specific year."""
    cache_dir = BASE_DIR / "data" / "_cache" / "annual_report_text"
    cache_file = cache_dir / f"{ticker}_{fy}.json"
    if not cache_file.exists():
        return None
    
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cached = json.load(f)
        return cached.get("pages", [])
    except Exception:
        return None


def _extract_source_context(issue: Dict, ticker: str) -> str:
    """Extract relevant source document context for a flagged issue."""
    context_parts = []
    issue_text = issue.get("issue", "")
    
    # Try to find the fiscal year mentioned in the issue
    fy_match = re.search(r"FY(\d{4})", issue_text)
    fy = int(fy_match.group(1)) if fy_match else None
    
    if fy:
        pages = _load_cached_ar_text(ticker, fy)
        if pages:
            # Find pages with financial data
            financial_keywords = ["NPAT", "net profit", "revenue", "earnings per share", "EPS",
                                 "dividend", "share price", "net assets", "total assets"]
            
            relevant_pages = []
            for page in pages:
                text = page.get("text", "").lower()
                if any(kw.lower() in text for kw in financial_keywords):
                    relevant_pages.append(page)
                    if len(relevant_pages) >= 10:  # max 10 pages
                        break
            
            if relevant_pages:
                context_parts.append(f"=== RELEVANT EXCERPTS FROM FY{fy} ANNUAL REPORT ===")
                for page in relevant_pages:
                    page_num = page.get("page_number", "?")
                    text = page.get("text", "")
                    if text:
                        # Truncate long pages
                        excerpt = text[:3000] + ("..." if len(text) > 3000 else "")
                        context_parts.append(f"\n--- Page {page_num} ---\n{excerpt}")
    
    # Add other year context if available for scale comparison
    other_years = issue.get("other_years_for_scale", {})
    if other_years:
        context_parts.append("\n=== OTHER YEAR VALUES FOR SCALE ===")
        for yr, val in sorted(other_years.items()):
            context_parts.append(f"FY{yr}: {val}")
    
    return "\n".join(context_parts)


def _send_remediation_request(
    client: OpenAI,
    model: str,
    issue: Dict,
    ticker: str,
    logger=None
) -> Tuple[Optional[Any], str, str]:
    """Send a targeted remediation request to LLM for value verification.
    
    Returns: (corrected_value, citation, confidence)
    """
    issue_text = issue.get("issue", "")
    recommendation = issue.get("recommendation", "")
    severity = issue.get("severity", "UNKNOWN")
    
    source_context = _extract_source_context(issue, ticker)
    
    prompt = """You are a financial data auditor verifying flagged values against source documents.

A data quality audit flagged a potential inconsistency. Review the source document excerpts and:
1. Find the CORRECT value from the source documents
2. Verify the unit (e.g., $millions, $thousands, raw dollars)
3. Cite the exact page number where the value appears
4. Report your confidence level

Return ONLY valid JSON:
{
  "found_value": true|false,
  "corrected_value": number or null,
  "unit": "$millions|$thousands|raw_dollars|other",
  "source_page": "p.N",
  "source_quote": "short excerpt showing the value",
  "confidence": "high|medium|low",
  "explanation": "brief explanation of your finding"
}
"""
    
    user_content = f"""
ISSUE: {issue_text}
SEVERITY: {severity}
RECOMMENDATION: {recommendation}

COMPANY: {ticker}

{source_context}

Please find the correct value from the source documents. If you cannot find it, set found_value=false and explain what you'd need.
"""
    
    if logger:
        logger.info(f"[REMEDIATION] Sending verification request: {issue_text[:100]}...")
    
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content}
                ],
                timeout=120.0
            )
            
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("LLM returned empty content")
            
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()
            
            start = content.find('{')
            end = content.rfind('}')
            if start >= 0 and end > start:
                content = content[start:end+1]
            
            result = json.loads(content)
            
            if result.get("found_value") and result.get("corrected_value") is not None:
                if logger:
                    logger.info(f"[REMEDIATION] Found value: {result['corrected_value']} ({result.get('unit', 'unknown')}) on {result.get('source_page', '?')}")
                return result["corrected_value"], result.get("source_page", ""), result.get("confidence", "medium")
            else:
                if logger:
                    logger.info(f"[REMEDIATION] Could not find value: {result.get('explanation', 'unknown')}")
                return None, "", result.get("confidence", "low")
                
        except json.JSONDecodeError as e:
            if attempt == 2:
                raise
            time.sleep(2)
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
            raise
    
    return None, "", "low"


def _apply_correction(workspace_dir: Path, issue: Dict, corrected_value: Any, citation: str, logger=None) -> str:
    """Apply a corrected value to the appropriate stage output file.
    
    Returns a description of what was changed for the technical log.
    """
    issue_text = issue.get("issue", "")
    
    # Determine which stage output to update based on issue content
    perf_path = workspace_dir / "extracted_json" / "performance_history_5yr.json"
    
    # Try to find the FY in the issue and the metric name
    fy_match = re.search(r"FY(\d{4})", issue_text)
    fy = fy_match.group(1) if fy_match else None
    
    changed = None
    mertic = None
    
    if fy and perf_path.exists():
        with open(perf_path, "r", encoding="utf-8") as f:
            perf_data = json.load(f)
        
        years = perf_data.get("years", {})
        ceo_pay = perf_data.get("ceo_realised_pay", {})
        
        # Check financial metrics
        for metric_key in ["npat", "underlying_npat", "revenue", "net_assets", "share_price", "dividend", "eps"]:
            if metric_key.lower() in issue_text.lower():
                if fy in years and years[fy].get(metric_key) is not None:
                    old_val = years[fy][metric_key]
                    years[fy][metric_key] = corrected_value
                    changed = f"FY{fy} {metric_key}: {old_val} -> {corrected_value} (source: {citation})"
                    break
        
        if not changed and fy in ceo_pay:
            old_val = ceo_pay[fy].get("value_m")
            if old_val is not None:
                ceo_pay[fy]["value_m"] = corrected_value
                ceo_pay[fy]["source_page"] = citation
                changed = f"FY{fy} CEO realised pay: ${old_val}m -> ${corrected_value}m (source: {citation})"
        
        if changed:
            with open(perf_path, "w", encoding="utf-8") as f:
                json.dump(perf_data, f, indent=2, ensure_ascii=False)
    
    if not changed:
        changed = f"Could not automatically apply correction for: {issue_text[:100]}..."
    
    return changed


def run_remediation(workspace_dir: Path, critical_issues: List[Dict], ticker: str, logger=None) -> Dict[str, Any]:
    """Run targeted remediation for critical/high audit findings.
    
    Returns remediation summary with before/after for each fixed issue.
    """
    remediation_results = {
        "started_at": datetime.now().isoformat(),
        "model_used": REMEDIATION_PRIMARY_MODEL,
        "issues_evaluated": len([i for i in critical_issues if i.get("severity") in ("CRITICAL", "HIGH")]),
        "issues_fixed": 0,
        "fixes": [],
        "unfixable": [],
    }
    
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )
    
    for issue in critical_issues:
        severity = issue.get("severity", "UNKNOWN")
        if severity not in ("CRITICAL", "HIGH"):
            continue
        
        print(f"\n[REMEDIATION] Checking: {issue['issue'][:100]}...")
        
        corrected_value = None
        citation = ""
        confidence = "low"
        
        try:
            corrected_value, citation, confidence = _send_remediation_request(
                client, REMEDIATION_PRIMARY_MODEL, issue, ticker, logger
            )
        except Exception as e:
            if logger:
                logger.warning(f"[REMEDIATION] Primary model failed: {e}, trying fallback")
            try:
                corrected_value, citation, confidence = _send_remediation_request(
                    client, REMEDIATION_FALLBACK_MODEL, issue, ticker, logger
                )
            except Exception as e2:
                if logger:
                    logger.error(f"[REMEDIATION] Both models failed: {e2}")
                remediation_results["unfixable"].append({
                    "issue": issue["issue"][:100],
                    "reason": str(e2)
                })
                continue
        
        if corrected_value is not None and confidence != "low":
            # Apply the correction
            change_desc = _apply_correction(workspace_dir, issue, corrected_value, citation, logger)
            
            fix_record = {
                "issue": issue["issue"][:150],
                "original_recommendation": issue.get("recommendation", ""),
                "corrected_value": corrected_value,
                "citation": citation,
                "confidence": confidence,
                "change": change_desc
            }
            remediation_results["fixes"].append(fix_record)
            remediation_results["issues_fixed"] += 1
            
            print(f"  FIXED: {change_desc}")
            if logger:
                logger.info(f"[REMEDIATION] Fixed: {change_desc}")
        else:
            remediation_results["unfixable"].append({
                "issue": issue["issue"][:100],
                "reason": f"LLM confidence: {confidence}" + (f", citation: {citation}" if citation else "")
            })
            print(f"  SKIPPED (confidence: {confidence})")
    
    remediation_results["ended_at"] = datetime.now().isoformat()
    return remediation_results