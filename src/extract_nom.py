import os
import json
import shutil
from pathlib import Path
import pypdf
from dotenv import load_dotenv, find_dotenv
from openrouter import OpenRouter

# Load environment variables
load_dotenv(find_dotenv())

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY not found in .env file.")

# Set up project base directory and paths
BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"

# Domain-specific directory structure
RAW_NOM_DIR = DATA_DIR / "notices" / "raw"
PROCESSED_NOM_DIR = DATA_DIR / "notices" / "processed"
JSON_OUTPUT_DIR = DATA_DIR / "extracted_json" / "notices"
FAILED_DIR = DATA_DIR / "failed"

# Automatically create directories if they do not exist
for folder in [PROMPTS_DIR, RAW_NOM_DIR, PROCESSED_NOM_DIR, JSON_OUTPUT_DIR, FAILED_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


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


def process_single_nom(pdf_path: Path, client: OpenRouter, system_prompt: str):
    """Processes a single Notice of Meeting PDF with OpenRouter."""
    print(f"Processing: {pdf_path.name}")
    raw_text = extract_text_from_pdf(pdf_path)

    response = client.chat.send(
        model="stealth/ox-alpha",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Extract data from this Notice of Meeting text:\n\n{raw_text}"}
        ]
    )

    content = response.choices[0].message.content.strip()

    # Strip markdown code blocks if the model includes them
    if content.startswith("```json"):
        content = content[7:-3].strip()
    elif content.startswith("```"):
        content = content[3:-3].strip()

    # Validate JSON payload structure
    json_data = json.loads(content)

    # Save output to data/extracted_json/notices/
    json_filename = pdf_path.stem + ".json"
    json_out_path = JSON_OUTPUT_DIR / json_filename
    with open(json_out_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    print(f" Saved JSON -> {json_out_path.relative_to(BASE_DIR)}")


def run_batch_pipeline():
    """Scans input folder and processes all pending NoM PDFs."""
    system_prompt = load_prompt("extract_nom.txt")
    pdf_files = list(RAW_NOM_DIR.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in {RAW_NOM_DIR.relative_to(BASE_DIR)}")
        return

    print(f"Found {len(pdf_files)} Notice of Meeting PDF(s) to process.\n")

    with OpenRouter(api_key=API_KEY) as client:
        for pdf_path in pdf_files:
            try:
                process_single_nom(pdf_path, client, system_prompt)
                
                # Archive original PDF upon successful processing
                target_path = PROCESSED_NOM_DIR / pdf_path.name
                shutil.move(str(pdf_path), str(target_path))
                print(f" Archived PDF -> {target_path.relative_to(BASE_DIR)}\n")
            
            except Exception as e:
                print(f" Error processing {pdf_path.name}: {e}")
                failed_path = FAILED_DIR / pdf_path.name
                shutil.move(str(pdf_path), str(failed_path))
                print(f" Moved to failed -> {failed_path.relative_to(BASE_DIR)}\n")


if __name__ == "__main__":
    run_batch_pipeline()