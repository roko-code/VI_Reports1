import os
import json
import re
import shutil
from pathlib import Path
import pypdf
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

# Environment setup
load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

# Model Configurations (Clean OpenRouter IDs)
PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "stealth/ox-alpha")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "google/gemini-2.0-flash-exp:free")

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"

# Domain-specific directory structure
ASA_GUIDELINES_DIR = DATA_DIR / "asa_guidelines"
NOM_JSON_DIR = DATA_DIR / "extracted_json" / "notices"
NOM_JSON_ARCHIVE_DIR = DATA_DIR / "extracted_json" / "notices_archived"
AR_RAW_DIR = DATA_DIR / "annual_reports" / "raw"
AR_PROCESSED_DIR = DATA_DIR / "annual_reports" / "processed"
REPORTS_DIR = DATA_DIR / "reports"
FAILED_DIR = DATA_DIR / "failed"

# Directory initialization
for folder in [
    PROMPTS_DIR, ASA_GUIDELINES_DIR, NOM_JSON_DIR, NOM_JSON_ARCHIVE_DIR,
    AR_RAW_DIR, AR_PROCESSED_DIR, REPORTS_DIR, FAILED_DIR
]:
    folder.mkdir(parents=True, exist_ok=True)


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


def find_matching_ar(json_stem: str) -> Path:
    """Matches NoM JSON (e.g. BHP_2026_NOM) with raw AR PDF (e.g. BHP_2026_AR.pdf)."""
    prefix = json_stem.replace("_NOM", "").replace("_nom", "")
    
    ar_files = list(AR_RAW_DIR.glob("*.pdf"))
    for ar_file in ar_files:
        if prefix.lower() in ar_file.stem.lower():
            return ar_file
            
    return None


def call_llm_with_fallback(client: OpenAI, system_prompt: str, user_payload: str) -> str:
    """
    Executes API requests using OpenRouter's native web search server tool:
    1. PRIMARY_MODEL + openrouter:web_search tool
    2. FALLBACK_MODEL + openrouter:web_search tool
    3. FALLBACK_MODEL (Standard generation without web search tool)
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload}
    ]
    web_search_tool = [{"type": "openrouter:web_search"}]

    # --- Attempt 1: Primary Model + Native Web Search Tool ---
    try:
        print(f" Requesting assessment via primary model ({PRIMARY_MODEL}) with Web Search...")
        response = client.chat.completions.create(
            model=PRIMARY_MODEL,
            messages=messages,
            tools=web_search_tool
        )
        return response.choices[0].message.content.strip()
    except Exception as err1:
        print(f" Primary model with web search tool failed: {err1}")

    # --- Attempt 2: Fallback Model + Native Web Search Tool ---
    try:
        print(f" Switching to fallback model ({FALLBACK_MODEL}) with Web Search...")
        response = client.chat.completions.create(
            model=FALLBACK_MODEL,
            messages=messages,
            tools=web_search_tool
        )
        return response.choices[0].message.content.strip()
    except Exception as err2:
        print(f" Fallback model with web search tool failed: {err2}")

    # --- Attempt 3: Standard Fallback (No Web Search) ---
    print(f" Executing standard assessment via {FALLBACK_MODEL} (No Web Search)...")
    response = client.chat.completions.create(
        model=FALLBACK_MODEL,
        messages=messages
    )
    return response.choices[0].message.content.strip()


def run_director_assessment(json_path: Path, ar_path: Path, asa_text: str, client: OpenAI, system_prompt: str):
    """Executes LLM evaluation using JSON, Annual Report, and ASA Guidelines."""
    print(f"\nProcessing Director Assessment for: {json_path.name} + {ar_path.name}")
    
    with open(json_path, "r", encoding="utf-8") as f:
        nom_data = json.load(f)

    if not has_director_resolutions(nom_data):
        print(" Skipping: No director election/re-election resolutions detected in JSON.")
        shutil.move(str(json_path), str(NOM_JSON_ARCHIVE_DIR / json_path.name))
        return

    ar_text = extract_pdf_text(ar_path)

    user_payload = (
        f"=== INPUT 1: NOTICE OF MEETING EXTRACTED JSON ===\n"
        f"{json.dumps(nom_data, indent=2)}\n\n"
        f"=== INPUT 2: ASA DIRECTOR ELECTION GUIDELINES ===\n"
        f"{asa_text}\n\n"
        f"=== INPUT 3: ANNUAL REPORT TEXT ===\n"
        f"{ar_text}"
    )

    result_md = call_llm_with_fallback(client, system_prompt, user_payload)

    # Clean up markdown code blocks if wrapped by the model
    if result_md.startswith("```markdown"):
        result_md = result_md[11:-3].strip()
    elif result_md.startswith("```"):
        result_md = result_md[3:-3].strip()

    # Save Markdown report output
    report_filename = f"{json_path.stem}_Director_Evaluation.md"
    report_path = REPORTS_DIR / report_filename
    report_path.write_text(result_md, encoding="utf-8")
    print(f" Report saved -> {report_path.relative_to(BASE_DIR)}")

    # Move processed inputs to archive directories
    shutil.move(str(json_path), str(NOM_JSON_ARCHIVE_DIR / json_path.name))
    shutil.move(str(ar_path), str(AR_PROCESSED_DIR / ar_path.name))
    print(f" Archived inputs to processed directories.")


def run_pipeline():
    system_prompt = load_prompt("director_assess.txt")
    asa_text = load_asa_guidelines()

    json_files = list(NOM_JSON_DIR.glob("*.json"))
    if not json_files:
        print(f"No pending NoM JSON files found in {NOM_JSON_DIR.relative_to(BASE_DIR)}")
        return

    print(f"Found {len(json_files)} JSON file(s) for evaluation.")

    # Initialize OpenAI client with OpenRouter Base URL
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY
    )

    for json_path in json_files:
        ar_path = find_matching_ar(json_path.stem)
        if not ar_path:
            print(f" Warning: No matching Annual Report found in {AR_RAW_DIR.relative_to(BASE_DIR)} for {json_path.name}. Skipping.")
            continue

        try:
            run_director_assessment(json_path, ar_path, asa_text, client, system_prompt)
        except Exception as e:
            print(f" Error executing assessment for {json_path.name}: {e}")
            shutil.move(str(json_path), str(FAILED_DIR / json_path.name))


if __name__ == "__main__":
    run_pipeline()