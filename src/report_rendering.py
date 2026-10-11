"""
Stage 10 - Word Report Rendering

Renders the final Word document (.docx) report from all upstream pipeline outputs.
Uses the template at data/_reference/word_templates/VI_Report_Blank.docx

Fills in:
- Company metadata (AGM details from NoM JSON)
- Resolution voting table (from NoM JSON + assessments)
- Financial history table (from performance_history_5yr.json)
- Analysis Section (from text_summaries.py output)
- Voting Rationales (from board_questions.py output)
- CEO Remuneration framework (from remuneration assessment)
- News search results (from news_prefetch.py)

Output: {workspace}/reports/{TICKER}_{YEAR}_VI_Report.docx
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List

from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
import docx

from logging_utils import write_supporting_json, write_supporting_text, TechnicalLogger
from llm_config import report_rendering_models

load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

REPORT_RENDERING_MODEL, REPORT_RENDERING_FALLBACK_MODEL = report_rendering_models()

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
REFERENCE_DIR = BASE_DIR / "data" / "_reference"
WORD_TEMPLATES_DIR = REFERENCE_DIR / "word_templates"

DEFAULT_TEMPLATE = WORD_TEMPLATES_DIR / "VI_Report_Blank.docx"


def _load_prompt(filename: str) -> str:
    """Load a prompt file from the prompts directory."""
    prompt_path = PROMPTS_DIR / filename
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"Failed to load prompt {filename}: {e}")


def _call_llm_report(
    client: OpenAI,
    system_prompt: str,
    user_payload: str,
    logger=None,
    call_name: str = "report_rendering",
    max_retries: int = 2,
) -> dict:
    """Call LLM for report rendering with JSON validation retries."""
    import time
    from datetime import datetime
    
    call_start = time.time()
    primary_model = REPORT_RENDERING_MODEL
    fallback_model = REPORT_RENDERING_FALLBACK_MODEL
    
    if logger:
        logger.info(f"[{call_name}] Requesting report generation via {primary_model}")
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload},
    ]
    
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=primary_model,
                messages=messages,
                temperature=0.1,
                max_tokens=12000,
            )
            content = response.choices[0].message.content
            
            # Try to parse JSON
            try:
                result = json.loads(content)
                if logger:
                    logger.info(f"[{call_name}] Success on attempt {attempt + 1}")
                return result
            except json.JSONDecodeError as e:
                if logger:
                    logger.warning(f"[{call_name}] JSON parse failed on attempt {attempt + 1}: {e}")
                if attempt < max_retries:
                    # Add correction message and retry
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "The previous response was not valid JSON. Please return only valid JSON matching the specified format."})
                    continue
                else:
                    raise
                    
        except Exception as e:
            if logger:
                logger.warning(f"[{call_name}] Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries:
                # Try fallback model
                if logger:
                    logger.info(f"[{call_name}] Falling back to {fallback_model}")
                try:
                    response = client.chat.completions.create(
                        model=fallback_model,
                        messages=messages,
                        temperature=0.1,
                        max_tokens=12000,
                    )
                    content = response.choices[0].message.content
                    result = json.loads(content)
                    if logger:
                        logger.info(f"[{call_name}] Fallback success")
                    return result
                except Exception as fallback_e:
                    if logger:
                        logger.error(f"[{call_name}] Fallback also failed: {fallback_e}")
                    raise RuntimeError(f"Both primary and fallback models failed: {e}; {fallback_e}")
            else:
                time.sleep(2 ** attempt)
    
    raise RuntimeError(f"All LLM attempts failed for {call_name}")


def _extract_company_name_and_details(workspace_dir: Path) -> Dict[str, Any]:
    """Extract company name and AGM details from NoM JSON."""
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
                    agm = nom_data.get("AGM_Details", {})
                    return {
                        "company_name": agm.get("company_name", ""),
                        "agm_date": agm.get("agm_date", ""),
                        "agm_time": agm.get("agm_time", ""),
                        "agm_location": agm.get("agm_location", ""),
                        "registry": agm.get("registry", ""),
                        "meeting_type": agm.get("agm_meetingtype", agm.get("meeting_type", "")),
                        "online_platform": agm.get("online_agm_platform", ""),
                        "resolutions": nom_data.get("Voting_Resolutions", []),
                    }
                except (json.JSONDecodeError, OSError):
                    continue
    return {"company_name": "", "resolutions": []}


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


def _load_remuneration_markdown(workspace_dir: Path, ticker: str, year: int) -> str:
    """Load remuneration assessment markdown."""
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


def _load_analysis_section(workspace_dir: Path, ticker: str, year: int) -> str:
    """Load the Analysis Section markdown from Stage 8."""
    reports_dir = workspace_dir / "reports"
    md_files = list(reports_dir.glob(f"{ticker}_{year}_Analysis_Section.md"))
    if not md_files:
        md_files = list(reports_dir.glob("*Analysis_Section.md"))
    if md_files:
        try:
            return md_files[0].read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _load_board_questions(workspace_dir: Path, ticker: str, year: int) -> str:
    """Load the Board Questions markdown from Stage 9."""
    reports_dir = workspace_dir / "reports"
    md_files = list(reports_dir.glob(f"{ticker}_{year}_Board_Questions.md"))
    if not md_files:
        md_files = list(reports_dir.glob("*Board_Questions.md"))
    if md_files:
        try:
            return md_files[0].read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _load_news_results(workspace_dir: Path) -> str:
    """Load news search results text."""
    reports_dir = workspace_dir / "reports"
    txt_files = list(reports_dir.glob("*News_Search_Results.txt"))
    if txt_files:
        try:
            return txt_files[0].read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _load_director_assessment(workspace_dir: Path, ticker: str, year: int) -> str:
    """Load the Director Assessment text from Stage 6."""
    # Try supporting_data first (newer format)
    stage6_dir = workspace_dir / "supporting_data" / "stage6_director_election"
    if stage6_dir.exists():
        txt_files = list(stage6_dir.glob("*_raw_llm.txt"))
        if txt_files:
            try:
                return txt_files[0].read_text(encoding="utf-8")
            except OSError:
                pass
    # Fallback: check reports dir for any director assessment
    reports_dir = workspace_dir / "reports"
    md_files = list(reports_dir.glob("*Director*Assessment*.md"))
    if not md_files:
        md_files = list(reports_dir.glob("*Director*Assessment*.txt"))
    if md_files:
        try:
            return md_files[0].read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _load_other_resolutions(workspace_dir: Path) -> Dict[str, Any]:
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


def _simple_markdown_to_docx(doc: docx.Document, markdown_text: str, max_chars: int = 50000) -> None:
    """Convert simple Markdown to docx paragraphs (headings, lists, bold, paragraphs)."""
    text = markdown_text[:max_chars]
    lines = text.split('\n')

    for line in lines:
        stripped = line.strip()
        if not stripped:
            doc.add_paragraph("")  # Empty paragraph for spacing
            continue

        # Headings
        if stripped.startswith("## "):
            p = doc.add_paragraph(stripped[3:], style="Heading 2")
        elif stripped.startswith("### "):
            p = doc.add_paragraph(stripped[4:], style="Heading 3")
        elif stripped.startswith("# "):
            p = doc.add_paragraph(stripped[2:], style="Heading 1")
        # List items
        elif stripped.startswith("- ") or stripped.startswith("* "):
            p = doc.add_paragraph(stripped[2:], style="List Bullet")
        elif re.match(r'^\d+\. ', stripped):
            p = doc.add_paragraph(stripped, style="List Number")
        else:
            p = doc.add_paragraph(stripped)


def _markdown_content_to_paragraphs(markdown_text: str) -> list:
    """Convert markdown text to a list of (text, style) tuples.
    
    Used for inserting paragraphs at specific positions in the document.
    """
    paragraphs = []
    lines = markdown_text.split('\n')
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            paragraphs.append(("", None))
            continue
        
        if stripped.startswith("## "):
            paragraphs.append((stripped[3:], "Heading 2"))
        elif stripped.startswith("### "):
            paragraphs.append((stripped[4:], "Heading 3"))
        elif stripped.startswith("# "):
            paragraphs.append((stripped[2:], "Heading 1"))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            paragraphs.append((stripped[2:], "List Bullet"))
        elif re.match(r'^\d+\. ', stripped):
            paragraphs.append((stripped, "List Number"))
        else:
            paragraphs.append((stripped, None))
    
    return paragraphs


def _populate_agm_table(doc: docx.Document, details: Dict[str, Any]) -> None:
    """Populate the AGM/summary table (Table 0) with company metadata."""
    if not doc.tables:
        return

    table = doc.tables[0]
    field_map = {
        "Company": details.get("company_name", ""),
        "Location": details.get("agm_location", ""),
        "Date/Time": f"{details.get('agm_date', '')} {details.get('agm_time', '')}".strip(),
        "Registry": details.get("registry", ""),
        "Meeting Type": details.get("meeting_type", ""),
        "Pre AGM": "",
        "Monitor": "AI Generated",
    }

    # Table 0 has 7 rows, each with [label, value] columns
    for row_idx, row in enumerate(table.rows):
        if row_idx < len(table.rows) and len(row.cells) >= 2:
            label_cell = row.cells[0].text.strip()
            if label_cell in field_map:
                row.cells[1].text = field_map[label_cell]


def _parse_remuneration_results(rem_markdown: str) -> Dict[str, Dict[str, Any]]:
    """Parse remuneration assessment markdown to extract resolution-level results.
    
    Looks for rows with 'F' (Fail) or 'U' (Uncertain) results and extracts
    the rule number and resolution reference.
    """
    results = {}
    lines = rem_markdown.split('\n')
    
    for line in lines:
        # Match table rows with rule codes and results
        # Format: | REM-M01 | F | High | ...
        match = re.match(r'\|\s*([\w\-0-9]+)\s*\|\s*([PFUN]/|F|P|U|N/A)\s*\|\s*([\w]+)\s*\|', line)
        if match:
            rule_code = match.group(1).strip()
            result = match.group(2).strip()
            confidence = match.group(3).strip()
            results[rule_code] = {
                "result": result,
                "confidence": confidence,
                "source_line": line[:200]
            }
    
    return results


def _parse_other_resolutions_results(other_res_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Parse other resolutions assessment to extract resolution-level results."""
    results = {}
    
    in_scope = other_res_data.get("in_scope_resolutions", [])
    for res in in_scope:
        res_num = res.get("resolution_number", "")
        results[res_num] = {
            "ai_result": res.get("ai_result", ""),
            "rules_applied": res.get("rules_applied", []),
            "confidence": res.get("confidence", ""),
            "evidence": res.get("evidence_comparison_action", ""),
        }
    
    return results


