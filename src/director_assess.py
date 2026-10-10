import os
import json
import re
import shutil
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from logging_utils import write_supporting_text
from ar_text_cache import get_ar_pages
from ar_retrieval import STAGE6_SPECS, build_context, write_manifest
from citation_check import check_citations, format_problems
from news_search_results import load_news_block
from ar_file_lookup import find_ar_file as _find_ar_file_lookup, parse_year_from_stem
from llm_config import director_assess_models

# Environment setup
load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

# Model Configurations (supports global override via LLM_PRIMARY_OVERRIDE / LLM_FALLBACK_OVERRIDE)
DIRECTOR_ASSESS_PRIMARY_MODEL, DIRECTOR_ASSESS_FALLBACK_MODEL = director_assess_models()

# Character budget for retrieved Annual Report sections. Verified in
# src/retrieval_selftest.py against all 14 cached reports.
AR_BUDGET_CHARS = int(os.getenv("AR_BUDGET_CHARS", "200000"))

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"


def load_prompt(filename: str) -> str:
    """Loads system prompt from disk."""
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file missing at: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


def load_asa_guidelines() -> str:
    """All ASA guideline PDFs in data/asa_guidelines/, concatenated.

    Folder-driven: adding/replacing a PDF in the folder changes what is sent
    with no prompt edit. Delegates to the shared loader so every assessment
    stage reads the same set of guidelines.
    """
    from input_sources import load_asa_guidelines as _load_all
    return _load_all()


def has_director_resolutions(nom_data: dict) -> bool:
    """Python pre-filter: checks if JSON contains director election resolutions."""
    resolutions = nom_data.get("Voting_Resolutions", [])
    keywords = re.compile(r"\b(director|election|re-election|elect|re-elect)\b", re.IGNORECASE)
    
    for res in resolutions:
        title = res.get("res_title", "")
        desc = res.get("res_description", "")
        if keywords.search(title) or keywords.search(desc):
            return True
    return False


def find_matching_ar(json_stem: str, ar_dir: Path, logger=None) -> Optional[Path]:
    """Match a NoM JSON stem (e.g. ``BHP_2026_NOM``) to its AR PDF.

    Falls back to the most recent available AR when the target-year AR is
    absent. Delegates to ``ar_file_lookup.find_ar_file`` so the year rule is
    identical to Stages 5/8/9. The previous two-sort-rule implementation
    agreed with this one for standard ``{TICKER}_{YEAR}_AR.pdf`` names but
    diverged on edge cases.
    """
    year = parse_year_from_stem(json_stem)
    if year is None:
        if logger:
            logger.warning(f"Could not parse year from NoM stem '{json_stem}'")
        return None
    lookup = _find_ar_file_lookup(ar_dir.parent, year, fallback=True, logger=logger, ar_dir_name=ar_dir.name)
    return lookup.path if lookup else None


