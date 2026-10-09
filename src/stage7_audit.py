"""
Stage 7 Audit - Data Quality Check

Performs comprehensive data quality audit across all completed pipeline stages.
Combines automated validation (schema, ranges, consistency) with LLM qualitative review.
"""

import os
import json
import re
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

from logging_utils import write_supporting_json, write_supporting_text
from news_search_results import load_news_block, has_news_results
from llm_json import parse_llm_json
from llm_config import audit_models

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

AUDIT_PRIMARY_MODEL, AUDIT_FALLBACK_MODEL = audit_models()

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"

# Token bucket
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


def _auto_validate(workspace_dir: Path, logger=None) -> List[Dict[str, Any]]:
    """Run automated validation checks on all stage outputs."""
    issues = []
    
    # Check 1: NoM JSON
    nom_dir = workspace_dir / "extracted_json" / "notices"
    nom_archived = workspace_dir / "extracted_json" / "notices_archived"
    
    # Find NoM JSON in either directory
    nom_json_files = []
    for search_dir in [nom_dir, nom_archived]:
        if search_dir.exists():
            nom_json_files.extend(search_dir.glob("*.json"))
    
    active_nom = any(nom_dir.glob("*.json")) if nom_dir.exists() else False
    archived_nom = any(nom_archived.glob("*.json")) if nom_archived.exists() else False
    
    if not active_nom and not archived_nom:
        issues.append({"check": "NoM JSON", "status": "FAIL", "detail": "NoM JSON not found in either active or archived directory"})
    elif active_nom and not archived_nom:
        pass  # Active only is fine
    elif not active_nom and archived_nom:
        issues.append({"check": "NoM JSON location", "status": "WARN", "detail": "NoM JSON only in archived directory - may need restoration for downstream stages"})
    else:
        issues.append({"check": "NoM JSON location", "status": "WARN", "detail": "NoM JSON exists in both locations - possible duplicate processing"})
    
    if nom_json_files:
        try:
            with open(nom_json_files[0], encoding="utf-8") as f:
                nom_data = json.load(f)
            
            if "Voting_Resolutions" not in nom_data and "voting_resolutions" not in nom_data:
                issues.append({"check": "NoM JSON", "status": "FAIL", "detail": "No voting resolutions found in NoM JSON"})
            
            agm = nom_data.get("AGM_Details", {})
            if agm:
                meeting_date = agm.get("agm_date") or agm.get("meeting_date")
                if not meeting_date:
                    issues.append({"check": "NoM JSON", "status": "WARN", "detail": "Meeting date missing from NoM JSON AGM_Details"})
                else:
                    # Meeting date is present - check it's in expected format
                    has_weekday = any(day in meeting_date.lower() for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])
                    if not has_weekday:
                        issues.append({"check": "NoM JSON", "status": "WARN", "detail": f"Meeting date '{meeting_date}' may not include weekday"})
            else:
                issues.append({"check": "NoM JSON", "status": "WARN", "detail": "AGM_Details section not found in NoM JSON"})
        except Exception as e:
            issues.append({"check": "NoM JSON", "status": "FAIL", "detail": f"Cannot parse NoM JSON: {e}"})
    
    # Check 2: Performance History
    perf_path = workspace_dir / "extracted_json" / "performance_history_5yr.json"
    if not perf_path.exists():
        issues.append({"check": "Performance history", "status": "FAIL", "detail": "performance_history_5yr.json not found"})
    else:
        try:
            with open(perf_path, encoding="utf-8") as f:
                perf_data = json.load(f)
            
            extraction_status = perf_data.get("extraction_status", "unknown")
            years = perf_data.get("years", {})
            ceo_pay = perf_data.get("ceo_realised_pay", {})
            
            if extraction_status == "failed":
                issues.append({"check": "Performance extraction", "status": "FAIL", "detail": "Extraction status is 'failed'"})
            elif extraction_status == "partial":
                issues.append({"check": "Performance extraction", "status": "WARN", "detail": f"Extraction is partial: {len(years)} years, {len(ceo_pay)} CEO pay years"})
            
            # Check CEO pay values are in reasonable range ($1M-$50M)
            for fy, pay in ceo_pay.items():
                val = pay.get("value_m", 0)
                if isinstance(val, (int, float)):
                    if val > 1000:
                        issues.append({"check": "CEO pay range", "status": "FAIL", 
                                      "detail": f"FY{fy} CEO pay ${val:.1f}m suspiciously high (off by 1000x)"})
                    elif val > 100:
                        issues.append({"check": "CEO pay range", "status": "WARN",
                                      "detail": f"FY{fy} CEO pay ${val:.1f}m above expected range"})
            
            # Check financial metric unit consistency across years
            financial_metrics = ["npat", "underlying_npat", "revenue", "net_assets"]
            for metric in financial_metrics:
                values = []
                for fy, data in sorted(years.items()):
                    val = data.get(metric)
                    if isinstance(val, (int, float)) and val is not None:
                        values.append((fy, val))
                
                if len(values) >= 2:
                    # Check for unit inconsistencies: values spanning >3 orders of magnitude
                    vals_only = [v[1] for v in values]
                    max_val = max(vals_only)
                    min_val = min(vals_only) if min(vals_only) > 0 else 1
                    if max_val / min_val > 1000:
                        issues.append({
                            "check": "Metric unit consistency",
                            "status": "CRITICAL",
                            "detail": f"{metric} values span >1000x across years ({min_val:.1f} to {max_val:.1f}) - likely unit mismatch. Values: {dict(values)}"
                        })
                    elif max_val / min_val > 100:
                        issues.append({
                            "check": "Metric unit consistency", 
                            "status": "WARN",
                            "detail": f"{metric} values vary widely across years (range: {min_val:.1f} to {max_val:.1f}) - possible unit issue. Values: {dict(values)}"
                        })
            
            # Check years have data
            years_with_data = 0
            for fy, data in years.items():
                if any(data.get(k) is not None for k in ["npat", "share_price", "dividend", "eps"] if k in data):
                    years_with_data += 1
            
            if years_with_data == 0 and not ceo_pay:
                issues.append({"check": "Performance data", "status": "FAIL", "detail": "No financial data extracted for any year"})
            elif years_with_data < 3:
                issues.append({"check": "Performance data", "status": "WARN", "detail": f"Only {years_with_data} years have financial data"})
                
        except Exception as e:
            issues.append({"check": "Performance history", "status": "FAIL", "detail": f"Cannot parse: {e}"})
    
    # Check 3: Director Evaluation
    dir_evals = list((workspace_dir / "reports").glob("*Director_Evaluation*"))
    if not dir_evals:
        issues.append({"check": "Director assessment", "status": "WARN", "detail": "No Director Evaluation output found"})
    
    # Check 4: Remuneration Assessment
    rem_assess_md = list((workspace_dir / "reports").glob("*Remuneration_Assessment.md"))
    rem_assess_json = list((workspace_dir / "extracted_json").glob("*Remuneration_Assessment.json"))
    if not rem_assess_md and not rem_assess_json:
        issues.append({"check": "Remuneration assessment", "status": "WARN", "detail": "No Remuneration Assessment output found"})
    else:
        try:
            if rem_assess_json:
                with open(rem_assess_json[0], encoding="utf-8") as f:
                    rem_data = json.load(f)
                # Check if the assessment has content
                raw_content = rem_data.get("raw_content", "")
                if raw_content:
                    # Count uncertain results by looking for common patterns
                    uncertain_count = raw_content.lower().count("unc")
                    if uncertain_count > 5:
                        issues.append({"check": "Remuneration assessment", "status": "WARN", 
                                    "detail": f"Many 'Uncertain' results detected in markdown - source docs may be insufficient"})
        except Exception as e:
            issues.append({"check": "Remuneration assessment", "status": "WARN", "detail": f"Cannot parse JSON: {e}"})
    
    # Check 5: Other Resolutions Assessment
    other_assess = list((workspace_dir / "reports").glob("*Other_Resolutions_Assessment*"))
    if not other_assess:
        issues.append({"check": "Other resolutions", "status": "WARN", "detail": "No Other Resolutions Assessment output found"})
    else:
        try:
            with open(other_assess[0], encoding="utf-8") as f:
                other_data = json.load(f)
            resolutions = other_data.get("resolution_categorisation", [])
            in_scope = [r for r in resolutions if r.get("category") == "in_scope"]
            assessed = other_data.get("in_scope_resolutions", [])
            if len(assessed) < len(in_scope):
                issues.append({"check": "Other resolutions", "status": "WARN", 
                              "detail": f"Only {len(assessed)}/{len(in_scope)} in-scope resolutions assessed"})
        except Exception as e:
            issues.append({"check": "Other resolutions", "status": "WARN", "detail": f"Cannot parse: {e}"})
    
    # Check 6: Annual Report files exist
    ar_dir = workspace_dir / "annual_reports"
    ar_files = list(ar_dir.glob("*.pdf")) if ar_dir.exists() else []
    expected_years = 5  # 2022-2026
    if len(ar_files) < expected_years:
        issues.append({"check": "Annual reports", "status": "WARN", 
                      "detail": f"Only {len(ar_files)}/{expected_years} AR files present"})
    
    # Check 7: VI Reports exist. Count only *_VIR.pdf: the vi_reports/
    # directory also holds AGM/EGM reports (Stage 3), which must not mask
    # missing VI reports.
    vi_dir = workspace_dir / "vi_reports"
    vi_files = list(vi_dir.glob("*_VIR.pdf")) if vi_dir.exists() else []
    if len(vi_files) < expected_years:
        issues.append({"check": "VI Reports", "status": "WARN", 
                      "detail": f"Only {len(vi_files)}/{expected_years} VI reports present"})
    
    # Check 8: Pipeline state consistency
    state_path = workspace_dir / "state.json"
    if state_path.exists():
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            pipeline_status = state.get("pipeline_status", {})
            extracted_data = state.get("extracted_data", {})
            
            if pipeline_status.get("performance_table_built") and not perf_data.get("years"):
                issues.append({"check": "State consistency", "status": "FAIL", 
                              "detail": "Flag says performance_table_built=True but years is empty"})
            
            has_pdfs = any(ar_dir.glob("*.pdf")) if ar_dir.exists() else False
            if not pipeline_status.get("annual_reports_downloaded") and has_pdfs:
                issues.append({"check": "State consistency", "status": "WARN", 
                              "detail": "AR files exist but flag says annual_reports_downloaded=False"})
        except Exception as e:
            issues.append({"check": "State file", "status": "WARN", "detail": f"Cannot parse state.json: {e}"})
    
    if logger:
        logger.info(f"Automated audit complete: {len(issues)} issues found")
    
    return issues


