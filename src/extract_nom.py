import os
import sys
import json
import shutil
from pathlib import Path
import pypdf
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from logging_utils import write_supporting_text

# Load environment variables
load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

# Model configuration (stage-specific env vars with fallback)
EXTRACT_NOM_MODEL = os.getenv("EXTRACT_NOM_MODEL", "stealth/ox-alpha")
EXTRACT_NOM_FALLBACK_MODEL = os.getenv("EXTRACT_NOM_FALLBACK_MODEL", "google/gemini-2.0-flash-exp:free")

# Set up project base directory and paths
BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"


def load_prompt(filename: str) -> str:
    """Reads prompt text from the prompts folder."""
    prompt_path = PROMPTS_DIR / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file missing at: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extracts raw text content from a PDF file."""
    reader = pypdf.PdfReader(pdf_path)
    text = ""
    for idx, page in enumerate(reader.pages):
        page_text = page.extract_text()
        if page_text:
            text += f"\n--- Page {idx + 1} ---\n" + page_text
    return text


def process_single_nom(pdf_path: Path, json_out_dir: Path, client: OpenAI, system_prompt: str, supporting_data_dir: Path = None, logger=None):
    """Processes a single Notice of Meeting PDF with OpenRouter."""
    if logger:
        logger.info(f"Processing PDF: {pdf_path.name}")
    print(f"Processing: {pdf_path.name}")
    raw_text = extract_text_from_pdf(pdf_path)

    # --- Attempt 1: Configured model ---
    try:
        print(f"  [LLM] Requesting extraction via model: {EXTRACT_NOM_MODEL}")
        if logger:
            logger.info(f"Primary model ({EXTRACT_NOM_MODEL}) requested for {pdf_path.name}")
        response = client.chat.completions.create(
            model=EXTRACT_NOM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Extract data from this Notice of Meeting text:\n\n{raw_text}"}
            ]
        )
        content = response.choices[0].message.content.strip()
    except Exception as err1:
        print(f"  [LLM] Primary model ({EXTRACT_NOM_MODEL}) failed: {err1}")
        if logger:
            logger.warning(f"Primary model ({EXTRACT_NOM_MODEL}) failed for {pdf_path.name}: {err1}")
        print(f"  [LLM] Falling back to model: {EXTRACT_NOM_FALLBACK_MODEL}")
        if logger:
            logger.info(f"Falling back to {EXTRACT_NOM_FALLBACK_MODEL} for {pdf_path.name}")
        try:
            response = client.chat.completions.create(
                model=EXTRACT_NOM_FALLBACK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Extract data from this Notice of Meeting text:\n\n{raw_text}"}
                ]
            )
            content = response.choices[0].message.content.strip()
        except Exception as err2:
            if logger:
                logger.error(f"Both primary and fallback failed for {pdf_path.name}: {err2}")
            raise RuntimeError(
                f"Both primary ({EXTRACT_NOM_MODEL}) and fallback ({EXTRACT_NOM_FALLBACK_MODEL}) failed. "
                f"Last error: {err2}"
            )

    # Save raw LLM response to supporting data
    if supporting_data_dir:
        write_supporting_text(supporting_data_dir, f"{pdf_path.stem}_raw_llm.txt", content)

    # Strip markdown code blocks if the model includes them
    if content.startswith("```json"):
        content = content[7:-3].strip()
    elif content.startswith("```"):
        content = content[3:-3].strip()

    # Validate JSON payload structure
    json_data = json.loads(content)

    # Save output to workspace extracted_json dir
    json_filename = pdf_path.stem + ".json"
    json_out_path = json_out_dir / json_filename
    with open(json_out_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    print(f" Saved JSON -> {json_out_path.relative_to(BASE_DIR)}")


def run_batch_pipeline(workspace_dir: Path, supporting_data_dir: Path = None, logger=None) -> bool:
    """Scans company workspace notices dir and processes all pending NoM PDFs.
    Returns True if at least one PDF was successfully processed, False otherwise.
    """
    system_prompt = load_prompt("extract_nom.txt")

    notices_dir = workspace_dir / "notices"
    notices_processed_dir = notices_dir / "processed"
    extracted_json_dir = workspace_dir / "extracted_json" / "notices"
    failed_dir = workspace_dir / "failed"

    for folder in [notices_dir, notices_processed_dir, extracted_json_dir, failed_dir]:
        folder.mkdir(parents=True, exist_ok=True)

    pdf_files = list(notices_dir.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in {notices_dir.relative_to(BASE_DIR)}")
        return False

    print(f"Found {len(pdf_files)} Notice of Meeting PDF(s) to process.\n")

    success_count = 0
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY
    )
    for pdf_path in pdf_files:
        try:
            process_single_nom(pdf_path, extracted_json_dir, client, system_prompt, supporting_data_dir, logger)

            # Archive original PDF upon successful processing
            target_path = notices_processed_dir / pdf_path.name
            shutil.move(str(pdf_path), str(target_path))
            print(f" Archived PDF -> {target_path.relative_to(BASE_DIR)}\n")
            success_count += 1

        except Exception as e:
            if logger:
                logger.error(f"Error processing {pdf_path.name}: {e}")
            print(f" Error processing {pdf_path.name}: {e}")
            failed_path = failed_dir / pdf_path.name
            shutil.move(str(pdf_path), str(failed_path))
            print(f" Moved to failed -> {failed_path.relative_to(BASE_DIR)}\n")

    if success_count == 0:
        print(" All PDFs failed. No JSON extracted.")
        if logger:
            logger.warning("All PDFs failed. No JSON extracted.")
        return False

    print(f" Stage 1 complete: {success_count} of {len(pdf_files)} PDF(s) successfully extracted.")
    if logger:
        logger.info(f"Stage 1 complete: {success_count} of {len(pdf_files)} PDF(s) successfully extracted.")
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        run_batch_pipeline(Path(sys.argv[1]))
    else:
        print("Usage: python src/extract_nom.py <workspace_dir>")
        print("Example: python src/extract_nom.py data/companies/BHP_2026")