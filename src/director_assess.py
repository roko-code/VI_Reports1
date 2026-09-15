import os
import json
import re
import shutil
from pathlib import Path
import pypdf
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from logging_utils import write_supporting_text

# Environment setup
load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

# Model Configurations (Clean OpenRouter IDs, stage-specific env vars)
DIRECTOR_ASSESS_PRIMARY_MODEL = os.getenv("DIRECTOR_ASSESS_PRIMARY_MODEL", "stealth/ox-alpha")
DIRECTOR_ASSESS_FALLBACK_MODEL = os.getenv("DIRECTOR_ASSESS_FALLBACK_MODEL", "google/gemini-2.0-flash-exp:free")

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"

# Domain-specific directory structure (shared reference docs at project level)
ASA_GUIDELINES_DIR = DATA_DIR / "asa_guidelines"


def load_prompt(filename: str) -> str:
    """Loads system prompt from disk."""
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file missing at: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


def extract_pdf_text(pdf_path: Path) -> str:
    """Extracts raw text content from a PDF file."""
    reader = pypdf.PdfReader(pdf_path)
    text = ""
    for idx, page in enumerate(reader.pages):
        page_text = page.extract_text()
        if page_text:
            text += f"\n--- Page {idx + 1} ---\n" + page_text
    return text


def load_asa_guidelines() -> str:
    """Loads text from the PDF stored in data/asa_guidelines/."""
    pdf_files = list(ASA_GUIDELINES_DIR.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No ASA Guideline PDF found in {ASA_GUIDELINES_DIR.relative_to(BASE_DIR)}")
    print(f"Loading ASA Guideline: {pdf_files[0].name}")
    return extract_pdf_text(pdf_files[0])


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


def find_matching_ar(json_stem: str, ar_dir: Path) -> Path:
    """Matches NoM JSON (e.g. BHP_2026_NOM) with raw AR PDF (e.g. BHP_2026_AR.pdf)."""
    prefix = json_stem.replace("_NOM", "").replace("_nom", "")

    ar_files = list(ar_dir.glob("*.pdf"))
    for ar_file in ar_files:
        if prefix.lower() in ar_file.stem.lower():
            return ar_file

    return None


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


def run_director_assessment(json_path: Path, ar_path: Path, asa_text: str, client: OpenAI, system_prompt: str, reports_dir: Path, supporting_data_dir: Path = None, logger=None, archive_dir: Path = None):
    """Executes LLM evaluation using JSON, Annual Report, and ASA Guidelines."""
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

    ar_text = extract_pdf_text(ar_path)

    user_payload = (
        f"=== INPUT 1: NOTICE OF MEETING EXTRACTED JSON ===\n"
        f"{json.dumps(nom_data, indent=2)}\n\n"
        f"=== INPUT 2: ASA DIRECTOR ELECTION GUIDELINES ===\n"
        f"{asa_text}\n\n"
        f"=== INPUT 3: ANNUAL REPORT TEXT ===\n"
        f"{ar_text}"
    )

    if logger:
        logger.info(f"Requesting LLM assessment for {json_path.name} via {DIRECTOR_ASSESS_PRIMARY_MODEL}")

    result_md = call_llm_with_fallback(client, system_prompt, user_payload, logger)

    # Save raw LLM response to supporting data
    if supporting_data_dir:
        write_supporting_text(supporting_data_dir, f"{json_path.stem}_raw_llm.txt", result_md)
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
    print(f" Report saved -> {report_path.relative_to(BASE_DIR)}")
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
        print(f"No pending NoM JSON files found in {workspace_nom_json_dir.relative_to(BASE_DIR)}")
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

    success_count = 0
    for json_path in json_files:
        ar_path = find_matching_ar(json_path.stem, annual_reports_dir)
        if not ar_path:
            print(f" Warning: No matching Annual Report found in {annual_reports_dir.relative_to(BASE_DIR)} for {json_path.name}. Skipping.")
            if logger:
                logger.warning(f"No matching AR found for {json_path.name}")
            continue

        try:
            result = run_director_assessment(json_path, ar_path, asa_text, client, system_prompt, workspace_reports_dir, supporting_data_dir, logger, workspace_nom_json_archive_dir)
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