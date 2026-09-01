import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
COMPANIES_DIR = DATA_DIR / "companies"
ASA_GUIDELINES_DIR = DATA_DIR / "asa_guidelines"


# Ensure root data directories exist
COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
ASA_GUIDELINES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_STATE_SCHEMA = {
    "metadata": {
        "ticker": "",
        "year": "",
        "created_at": "",
        "last_updated": ""
    },
    "pipeline_status": {
        "nom_extracted": False,
        "performance_table_built": False,
        "remuneration_extracted": False,
        "director_assessments_complete": False,
        "board_questions_generated": False,
        "audit_passed": False,
        "report_rendered": False
    },
    "extracted_data": {
        "agm_details": {},
        "voting_resolutions": [],
        "performance_history_5yr": {},
        "ceo_remuneration": {},
        "director_scorecards": [],
        "board_questions": []
    }
}

class CompanyPipeline:
    def __init__(self, ticker: str, year: str):
        self.ticker = ticker.upper()
        self.year = str(year)
        self.company_id = f"{self.ticker}_{self.year}"
        
        # Directory Structure: data/companies/SUN_2026/
        self.workspace_dir = COMPANIES_DIR / self.company_id
        self.reports_dir = self.workspace_dir / "reports"
        self.state_file = self.workspace_dir / "state.json"
        
        self._setup_dirs()
        self.state = self._load_or_init_state()

    def _setup_dirs(self):
        """Creates complete per-company directory structure."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        
        # Define all per-company subfolders
        self.notices_dir = self.workspace_dir / "notices"
        self.annual_reports_dir = self.workspace_dir / "annual_reports"
        self.extracted_json_dir = self.workspace_dir / "extracted_json"
        self.reports_dir = self.workspace_dir / "reports"
        self.failed_dir = self.workspace_dir / "failed"
        
        # Create subfolders if they don't exist
        for folder in [self.notices_dir, self.annual_reports_dir, self.extracted_json_dir, self.reports_dir, self.failed_dir]:
            folder.mkdir(parents=True, exist_ok=True)

    def _load_or_init_state(self) -> dict:
        """Loads existing state.json or creates a new one from schema."""
        if self.state_file.exists():
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        
        now = datetime.now().isoformat()
        new_state = DEFAULT_STATE_SCHEMA.copy()
        new_state["metadata"] = {
            "ticker": self.ticker,
            "year": self.year,
            "created_at": now,
            "last_updated": now
        }
        self.save_state(new_state)
        return new_state

    def save_state(self, new_state_data: dict = None):
        """Persists current in-memory state back to state.json."""
        if new_state_data:
            self.state = new_state_data
            
        self.state["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def update_status(self, step_key: str, is_complete: bool):
        """Helper to mark individual pipeline stages complete/incomplete."""
        if step_key in self.state["pipeline_status"]:
            self.state["pipeline_status"][step_key] = is_complete
            self.save_state()

    def run_pipeline(self, force_reprocess: bool = False):
        """Main execution sequence checking status before calling child modules."""
        print(f"\n==========================================")
        print(f" Starting Pipeline for [{self.company_id}]")
        print(f"==========================================")
        
        # Stage 1: NoM Metadata & Resolution Extraction
        if not self.state["pipeline_status"]["nom_extracted"] or force_reprocess:
            print("[Stage 1] Extracting NoM & Resolutions...")
            # CALL extract_nom module here
            # self.state["extracted_data"]["voting_resolutions"] = extracted_res
            # self.update_status("nom_extracted", True)
        else:
            print("[Stage 1] NoM Extraction already COMPLETE. Skipping.")

        # Stage 2: 5-Year Financial & Performance History Table
        if not self.state["pipeline_status"]["performance_table_built"] or force_reprocess:
            print("[Stage 2] Building 5-Year Performance History...")
            # CALL 5-year performance history module here
        else:
            print("[Stage 2] Performance Table already COMPLETE. Skipping.")

        # Stage 3: CEO Remuneration Breakdown
        if not self.state["pipeline_status"]["remuneration_extracted"] or force_reprocess:
            print("[Stage 3] Extracting CEO Remuneration Breakdown...")
            # CALL remuneration module here
        else:
            print("[Stage 3] Remuneration Breakdown already COMPLETE. Skipping.")

        # Stage 4: Sequential Resolution Dispatcher (Director Assess, Rem Vote, etc.)
        if not self.state["pipeline_status"]["director_assessments_complete"] or force_reprocess:
            print("[Stage 4] Dispatching Voting Resolution Assessments...")
            # CALL resolution dispatcher loop here
        else:
            print("[Stage 4] Resolution Assessments already COMPLETE. Skipping.")

        # Stage 5: Board Questions Generator
        if not self.state["pipeline_status"]["board_questions_generated"] or force_reprocess:
            print("[Stage 5] Synthesizing Questions for the Board...")
            # CALL board questions synthesizer here
        else:
            print("[Stage 5] Board Questions already COMPLETE. Skipping.")

        # Stage 6: Word Report Rendering
        if not self.state["pipeline_status"]["report_rendered"] or force_reprocess:
            print("[Stage 6] Rendering final docx report...")
            # CALL docxtpl rendering engine here
        else:
            print("[Stage 6] Report Rendering already COMPLETE. Skipping.")

        print(f"\nPipeline state saved to: {self.state_file.relative_to(BASE_DIR)}")

from find_nom import scan_today_announcements

def run_daily_batch():
    """Scans ASX for today's NoMs and processes each company pipeline."""
    print("[Orchestrator] Starting Daily ASX NoM Scan...")
    downloaded_noms = scan_today_announcements()
    
    if not downloaded_noms:
        print("[Orchestrator] No new NoM PDFs downloaded today.")
        return

    for nom in downloaded_noms:
        ticker = nom["ticker"]
        year = nom["year"]
        print(f"\n[Orchestrator] Spawning pipeline for {ticker} ({year})...")
        pipeline = CompanyPipeline(ticker=ticker, year=year)
        pipeline.run_pipeline()

if __name__ == "__main__":
    import sys
    
    # Mode A: If ticker & year are passed via command line -> run single target
    # Usage: python src/orchestrator.py SUN 2026
    if len(sys.argv) >= 3:
        target_ticker = sys.argv[1]
        target_year = sys.argv[2]
        pipeline = CompanyPipeline(ticker=target_ticker, year=target_year)
        pipeline.run_pipeline()
        
    # Mode B: Default daily batch run
    # Usage: python src/orchestrator.py
    else:
        run_daily_batch()

if __name__ == "__main__":
    # Test harness initialization
    test_ticker = "SUN"
    test_year = "2026"
    
    pipeline = CompanyPipeline(ticker=test_ticker, year=test_year)
    pipeline.run_pipeline()