def _build_audit_summary(workspace_dir: Path, auto_issues: List[Dict], logger=None) -> str:
    """Build a comprehensive audit summary for LLM review."""
    
    summary_parts = []
    
    # Pipeline state
    state_path = workspace_dir / "state.json"
    if state_path.exists():
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        summary_parts.append("## Pipeline State")
        for k, v in state.get("pipeline_status", {}).items():
            summary_parts.append(f"- {k}: {v}")
    
    # Automated issues
    if auto_issues:
        summary_parts.append("\n## Automated Validation Issues")
        by_status = {"FAIL": [], "WARN": [], "PASS": []}
        for issue in auto_issues:
            by_status.setdefault(issue["status"], []).append(issue)
        
        for status in ["FAIL", "WARN", "PASS"]:
            for issue in by_status.get(status, []):
                summary_parts.append(f"- [{issue['status']}] {issue['check']}: {issue['detail']}")
    
    # Include key outputs for qualitative review
    perf_path = workspace_dir / "extracted_json" / "performance_history_5yr.json"
    if perf_path.exists():
        with open(perf_path) as f:
            perf_data = json.load(f)
        years = perf_data.get("years", {})
        ceo_pay = perf_data.get("ceo_realised_pay", {})
        
        summary_parts.append("\n## Performance Summary")
        summary_parts.append(f"- Extraction status: {perf_data.get('extraction_status', 'unknown')}")
        summary_parts.append(f"- Years with data: {list(years.keys())}")
        
        for fy, data in sorted(years.items()):
            metrics = {k: v for k, v in data.items() if v is not None and k not in ["missing_metrics"]}
            summary_parts.append(f"- FY{fy}: {json.dumps(metrics)}")
        
        summary_parts.append("\n## CEO Realised Pay")
        for fy, pay in sorted(ceo_pay.items()):
            summary_parts.append(f"- FY{fy}: ${pay.get('value_m', 0):.2f}m ({pay.get('source_method')})")
    
    return "\n".join(summary_parts)