def _populate_voting_table(doc: docx.Document, resolutions: List[Dict], 
                            other_res_data: Dict[str, Any], rem_markdown: str,
                            llm_voting_rationales: List[Dict] = None) -> None:
    """Populate the resolution voting table (Table 1) with ASA vote recommendations.
    
    If llm_voting_rationales is provided, it is the SINGLE SOURCE OF TRUTH for all votes.
    Otherwise, falls back to legacy heuristic (for backward compatibility).
    
    Fallback default is resolution-specific FOR (derives from the director assessment
    for non-remuneration, non-director-election resolutions).
    """
    if len(doc.tables) < 2:
        return

    table = doc.tables[1]

    # Parse remuneration results for Fail/Uncertain (fallback)
    rem_results = _parse_remuneration_results(rem_markdown)
    
    # Parse other resolutions results (fallback)
    other_results = _parse_other_resolutions_results(other_res_data)

    # Build LLM vote lookup if provided (authoritative source)
    llm_vote_map = {}
    if llm_voting_rationales:
        for rat in llm_voting_rationales:
            res_num = rat.get("resolution_number", "")
            vote = rat.get("asa_vote", "")
            if res_num and vote:
                llm_vote_map[res_num] = vote

    # Build voting rows from NoM JSON (only voting items)
    voting_res = [r for r in resolutions if r.get("is_voting_item")]

    for row_idx, res in enumerate(voting_res):
        res_num = res.get("res_num", "") or res.get("res_number", "")
        res_title = res.get("res_title", "")
        res_text = f"{res_title} {res.get('res_description', '')}".lower()

        # LLM vote is authoritative if available
        if res_num in llm_vote_map:
            asa_vote = llm_vote_map[res_num]
        elif res_num in other_results:
            other_data = other_results[res_num]
            ai_result = other_data.get("ai_result", "")
            if ai_result == "F":
                asa_vote = "AGAINST"
            elif ai_result == "U":
                asa_vote = "AGAINST*"
            elif ai_result == "P":
                asa_vote = "FOR"
            else:
                asa_vote = "FOR"
        elif "remuneration" in res_text:
            asa_vote = "AGAINST" if any(r["result"] in ("F", "U") for r in rem_results.values()) else "FOR"
        else:
            asa_vote = "FOR"

        # Write to table
        data_idx = row_idx + 1
        if data_idx < len(table.rows):
            table.cell(data_idx, 0).text = res_num
            table.cell(data_idx, 1).text = res_title
            table.cell(data_idx, 2).text = asa_vote
        else:
            new_row = table.add_row()
            new_row.cells[0].text = res_num
            new_row.cells[1].text = res_title
            new_row.cells[2].text = asa_vote