def call_llm_with_fallback(client: OpenAI, system_prompt: str, user_payload: str, logger=None) -> str:
    """
    Executes API requests using OpenRouter's native web search server tool:
    1. DIRECTOR_ASSESS_PRIMARY_MODEL + openrouter:web_search tool
    2. DIRECTOR_ASSESS_FALLBACK_MODEL + openrouter:web_search tool
    3. DIRECTOR_ASSESS_FALLBACK_MODEL (Standard generation without web search tool)
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload}
    ]
    web_search_tool = [{"type": "openrouter:web_search"}]

    # --- Attempt 1: Primary Model + Native Web Search Tool ---
    try:
        print(f" Requesting assessment via primary model ({DIRECTOR_ASSESS_PRIMARY_MODEL}) with Web Search...")
        if logger:
            logger.info(f"Requesting assessment via primary model ({DIRECTOR_ASSESS_PRIMARY_MODEL}) with Web Search...")
        response = client.chat.completions.create(
            model=DIRECTOR_ASSESS_PRIMARY_MODEL,
            messages=messages,
            tools=web_search_tool
        )
        return response.choices[0].message.content.strip()
    except Exception as err1:
        print(f" Primary model with web search tool failed: {err1}")
        if logger:
            logger.warning(f"Primary model with web search tool failed: {err1}")

    # --- Attempt 2: Fallback Model + Native Web Search Tool ---
    try:
        print(f" Switching to fallback model ({DIRECTOR_ASSESS_FALLBACK_MODEL}) with Web Search...")
        if logger:
            logger.info(f"Switching to fallback model ({DIRECTOR_ASSESS_FALLBACK_MODEL}) with Web Search...")
        response = client.chat.completions.create(
            model=DIRECTOR_ASSESS_FALLBACK_MODEL,
            messages=messages,
            tools=web_search_tool
        )
        return response.choices[0].message.content.strip()
    except Exception as err2:
        print(f" Fallback model with web search tool failed: {err2}")
        if logger:
            logger.warning(f"Fallback model with web search tool failed: {err2}")

    # --- Attempt 3: Standard Fallback (No Web Search) ---
    print(f" Executing standard assessment via {DIRECTOR_ASSESS_FALLBACK_MODEL} (No Web Search)...")
    if logger:
        logger.info(f"Executing standard assessment via {DIRECTOR_ASSESS_FALLBACK_MODEL} (No Web Search)...")
    response = client.chat.completions.create(
        model=DIRECTOR_ASSESS_FALLBACK_MODEL,
        messages=messages
    )
    return response.choices[0].message.content.strip()


def run_director_assessment(json_path: Path, ar_path: Path, asa_text: str, client: OpenAI, system_prompt: str, reports_dir: Path, supporting_data_dir: Path = None, logger=None, archive_dir: Path = None, news_block: str = "", cgs_text: str = None, prior_vi_text: str = None, prior_meeting_text: str = None):
    """Executes LLM evaluation using JSON, Annual Report, and ASA Guidelines.

    news_block: pre-fetched news search results (Stage 7.5), supplied as
    supplementary external context. Empty string omits the section.
    cgs_text / prior_vi_text / prior_meeting_text: folder-driven standalone
    CGS and prior ASA VI / AGM report; None omits the section.
    """
    if logger:
        logger.info(f"Processing Director Assessment for: {json_path.name} + {ar_path.name}")
    print(f"\nProcessing Director Assessment for: {json_path.name} + {ar_path.name}")

    with open(json_path, "r", encoding="utf-8") as f:
        nom_data = json.load(f)

    if not has_director_resolutions(nom_data):
        print(" Skipping: No director election/re-election resolutions detected in JSON.")
        if logger:
            logger.info(f"Skipping {json_path.name}: no director resolutions detected")
        return False

    # Retrieve the director, governance and conflicts-of-interest sections
    # rather than passing the whole Annual Report. The full text of a large
    # report exceeds the model context window outright, so this call was
    # previously either rejected or silently truncated by the provider.
    ar_match = re.search(r"(\d{4})", ar_path.stem)
    ar_year = int(ar_match.group(1)) if ar_match else 0
    ticker = ar_path.stem.split("_")[0].upper()

    ar_pages = get_ar_pages(ticker, ar_year, ar_path, logger=logger)
    ar_retrieval = build_context(
        ar_pages,
        STAGE6_SPECS,
        budget_chars=AR_BUDGET_CHARS,
        logger=logger,
    )
    ar_text = ar_retrieval.text
    if supporting_data_dir:
        write_manifest(
            ar_retrieval, supporting_data_dir, "stage6", ticker, ar_year, logger=logger
        )
    if logger:
        logger.info(
            f"AR retrieval: {len(ar_retrieval.included_pages)}/"
            f"{ar_retrieval.total_pages} pages, {ar_retrieval.chars_used:,} chars"
        )
        if ar_retrieval.missing_mandatory:
            if logger:
                logger.warning(
                    "Stage 6 could not locate mandatory sections: "
                    + ", ".join(ar_retrieval.missing_mandatory)
                    + ". Rules depending on these will be assessed as Uncertain."
                )

    user_payload = (
        f"{ar_retrieval.coverage_note()}\n\n"
        f"=== INPUT 1: NOTICE OF MEETING EXTRACTED JSON ===\n"
        f"{json.dumps(nom_data, indent=2)}\n\n"
        f"=== INPUT 2: ASA DIRECTOR ELECTION GUIDELINES ===\n"
        f"{asa_text}\n\n"
        f"=== INPUT 3: ANNUAL REPORT (retrieved sections) ===\n"
        f"{ar_text}"
    )

    # Pre-fetched news (Stage 7.5). Supplementary external context for
    # candidate due diligence; the prompt verifies company/candidate match
    # and treats items as unverified leads. Omitted entirely when absent so
    # the model is not shown an empty input.
    if news_block and news_block.strip():
        user_payload += (
            "\n\n=== INPUT 4: NEWS SEARCH RESULTS (pre-fetched, ~12 months) ===\n"
            f"{news_block}"
        )

    # Folder-driven supplementary sources (2026-10-10). Each omitted when absent.
    if cgs_text and cgs_text.strip():
        user_payload += (
            "\n\n=== INPUT 5: CORPORATE GOVERNANCE STATEMENT (standalone CGS) ===\n"
            f"{cgs_text}"
        )
    if prior_vi_text and prior_vi_text.strip():
        user_payload += (
            "\n\n=== INPUT 6: PRIOR ASA VOTING INTENTIONS REPORT ===\n"
            f"{prior_vi_text}"
        )
    if prior_meeting_text and prior_meeting_text.strip():
        user_payload += (
            "\n\n=== INPUT 7: PRIOR ASA AGM/EGM MEETING REPORT ===\n"
            f"{prior_meeting_text}"
        )

    if logger:
        logger.info(f"Requesting LLM assessment for {json_path.name} via {DIRECTOR_ASSESS_PRIMARY_MODEL}")

    result_md = call_llm_with_fallback(client, system_prompt, user_payload, logger)

    # Save raw LLM response to supporting data
    if supporting_data_dir:
        write_supporting_text(supporting_data_dir, f"{json_path.stem}_raw_llm.txt", result_md)
        # Citation integrity - Stage 6 has never validated its [p.N] citations.
# Logged, not enforced; see citation_check for the scope limit.
        try:
            rep = check_citations(result_md, ar_retrieval.included_pages)
            for problem in format_problems(rep):
                if logger:
                    logger.warning(f"Stage 6 citation check: {problem}")
                if logger:
                    logger.info(f"Stage 6 citation check: {rep.summary()}")
        except Exception as e:                  # never fail the stage on this
            if logger:
                logger.warning(f"Stage 6 citation check could not run: {e}")
        if logger:
            logger.info(f"Raw LLM response saved for {json_path.name}")

    # Clean up markdown code blocks if wrapped by the model
    if result_md.startswith("```markdown"):
        result_md = result_md[11:-3].strip()
    elif result_md.startswith("```"):
        result_md = result_md[3:-3].strip()

    # Save Markdown report output
    report_filename = f"{json_path.stem}_Director_Evaluation.md"
    report_path = reports_dir / report_filename
    report_path.write_text(result_md, encoding="utf-8")
    try:
        print(f" Report saved -> {report_path.relative_to(BASE_DIR)}")
    except ValueError:
        print(f" Report saved -> {report_path}")
    if logger:
        logger.info(f"Report saved: {report_path.name}")

    # Move processed inputs to archive directories
    if archive_dir:
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(json_path), str(archive_dir / json_path.name))
        print(f" Archived inputs to processed directories.")
        if logger:
            logger.info(f"Archived {json_path.name} to notices_archived")
    return True


def run_pipeline(workspace_dir: Path, supporting_data_dir: Path = None, logger=None) -> bool:
    system_prompt = load_prompt("director_assess.txt")
    asa_text = load_asa_guidelines()

    # Folder-driven supplementary sources (2026-10-10), loaded once and shared
    # by every per-candidate call.
    from input_sources import load_cgs_text, load_latest_meeting_text, load_latest_vi_text
    cgs_text = load_cgs_text(workspace_dir)
    prior_vi_text = load_latest_vi_text(workspace_dir)
    prior_meeting_text = load_latest_meeting_text(workspace_dir)

    extracted_json_dir = workspace_dir / "extracted_json"
    workspace_nom_json_dir = extracted_json_dir / "notices"
    workspace_nom_json_archive_dir = extracted_json_dir / "notices_archived"
    annual_reports_dir = workspace_dir / "annual_reports"
    workspace_reports_dir = workspace_dir / "reports"
    failed_dir = workspace_dir / "failed"

    for folder in [
        extracted_json_dir, workspace_nom_json_dir, workspace_nom_json_archive_dir,
        annual_reports_dir, workspace_reports_dir, failed_dir
    ]:
        folder.mkdir(parents=True, exist_ok=True)

    json_files = list(workspace_nom_json_dir.glob("*.json"))
    if not json_files:
        print(f"No pending NoM JSON files found in {workspace_nom_json_dir}")
        if logger:
            logger.warning("No pending NoM JSON files found")
        return False

    print(f"Found {len(json_files)} JSON file(s) for evaluation.")
    if logger:
        logger.info(f"Found {len(json_files)} JSON file(s) for evaluation")

    # Initialize OpenAI client with OpenRouter Base URL
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY
    )

    # Pre-fetched news (Stage 7.5), loaded once and shared by every
    # per-candidate assessment call in this run.
    news_block = load_news_block(workspace_dir)
    if logger:
        logger.info(f"Stage 6 news context: {'supplied' if news_block.strip() else 'not available'}")

    success_count = 0
    for json_path in json_files:
        ar_path = find_matching_ar(json_path.stem, annual_reports_dir)
        if not ar_path:
            print(f" Warning: No matching Annual Report found in {annual_reports_dir} for {json_path.name}. Skipping.")
            if logger:
                logger.warning(f"No matching AR found for {json_path.name}")
            continue

        try:
            result = run_director_assessment(json_path, ar_path, asa_text, client, system_prompt, workspace_reports_dir, supporting_data_dir, logger, workspace_nom_json_archive_dir, news_block=news_block, cgs_text=cgs_text, prior_vi_text=prior_vi_text, prior_meeting_text=prior_meeting_text)
            if result:
                success_count += 1
        except Exception as e:
            if logger:
                logger.error(f"Error executing assessment for {json_path.name}: {e}")
            print(f" Error executing assessment for {json_path.name}: {e}")
            shutil.move(str(json_path), str(failed_dir / json_path.name))

    if success_count == 0:
        print(" Stage 6 complete: 0 assessments completed.")
        if logger:
            logger.warning("Stage 6 complete: 0 assessments completed")
        return False

    print(f" Stage 6 complete: {success_count} assessment(s) completed.")
    if logger:
        logger.info(f"Stage 6 complete: {success_count} assessment(s) completed")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        run_pipeline(Path(sys.argv[1]))
    else:
        print("Usage: python src/director_assess.py <workspace_dir>")
        print("Example: python src/director_assess.py data/companies/BHP_2026")