def _audit_passes(audit_result: dict) -> bool:
    """Whether the audit gate passes: the data-quality score must clear the
    threshold recorded on the result.

    The qualitative ``overall_assessment`` (PASS/PARTIAL/FAIL) is recorded in
    the output but does not gate - a score below threshold must fail the
    stage even when the LLM says PARTIAL. An earlier version read
    ``audit_result.get("aurdit_passed_threshold")`` (a typo of the key set two
    lines above), so the lookup always returned None and the gate silently
    degenerated to ``overall_assessment != "FAIL"``.
    """
    return bool(audit_result.get("audit_passed_threshold"))


def run_audit(workspace_dir: Path, supporting_data_dir: Path, logger=None) -> bool:
    """
    Run comprehensive data quality audit with remediation loop.
    
    Loop: audit -> score < threshold -> remediate -> re-audit (max 2 remediation cycles).
    Returns True if audit passes (score >= threshold) or remediation exhausted.
    """
    from stage7_remediation import run_remediation
    
    AUDIT_PASS_THRESHOLD = int(os.getenv("AUDIT_PASS_THRESHOLD", "75"))
    MAX_REMEDIATION_CYCLES = 2  # 3 total audit passes (1 initial + 2 remediation)
    
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
        year = int(workspace_name.split("_")[1])
    
    if logger:
        logger.stage_start(f"Stage 7 Audit: Data Quality Audit for {ticker} FY{year}")
    
    all_remediation_results = []
    audit_result = {}
    score = 0
    
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )
    
    prompt = """You are a data quality auditor reviewing pipeline outputs for ASA (Australian Shareholders' Association) company assessments.

Review the provided audit summary and identify:
1. Any data quality issues (missing fields, invalid values, inconsistencies)
2. Any logical errors (e.g., CEO pay showing $3.6B instead of $3.6M)
3. Any missing documents that would improve assessment quality
4. Any stage outputs that should be re-run
5. Overall assessment of pipeline completeness

If a "SUPPLEMENTARY: NEWS SEARCH RESULTS" block is included in the user
message, it is pre-fetched news context - NOT a pipeline output. Do not audit
it, do not list it as a missing document, and do not raise its (non-)use as a
data-quality issue. Use it only to sanity-check whether an assessment's factual
claims are consistent with publicly reported events.

Output as JSON:
{
  "overall_assessment": "PASS|PARTIAL|FAIL",
  "critical_issues": [{"issue": "string", "severity": "CRITICAL|HIGH|MEDIUM|LOW", "recommendation": "string"}],
  "stages_requiring_rerun": ["stage_name"],
  "missing_documents": ["document_name"],
  "data_quality_score": 0-100,
  "summary": "string"
}
"""
    
    # Remediation loop
    for iteration in range(MAX_REMEDIATION_CYCLES + 1):  # 0 = initial, 1-2 = remediation
        if iteration > 0:
            print(f"\n{'='*60}")
            print(f" REMEDIATION CYCLE {iteration}/{MAX_REMEDIATION_CYCLES}")
            print(f"{'='*60}")
            if logger:
                logger.info(f"Starting remediation cycle {iteration}/{MAX_REMEDIATION_CYCLES}")
        
        # Step 1: Automated validation
        auto_issues = _auto_validate(workspace_dir, logger)
        write_supporting_json(supporting_data_dir, f"auto_audit_issues_{iteration}.json", auto_issues)
        
        # Step 2: Build summary for LLM review
        audit_summary = _build_audit_summary(workspace_dir, auto_issues, logger)
        write_supporting_text(supporting_data_dir, f"audit_summary_{iteration}.txt", audit_summary)
        
        # Include previous remediation results in summary
        if all_remediation_results:
            audit_summary += "\n\n## Previous Remediation Actions\n"
            for i, rem_result in enumerate(all_remediation_results):
                audit_summary += f"\n### Cycle {i+1}\n"
                for fix in rem_result.get("fixes", []):
                    audit_summary += f"- FIXED: {fix.get('change', '')}\n"
        
        # Supplementary context: pre-fetched news (Stage 7.5). Included only
        # when results actually exist - when absent, the model is not shown a
        # "no news" marker that it might flag as a missing document.
        audit_user_content = f"Company: {ticker} FY{year}\n\n{audit_summary}"
        if has_news_results(workspace_dir):
            audit_user_content = (
                f"Company: {ticker} FY{year}\n\n"
                f"## SUPPLEMENTARY: NEWS SEARCH RESULTS (pre-fetched, NOT a pipeline output)\n\n"
                f"{load_news_block(workspace_dir)}\n\n"
                f"{audit_summary}"
            )

        # Step 3: Send to LLM for qualitative review
        _token_bucket.take()
        
        try:
            response = client.chat.completions.create(
                model=AUDIT_PRIMARY_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": audit_user_content}
                ],
                timeout=120.0
            )
            
            content = response.choices[0].message.content
            if content:
                try:
                    audit_result = parse_llm_json(content.strip())
                except ValueError as ve:
                    audit_result = {"error": f"LLM JSON parse failed: {ve}"}
            else:
                audit_result = {"error": "LLM returned empty response"}
        except Exception as e:
            if logger:
                logger.warning(f"LLM audit failed: {e}")
            try:
                response = client.chat.completions.create(
                    model=AUDIT_FALLBACK_MODEL,
                    messages=[{"role": "system", "content": prompt},
                            {"role": "user", "content": audit_user_content}],
                    timeout=120.0
                )
                content = response.choices[0].message.content
                if content:
                    try:
                        audit_result = parse_llm_json(content.strip())
                    except ValueError as ve:
                        audit_result = {"error": f"Fallback LLM JSON parse failed: {ve}"}
                else:
                    audit_result = {"error": "Fallback LLM returned empty response"}
            except Exception as e2:
                audit_result = {"error": f"Both models failed: {e}, {e2}", "auto_issues": auto_issues}
        
        # Add automated issues
        audit_result["auto_validation_issues"] = auto_issues
        audit_result["audit_timestamp"] = datetime.now().isoformat()
        audit_result["company"] = ticker
        audit_result["financial_year"] = str(year)
        audit_result["iteration"] = iteration
        audit_result["remediation_cycles_applied"] = len(all_remediation_results)
        
        score = audit_result.get("data_quality_score", 0)
        
        # Print iteration summary
        print(f"\n--- Audit Iteration {iteration} ---")
        print(f"  Score: {score} | Threshold: {AUDIT_PASS_THRESHOLD}")
        print(f"  Critical issues: {len(audit_result.get('critical_issues', []))}")
        print(f"  Auto issues: {len(auto_issues)}")
        
        # Check if we pass or if we should remediate
        if score >= AUDIT_PASS_THRESHOLD:
            print(f"  [PASS] Score {score} >= threshold {AUDIT_PASS_THRESHOLD}")
            if logger:
                logger.info(f"Audit PASSED with score {score}")
            break
        
        # Check if we should remediate more
        critical_issues = audit_result.get("critical_issues", [])
        high_issues = [i for i in critical_issues if i.get("severity") in ("CRITICAL", "HIGH")]
        
        if iteration >= MAX_REMEDIATION_CYCLES:
            print(f"  [FAIL] Remediation exhausted ({MAX_REMEDIATION_CYCLES} cycles) - score {score} < threshold {AUDIT_PASS_THRESHOLD}")
            if logger:
                logger.warning(f"Audit remediation exhausted with score {score}")
            break
        
        if not high_issues:
            print(f"  No CRITICAL/HIGH issues to remediate - accepting score {score}")
            break
        
        # Run remediation
        remediation_result = run_remediation(workspace_dir, high_issues, ticker, logger)
        all_remediation_results.append(remediation_result)
        
        if logger:
            logger.info(f"Remediation cycle {iteration+1} complete: {remediation_result['issues_fixed']} fixed, {len(remediation_result['unfixable'])} unfixable")
    
    # Finalize and save
    audit_result["remediation_history"] = all_remediation_results
    audit_result["audit_passed_threshold"] = score >= AUDIT_PASS_THRESHOLD
    audit_result["threshold_used"] = AUDIT_PASS_THRESHOLD
    audit_result["max_cycles"] = MAX_REMEDIATION_CYCLES
    
    result_path = workspace_dir / "reports" / f"{ticker}_{year}_Audit_Report.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(audit_result, f, indent=2, ensure_ascii=False)
    
    write_supporting_json(supporting_data_dir, "audit_result.json", audit_result)
    
    # Print final summary
    print(f"\n{'='*60}")
    print(f" Stage 7 Audit Results for {ticker} FY{year}")
    print(f"{'='*60}")
    print(f" Final Score: {score} | Threshold: {AUDIT_PASS_THRESHOLD} {'[PASS]' if score >= AUDIT_PASS_THRESHOLD else '[FAIL]'}")
    print(f" Overall: {audit_result.get('overall_assessment', 'UNKNOWN')}")
    print(f" Remediation cycles: {len(all_remediation_results)}")
    print(f" Total fixes applied: {sum(r.get('issues_fixed', 0) for r in all_remediation_results)}")
    if audit_result.get('summary'):
        print(f"\n Summary: {audit_result['summary'][:200]}...")
    
    # Same gate the remediation loop already uses to stop remediating
    # (score >= threshold). The old line read "aurdit_passed_threshold" -
    # a typo - so this .get() always returned None and the gate degenerated
    # to overall_assessment != "FAIL": a PARTIAL below threshold passed.
    audit_passed = _audit_passes(audit_result)
    
    if logger:
        logger.stage_end("Stage 7 Audit: Data Quality Audit", audit_passed)
    
    return audit_passed


def _run_audit_single(workspace_dir: Path, supporting_data_dir: Path, logger=None) -> bool:
    """Backward-compatible single audit call (no remediation loop)."""
    return run_audit(workspace_dir, supporting_data_dir, logger)


# Alias for orchestrator compatibility
run_pipeline = run_audit


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        workspace = Path(sys.argv[1])
        supporting = workspace / "supporting_data" / "stage7_audit"
        result = run_audit(workspace, supporting)
        print(f"\nResult: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/stage7_audit.py <workspace_dir>")
        sys.exit(1)