def _populate_financial_table(doc: docx.Document, perf_data: Dict[str, Any], year: int) -> None:
    """Populate the financial history table (Table 2)."""
    if len(doc.tables) < 3:
        return

    table = doc.tables[2]

    years = perf_data.get("years", {})
    ceo_pay = perf_data.get("ceo_realised_pay", {})

    # Sort years descending (most recent first)
    sorted_years = sorted(years.keys(), key=lambda x: int(x), reverse=True)

    # Fill year headers (columns 1-5)
    for col_idx, fy in enumerate(sorted_years[:5], start=1):
        if col_idx < len(table.columns):
            table.cell(0, col_idx).text = f"FY{fy}"

    # Map row labels to data keys
    row_labels = {
        "NPAT ($m)": "npat",
        "Underlying NPAT ($m)": "underlying_npat",
        "Share Price ($)": "share_price",
        "Dividend (cents)": "dividend",
        "Simple TSR (%)": "simple_tsr",
        "EPS (cents)": "eps",
    }

    for row_idx, (label, key) in enumerate(row_labels.items(), start=1):
        for col_idx, fy in enumerate(sorted_years[:5], start=1):
            if col_idx >= len(table.columns) or row_idx >= len(table.rows):
                continue
            fy_data = years.get(fy, {})
            val = fy_data.get(key)
            if val is not None and isinstance(val, (int, float)):
                table.cell(row_idx, col_idx).text = f"{val}"
            else:
                table.cell(row_idx, col_idx).text = "—"

    # CEO Realised Rem row (last row)
    if len(table.rows) > 7:
        for col_idx, fy in enumerate(sorted_years[:5], start=1):
            if col_idx >= len(table.columns):
                continue
            pay_data = ceo_pay.get(fy, {})
            # The compiled JSON stores `value_m`; `value_aud_m` was an old key
            # that Stage 4 no longer writes. Fall back so the row is not left
            # as em-dashes when the payload only has value_m.
            val_aud = pay_data.get("value_aud_m")
            if val_aud is None:
                val_aud = pay_data.get("value_m")
            val_src = pay_data.get("value_m")
            currency = pay_data.get("currency", "AUD")
            fx_rate = pay_data.get("fx_rate")
            if val_aud is not None:
                if currency != "AUD" and val_aud != val_src:
                    # Show both source currency and AUD
                    table.cell(7, col_idx).text = f"${val_aud:.2f}m AUD (${val_src:.2f}m {currency} @ {fx_rate or 'N/A'})"
                else:
                    table.cell(7, col_idx).text = f"${val_aud:.2f}m"
            else:
                table.cell(7, col_idx).text = "\u2014"


def _parse_money_value(text: str) -> str:
    """Parse a money value from text and convert to millions (e.g., 2.185 or 2,500,000 -> 2.5).
    
    Handles formats like:
    - $2.185m -> "2.185"
    - $2,500,000 -> "2.5"
    - $2500000 -> "2.5"
    """
    # Try format with 'm' suffix first (e.g., $2.185m)
    match = re.search(r'\$\s*([\d.]+)\s*m', text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Try format with commas (e.g., $2,500,000)
    match = re.search(r'\$\s*([\d,]+)', text)
    if match:
        raw = match.group(1).replace(',', '')
        value = float(raw) / 1_000_000
        return f"{value:.3f}"
    
    # Try plain number after $ (e.g., $2500000)
    match = re.search(r'\$\s*([\d]+)', text)
    if match:
        value = float(match.group(1)) / 1_000_000
        return f"{value:.3f}"
    
    return "\u2014"


def _load_ar_framework(workspace_dir, ticker: str, year: int):
    """Extract the CEO framework figures from the Annual Report's own
    "Awarded Remuneration" table (MQG ARR p.144), which is authoritative:
    the model's Section 4 prose varies run to run and has produced wrong
    figures (MQG FY2026 "target STI approx. $9.0m" vs the AR's $21.0m).

    Returns (fixed, sti_cash, sti_equity, lti) in $millions, or None when the
    AR table cannot be located or its internal arithmetic does not reconcile.
    """
    try:
        from ar_file_lookup import find_ar_file
        from ar_text_cache import get_ar_pages
        from logging_utils import TechnicalLogger

        ar_path = find_ar_file(workspace_dir, year, fallback=False,
                               logger=TechnicalLogger(workspace_dir / "reports" / "technical_log.md"))
        if not ar_path:
            return None
        ar_path = ar_path.path if hasattr(ar_path, "path") else ar_path
        pages = get_ar_pages(ticker, year, ar_path)
    except Exception:
        return None

    # Column order in the "Awarded Remuneration" data row:
    #   Fixed | Total profit share | Cash | Retained equity | PSU | Total
    # i.e. fixed=nums[0], sti_total=nums[1], sti_cash=nums[2],
    #      sti_equity=nums[3], lti=nums[4], total=nums[5]
    NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    in_director_section = False
    for page in pages:
        text = page.get("text") or ""
        for line in text.splitlines():
            if "Executive Voting Director" in line or "Executive KMP awarded remuneration" in line:
                in_director_section = True
                continue
            if not in_director_section:
                continue
            nums = re.findall(NUM, line)
            if len(nums) >= 6:
                vals = [float(n.replace(",", "")) for n in nums[:6]]
                fixed, sti_total, sti_cash, sti_equity, lti, total = vals
                # Reconcile against the row's own totals before trusting it.
                if (abs(sti_total - (sti_cash + sti_equity)) <= 0.02 * sti_total
                        and abs(total - (fixed + sti_total + lti)) <= 0.02 * total):
                    return (fixed / 1_000_000.0, sti_cash / 1_000_000.0,
                            sti_equity / 1_000_000.0, lti / 1_000_000.0)
                # Not the CEO row (e.g. another Executive KMP) - keep scanning.
    return None


def _populate_ceo_remuneration_table(doc: docx.Document, rem_markdown: str, workspace_dir=None,
                                    ticker=None, year=None) -> None:
    """Populate Table 3 (CEO Remuneration Framework) from the remuneration assessment.
    
    Primary source is the Annual Report's own "Awarded Remuneration" table
    (see _load_ar_framework); the model's Section 4 prose is the fallback.
    """
    if len(doc.tables) < 4:
        return
    
    table = doc.tables[3]
    
    lines = rem_markdown.split('\n')

    # Primary source: the AR's own "Awarded Remuneration" table. The model's
    # Section 4 prose is unreliable across runs (MQG FY2026 "target STI
    # approx. $9.0m" vs the AR's $21.0m), so the AR governs when present.
    ar_fixed = ar_sti_cash = ar_sti_equity = ar_lti = None
    if workspace_dir is not None and ticker and year:
        ar_vals = _load_ar_framework(workspace_dir, ticker, year)
        if ar_vals:
            ar_fixed, ar_sti_cash, ar_sti_equity, ar_lti = ar_vals

    # Parsed amounts in $millions (None = not found).
    fixed = None
    sti_cash = None          # STI cash tranche (30%)
    sti_total = None         # total profit share (cash + retained equity)
    sti_max_total = None     # maximum profit share before risk adjustment
    lti = None

    def _amount(text: str):
        """Return a $-amount as a float in $millions, or None."""
        m = re.search(r'\$\s*([\d.,]+)(?:\s*m)?', text, re.IGNORECASE)
        if not m:
            return None
        val = float(m.group(1).replace(',', ''))
        # A trailing 'm' means the figure is already in $millions.
        return val if re.search(r'\$\s*[\d.,]+\s*m', text, re.IGNORECASE) else val / 1_000_000.0

    for line in lines:
        if 'Target and maximum framework' not in line and not ('Fixed pay' in line and 'STI' in line and 'LTI' in line):
            continue
        # Classify each dollar amount by the phrase that precedes it, so the
        # parser tolerates each issuer's wording instead of a single template.
        for amt in re.finditer(r'\$\s*([\d.,]+)(?:\s*m)?', line):
            val = _amount(amt.group(0))
            if val is None:
                continue
            ctx = line[max(0, amt.start() - 70): amt.start()].lower()
            if fixed is None and 'fixed' in ctx:
                fixed = val
            elif ('maximum' in ctx or 'maximum' in line.lower()[amt.start() - 70: amt.start()] or 'max' in ctx) and 'profit share' in ctx:
                if sti_max_total is None:
                    sti_max_total = val
            elif 'profit share' in ctx:
                if sti_total is None:
                    sti_total = val
            elif 'sti' in ctx and ('target' in ctx or 'cash' in ctx):
                if sti_cash is None:
                    sti_cash = val
            elif 'lti' in ctx:
                if lti is None:
                    lti = val

    # Retained-equity portion = total profit share - cash tranche (ARR p.145
    # reports "Total available" and "Retained allocation" as the two legs).
    sti_equity = None
    if sti_cash is not None and sti_total is not None and sti_total >= sti_cash:
        sti_equity = sti_total - sti_cash

    # Max opportunities: PSUs are granted at face value with no leverage, so
    # LTI max == LTI target. Fixed pay does not vary. STI max is split by the
    # same cash/equity ratio as the target.
    fixed_max = fixed
    lti_max = lti
    sti_cash_max = None
    sti_equity_max = None
    if sti_max_total is not None and sti_total and sti_cash is not None:
        sti_cash_max = sti_max_total * (sti_cash / sti_total)
        sti_equity_max = sti_max_total * (sti_equity / sti_total)

    targets = [fixed, sti_cash, sti_equity, lti]
    maxima = [fixed_max, sti_cash_max, sti_equity_max, lti_max]

    # The AR governs: override anything the prose parser found with the
    # authoritative table values (None keeps the prose value).
    if ar_fixed is not None:
        fixed = fixed_max = ar_fixed
    if ar_sti_cash is not None:
        sti_cash = sti_cash_max = ar_sti_cash
    if ar_sti_equity is not None:
        sti_equity = sti_equity_max = ar_sti_equity
    if ar_lti is not None:
        lti = lti_max = ar_lti
    targets = [fixed, sti_cash, sti_equity, lti]
    maxima = [fixed_max, sti_cash_max, sti_equity_max, lti_max]

    def _fmt(v):
        return f"{v:.2f}" if v is not None else "\u2014"

    def _pct(part, whole):
        if part is None or whole in (None, 0):
            return "\u2014"
        return f"{part / whole * 100:.2f}%"

    total_target = sum(v for v in targets if v is not None) or None
    total_max = sum(v for v in maxima if v is not None) or None

    # Rows 1-4: Fixed, STI-Cash, STI-Equity, LTI.
    for i in range(4):
        if i + 1 < len(table.rows):
            table.cell(i + 1, 1).text = _fmt(targets[i])
            table.cell(i + 1, 2).text = _pct(targets[i], total_target)
            table.cell(i + 1, 3).text = _fmt(maxima[i])
            table.cell(i + 1, 4).text = _pct(maxima[i], total_max)

    # Row 5: Total.
    if len(table.rows) > 5:
        table.cell(5, 1).text = _fmt(total_target)
        table.cell(5, 2).text = _pct(total_target, total_target)
        table.cell(5, 3).text = _fmt(total_max)
        table.cell(5, 4).text = _pct(total_max, total_max)


def _replace_placeholders(doc: docx.Document, replacements: Dict[str, str]) -> None:
    """Replace placeholder text in all paragraphs and tables."""
    placeholder_map = {
        "<company name>": replacements.get("company_name", ""),
        "<year>": replacements.get("year", ""),
    }

    # Add year-1, year-2, etc. based on the current year
    year = replacements.get("year_int", datetime.now().year)
    for i in range(1, 5):
        placeholder_map[f"<year-{i}>"] = str(year - i)

    # Replace in paragraphs
    for para in doc.paragraphs:
        if para.text:
            for placeholder, value in placeholder_map.items():
                para.text = para.text.replace(placeholder, value)

    # Replace in tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    for placeholder, value in placeholder_map.items():
                        cell.text = cell.text.replace(placeholder, value)


def _insert_paragraphs_after(doc: docx.Document, after_para, paragraphs_data: list) -> None:
    """Insert paragraphs after a specific paragraph using lxml manipulation.
    
    Args:
        doc: The docx Document
        after_para: The docx Paragraph after which to insert
        paragraphs_data: List of (text, style) tuples to insert
    """
    parent = after_para._p.getparent()
    para_idx = list(parent).index(after_para._p)
    
    for offset, (text, style) in enumerate(paragraphs_data, start=1):
        new_para = doc.add_paragraph(text, style=style)
        new_para_p = new_para._p
        # Remove from end of document and insert at correct position
        parent.remove(new_para_p)
        # Insert after the after_para, in order
        insert_before_idx = para_idx + offset
        if insert_before_idx < len(list(parent)):
            target = list(parent)[insert_before_idx]
            parent.insert(insert_before_idx, new_para_p)
        else:
            parent.append(new_para_p)


def _insert_markdown_into_sections(doc: docx.Document, markdown_text: str) -> None:
    """Insert markdown content into the document's placeholder paragraphs.
    
    The template has placeholder paragraphs in Section 3 (Analysis Section):
    - "Summary of Issues" (para 8)
    - "Matters Considered" (para 11)
    - "Governance" (para 12)
    - "ESG" (para 15)
    - "Board" (para 18)
    
    The markdown has matching section headings. We extract each section's
    content and insert it after the matching placeholder paragraph.
    """
    # Parse markdown into sections
    sections = {}
    current_section = None
    current_lines = []
    
    for line in markdown_text.split('\n'):
        stripped = line.strip()
        if stripped.startswith('## '):
            if current_section:
                sections[current_section] = '\n'.join(current_lines)
            current_section = stripped[3:]  # Remove "## "
            current_lines = []
        elif stripped.startswith('### '):
            if current_section:
                sections[current_section] = '\n'.join(current_lines)
            current_section = stripped[4:]  # Remove "### "
            current_lines = []
        elif current_section:
            current_lines.append(line)
    
    if current_section:
        sections[current_section] = '\n'.join(current_lines)
    
    # Map markdown sections to template placeholder paragraphs
    section_placeholders = {
        "Summary of Issues": "Summary of Issues",
        "Governance": "Governance",
        "ESG": "ESG",
        "Board": "Board",
    }
    
# For each section, find the placeholder in the document and insert content after it
    # Process in true bottom-up order: Board -> ESG -> Governance -> Matters Considered -> Summary of Issues
    # This ensures insertions don't shift earlier headings
    section_order = [
        ("board", "Board", "Heading 5"),
        ("esg", "ESG", "Heading 5"),
        ("governance", "Governance", "Heading 5"),
        ("matters_considered", "Matters Considered", "Heading 4"),
        ("summary_of_issues", "Summary of Issues", "Heading 4"),
    ]
    
    for section_name, placeholder_text, expected_style in section_order:
        if section_name not in sections:
            continue
        
        # Find the placeholder paragraph by style AND exact normalized text match
        for i, para in enumerate(doc.paragraphs):
            if para.style and para.style.name == expected_style:
                para_text = para.text.strip()
                norm_para = ' '.join(para_text.split())
                norm_placeholder = ' '.join(placeholder_text.split())
                # Exact normalized match only
                if norm_para == norm_placeholder:
                    paragraphs = _markdown_content_to_paragraphs(sections[section_name])
                    _insert_paragraphs_after(doc, para, paragraphs)
                    break


def _insert_into_voting_rationales(doc: docx.Document, board_questions: str) -> None:
    """Insert board questions content after the '5. Voting Rationales:' section."""
    for i, para in enumerate(doc.paragraphs):
        if "Voting Rationales" in para.text:
            paragraphs = _markdown_content_to_paragraphs(board_questions)
            _insert_paragraphs_after(doc, para, paragraphs)
            return
    # Fallback: append at end
    doc.add_paragraph("5. Voting Rationales", style="Heading 3")
    _simple_markdown_to_docx(doc, board_questions)


def _build_llm_payload(
    workspace_dir: Path,
    ticker: str,
    year: int,
    nom_details: Dict[str, Any],
    perf_data: Dict[str, Any],
    rem_markdown: str,
    analysis_section: str,
    board_questions: str,
    director_assessment: str,
    news_results: str,
    other_res_data: Dict[str, Any],
) -> str:
    """Build the complete payload for the LLM report generation call."""
    
    # Extract resolutions for voting rationales
    resolutions = nom_details.get("resolutions", [])
    voting_res = [r for r in resolutions if r.get("is_voting_item")]
    
    # Parse other resolutions results
    other_results = _parse_other_resolutions_results(other_res_data)
    
    # Parse remuneration results
    rem_results = _parse_remuneration_results(rem_markdown)
    
    payload = {
        "company_name": nom_details.get("company_name", ticker),
        "ticker": ticker,
        "year": year,
        "agm_details": {
            "date": nom_details.get("agm_date", ""),
            "time": nom_details.get("agm_time", ""),
            "location": nom_details.get("agm_location", ""),
            "meeting_type": nom_details.get("meeting_type", ""),
        },
        "resolutions": [
            {
                "number": r.get("res_number", ""),
                "title": r.get("res_title", ""),
                "description": r.get("res_description", ""),
                "is_voting_item": r.get("is_voting_item", False),
                "is_remuneration": "remuneration" in f"{r.get('res_title', '')} {r.get('res_description', '')}".lower(),
            }
            for r in resolutions
        ],
        "voting_resolutions": [
            {
                "number": r.get("res_number", ""),
                "title": r.get("res_title", ""),
                "description": r.get("res_description", ""),
            }
            for r in voting_res
        ],
        "performance_history": perf_data,
        "remuneration_assessment": rem_markdown[:30000] if rem_markdown else "",
        "analysis_section": analysis_section[:30000] if analysis_section else "",
        "board_questions": board_questions[:30000] if board_questions else "",
        "director_assessment": director_assessment[:30000] if director_assessment else "",
        "news_results": news_results[:15000] if news_results else "",
        "other_resolutions": other_res_data,
        "remuneration_results": rem_results,
        "other_resolutions_results": other_results,
    }
    
    return json.dumps(payload, indent=2)


def _insert_llm_content(
    doc: docx.Document,
    llm_result: dict,
) -> None:
    """Insert LLM-generated content into the document template."""
    
    # Insert Analysis Section content
    analysis = llm_result.get("analysis_section", {})
    if analysis:
        _insert_analysis_sections(doc, analysis)
    
    # Insert Voting Rationales
    voting_rationales = llm_result.get("voting_rationales", [])
    if voting_rationales:
        _insert_voting_rationales(doc, voting_rationales)


def _insert_analysis_sections(doc: docx.Document, analysis: dict) -> None:
    """Replace the entire Analysis Section block with LLM-generated content.
    
    The template has these paragraphs in the Analysis Section:
    - "3. Analysis Section" (Heading 3)
    - "Summary of Issues" (Heading 4)
    - "Matters Considered" (Heading 4)
    - "Governance" (Heading 5)
    - "ESG" (Heading 5)
    - "Board" (Heading 5)
    - Then "4. Financial History Table:" (Heading 3)
    
    We'll find "3. Analysis Section" and replace everything after it
    up to "4. Financial History Table:" with our generated content.
    """
    # Build complete markdown for Analysis Section
    md_parts = []
    
    section_order = [
        ("summary_of_issues", "## Summary of Issues"),
        ("matters_considered", "## Matters Considered"),
        ("governance", "## Governance"),
        ("esg", "## ESG"),
        ("board", "## Board"),
    ]
    
    for section_key, heading in section_order:
        content = analysis.get(section_key, "")
        if content:
            md_parts.append(heading)
            md_parts.append(content)
            md_parts.append("")
    
    if not md_parts:
        return
    
    combined_md = "\n".join(md_parts)
    
    # Find "3. Analysis Section" heading
    analysis_start_idx = None
    analysis_end_idx = None
    
    for i, para in enumerate(doc.paragraphs):
        if "3. Analysis Section" in para.text:
            analysis_start_idx = i
        elif analysis_start_idx is not None and "4. Financial History Table:" in para.text:
            analysis_end_idx = i
            break
    
    if analysis_start_idx is None:
        # Fallback: append at end
        doc.add_paragraph("3. Analysis Section", style="Heading 3")
        _simple_markdown_to_docx(doc, combined_md)
        return
    
    if analysis_end_idx is None:
        analysis_end_idx = len(doc.paragraphs)
    
    # Remove all paragraphs between start and end (exclusive of start, inclusive of end)
    paragraphs_to_remove = list(range(analysis_start_idx + 1, analysis_end_idx))
    
    # Convert markdown to paragraphs
    new_paragraphs = _markdown_content_to_paragraphs(combined_md)
    
    # Insert new paragraphs after the "3. Analysis Section" heading
    parent = doc.paragraphs[analysis_start_idx]._p.getparent()
    start_idx = list(parent).index(doc.paragraphs[analysis_start_idx]._p)
    
    # Remove old paragraphs
    for idx in reversed(paragraphs_to_remove):
        if idx < len(doc.paragraphs):
            p = doc.paragraphs[idx]
            parent.remove(p._p)
    
    # Insert new paragraphs
    for offset, (text, style) in enumerate(new_paragraphs, start=1):
        new_para = doc.add_paragraph(text, style=style)
        new_para_p = new_para._p
        parent.remove(new_para_p)
        insert_idx = start_idx + offset
        if insert_idx < len(list(parent)):
            target = list(parent)[insert_idx]
            parent.insert(insert_idx, new_para_p)
        else:
            parent.append(new_para_p)


def _insert_voting_rationales(doc: docx.Document, rationales: list) -> None:
    """Insert LLM-generated voting rationales after the '5. Voting Rationales:' section."""
    
    # Build combined markdown
    md_parts = []
    for rat in rationales:
        res_num = rat.get("resolution_number", "")
        title = rat.get("title", "")
        vote = rat.get("asa_vote", "")
        rationale = rat.get("rationale", "")
        
        md_parts.append(f"### Resolution {res_num}: {title}")
        md_parts.append(f"**ASA Vote: {vote}**")
        md_parts.append(rationale)
        md_parts.append("")
    
    combined_md = "\n".join(md_parts)
    
    # Find the Voting Rationales placeholder
    for i, para in enumerate(doc.paragraphs):
        if "Voting Rationales" in para.text:
            paragraphs = _markdown_content_to_paragraphs(combined_md)
            _insert_paragraphs_after(doc, para, paragraphs)
            return
    
    # Fallback: append at end
    doc.add_paragraph("5. Voting Rationales", style="Heading 2")
    _simple_markdown_to_docx(doc, combined_md)


def run_report_rendering(
    workspace_dir: Path,
    supporting_data_dir: Path,
    logger=None,
) -> bool:
    """
    Run Stage 10 - Word Report Rendering with LLM-generated narrative.

    Loads all upstream outputs, calls LLM to generate ASA-style narrative,
    fills in the Word template, and saves the final VI Report .docx.

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

    logger.stage_start(f"Stage 10: Word Report Rendering for {ticker} FY{year}")

    # Load template
    template_path = DEFAULT_TEMPLATE
    if not template_path.exists():
        templates = list(WORD_TEMPLATES_DIR.glob("*.docx"))
        if templates:
            template_path = templates[0]
            logger.info(f"Using template: {template_path.name}")
        else:
            logger.error(f"No Word template found in {WORD_TEMPLATES_DIR}")
            print(f"[Stage 10] No Word template found in {WORD_TEMPLATES_DIR}")
            return False

    logger.info(f"Loading template: {template_path}")
    doc = docx.Document(str(template_path))

    # Load all upstream data
    nom_details = _extract_company_name_and_details(workspace_dir)
    company_name = nom_details.get("company_name", ticker)
    logger.info(f"Company name: {company_name}")

    perf_data = _load_performance_data(workspace_dir)
    rem_markdown = _load_remuneration_markdown(workspace_dir, ticker, year)
    analysis_section = _load_analysis_section(workspace_dir, ticker, year)
    board_questions = _load_board_questions(workspace_dir, ticker, year)
    director_assessment = _load_director_assessment(workspace_dir, ticker, year)
    news_results = _load_news_results(workspace_dir)
    other_res_data = _load_other_resolutions(workspace_dir)

    # Populate CEO remuneration framework table (Table 3) - structured data
    if rem_markdown:
        _populate_ceo_remuneration_table(doc, rem_markdown,
                                        workspace_dir=workspace_dir,
                                        ticker=ticker, year=year)

    # Replace simple placeholders
    _replace_placeholders(doc, {
        "company_name": company_name,
        "year": str(year),
        "year_int": year,
    })

    # Populate AGM summary table
    _populate_agm_table(doc, nom_details)

    # Populate financial history table
    _populate_financial_table(doc, perf_data, year)

    # --- LLM-Generated Narrative Sections ---
    logger.info("Generating LLM narrative for VI Report...")
    
    try:
        # Initialize OpenAI client
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=API_KEY,
        )
        
        # Load system prompt
        system_prompt = _load_prompt("report_rendering.txt")
        
        # Build payload with all upstream data
        user_payload = _build_llm_payload(
            workspace_dir=workspace_dir,
            ticker=ticker,
            year=year,
            nom_details=nom_details,
            perf_data=perf_data,
            rem_markdown=rem_markdown,
            analysis_section=analysis_section,
            board_questions=board_questions,
            director_assessment=director_assessment,
            news_results=news_results,
            other_res_data=other_res_data,
        )
        
        # Call LLM
        llm_result = _call_llm_report(
            client=client,
            system_prompt=system_prompt,
            user_payload=user_payload,
            logger=logger,
            call_name="report_rendering",
        )
        
        # Save raw LLM output for debugging
        write_supporting_json(supporting_data_dir, "report_llm_output.json", llm_result)
        logger.info("LLM narrative generation successful")
        
        # Populate voting table using LLM votes as authoritative source
        llm_rationales = llm_result.get("voting_rationales", []) if llm_result else []
        _populate_voting_table(doc, nom_details.get("resolutions", []), other_res_data, rem_markdown, llm_voting_rationales=llm_rationales)
        
        # Insert LLM-generated content into document
        _insert_llm_content(doc, llm_result)
        
    except Exception as e:
        logger.error(f"LLM narrative generation failed: {e}")
        logger.warning("Falling back to legacy markdown insertion...")
        
        # Fallback: legacy markdown insertion
        if analysis_section:
            _insert_markdown_into_sections(doc, analysis_section)
        if board_questions:
            _insert_into_voting_rationales(doc, board_questions)
        if news_results:
            doc.add_paragraph("News Search Results", style="Heading 2")
            doc.add_paragraph(news_results, style="List Bullet")

        # Fallback vote table using legacy heuristic
        _populate_voting_table(doc, nom_details.get("resolutions", []), other_res_data, rem_markdown)

    # Add News Search Results (if not already handled by LLM)
    if news_results:
        # Check if LLM already covered news in analysis
        try:
            llm_output = (supporting_data_dir / "report_llm_output.json").read_text()
            if "news" not in llm_output.lower() and "News Search Results" not in str(llm_output):
                doc.add_paragraph("News Search Results", style="Heading 2")
                doc.add_paragraph(news_results, style="List Bullet")
        except:
            pass

    # Save the final document
    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / f"{ticker}_{year}_VI_Report.docx"

    doc.save(str(output_path))
    logger.info(f"Saved report to {output_path}")

    # Save metadata
    metadata = {
        "ticker": ticker,
        "year": year,
        "company_name": company_name,
        "template_used": str(template_path.name),
        "output_file": str(output_path.name),
        "sections_included": {
            "analysis_section": True,
            "board_questions": True,
            "news_results": bool(news_results),
            "performance_table": bool(perf_data),
            "remuneration_assessment": bool(rem_markdown),
            "llm_generated": True,
        },
        "rendered_at": datetime.now().isoformat(),
    }
    write_supporting_json(supporting_data_dir, "report_rendering_metadata.json", metadata)

    # Also save a text copy for easy review
    text_summary = f"""Report rendered: {output_path.name}

Template: {template_path.name}

Sections included:
- Analysis Section: Yes (LLM-generated)
- Board Questions: Yes (LLM-generated)
- News Search Results: {'Yes' if news_results else 'No'}
- Performance Table: {'Yes' if perf_data else 'No'}
- Remuneration Assessment: {'Yes' if rem_markdown else 'No'}

Data sources used:
- NoM JSON: {company_name}
- Performance history: {'Found' if perf_data else 'Not found'}
- Remuneration: {'Found' if rem_markdown else 'Not found'}
"""
    write_supporting_text(supporting_data_dir, "report_rendering_summary.txt", text_summary)

    logger.stage_end(
        f"Stage 10: Word Report Rendering for {ticker} FY{year}",
        True,
        summary=f"Saved {output_path.name}"
    )

    return True


# Alias for orchestrator compatibility
run_pipeline = run_report_rendering


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        workspace = Path(sys.argv[1])
        supporting = workspace / "supporting_data" / "stage10_report_rendering"
        result = run_report_rendering(workspace, supporting)
        print(f"Result: {result}")
        sys.exit(0 if result else 1)
    else:
        print("Usage: python src/report_rendering.py <workspace_dir>")
        sys.exit(1)
