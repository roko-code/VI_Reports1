import json
import os
import sys
from datetime import datetime
from pathlib import Path

from logging_utils import TechnicalLogger, write_supporting_json, write_supporting_text

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
        "last_updated": "",
        "legacy_codes": []
    },
    "pipeline_status": {
        "nom_extracted": False,
        "annual_reports_downloaded": False,
        "performance_table_built": False,
        "vi_reports_downloaded": False,
        "remuneration_assessed": False,
        "director_assessments_complete": False,
        "other_resolutions_assessed": False,
        "text_summaries_generated": False,
        "audit_passed": False,
        "board_questions_generated": False,
        "report_rendered": False
    },
    "extracted_data": {
        "agm_details": {},
        "voting_resolutions": [],
        "performance_history_5yr": {},
        "ceo_remuneration": {},
        "director_scorecards": [],
        "other_resolution_assessments": [],
        "text_summaries": {},
        "board_questions": []
    }
}

# Stage module imports (graceful degradation if a module is missing)
try:
    from extract_nom import run_batch_pipeline as stage_nom_extraction
except Exception:
    stage_nom_extraction = None

try:
    from fetch_annual_reports import fetch_annual_reports as stage_ar_download
except Exception:
    stage_ar_download = None

try:
    from fetch_past_vi_reports import fetch_vi_reports as stage_vi_reports
except Exception:
    stage_vi_reports = None

try:
    from performance_table import run_pipeline as stage_performance_table
except Exception:
    stage_performance_table = None

try:
    from director_assess import run_pipeline as stage_director_assessment
except Exception:
    stage_director_assessment = None

try:
    from remuneration_assess import run_pipeline as stage_remuneration_assessment
except Exception:
    stage_remuneration_assessment = None

try:
    from other_resolutions_assess import run_pipeline as stage_other_resolutions
except Exception:
    stage_other_resolutions = None

try:
    from text_summaries import run_pipeline as stage_text_summaries
except Exception:
    stage_text_summaries = None

try:
    from board_questions import run_pipeline as stage_board_questions
except Exception:
    stage_board_questions = None

try:
    from report_rendering import run_pipeline as stage_report_rendering
except Exception:
    stage_report_rendering = None


class CompanyPipeline:
    def __init__(self, ticker: str, year: str):
        self.ticker = ticker.upper()
        self.year = str(year)
        self.company_id = f"{self.ticker}_{self.year}"

        # Directory Structure: data/companies/SUN_2026/
        self.workspace_dir = COMPANIES_DIR / self.company_id
        self.reports_dir = self.workspace_dir / "reports"
        self.supporting_data_dir = self.workspace_dir / "supporting_data"
        self.state_file = self.workspace_dir / "state.json"
        self.technical_log_path = self.reports_dir / "technical_log.md"

        self._setup_dirs()
        self.logger = TechnicalLogger(self.technical_log_path)
        self.state = self._load_or_init_state()

    def _setup_dirs(self):
        """Creates complete per-company directory structure."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Define all per-company subfolders
        self.notices_dir = self.workspace_dir / "notices"
        self.annual_reports_dir = self.workspace_dir / "annual_reports"
        self.extracted_json_dir = self.workspace_dir / "extracted_json"
        self.reports_dir = self.workspace_dir / "reports"
        self.supporting_data_dir = self.workspace_dir / "supporting_data"
        self.failed_dir = self.workspace_dir / "failed"

        # Create subfolders if they don't exist
        for folder in [
            self.notices_dir, self.annual_reports_dir, self.extracted_json_dir,
            self.reports_dir, self.supporting_data_dir, self.failed_dir
        ]:
            folder.mkdir(parents=True, exist_ok=True)

    def _load_or_init_state(self) -> dict:
        """Loads existing state.json, migrates missing fields, and rejects stale success flags."""
        if self.state_file.exists():
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            status = state.setdefault("pipeline_status", {})
            migrated = False

            if "annual_reports_downloaded" not in status:
                # Existing workspaces predate this flag; infer it from the workspace.
                status["annual_reports_downloaded"] = any(self.annual_reports_dir.glob("*.pdf"))
                migrated = True
                self.logger.warning(
                    f"Added missing state flag annual_reports_downloaded={status['annual_reports_downloaded']}"
                )

            performance_path = self.extracted_json_dir / "performance_history_5yr.json"
            if status.get("performance_table_built"):
                performance_valid = False
                if performance_path.exists():
                    try:
                        with open(performance_path, "r", encoding="utf-8") as f:
                            performance_data = json.load(f)
                        performance_valid = bool(performance_data.get("years"))
                    except (json.JSONDecodeError, OSError):
                        performance_valid = False

                if not performance_valid:
                    status["performance_table_built"] = False
                    migrated = True
                    self.logger.warning(
                        "Reset stale performance_table_built flag because the performance output has no extracted years"
                    )

            if migrated:
                self.save_state(state)
            return state
        
        now = datetime.now().isoformat()
        new_state = DEFAULT_STATE_SCHEMA.copy()
        new_state["metadata"] = {
            "ticker": self.ticker,
            "year": self.year,
            "created_at": now,
            "last_updated": now,
            "legacy_codes": []
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
            if stage_nom_extraction:
                stage_dir = self.supporting_data_dir / "stage1_nom_extraction"
                self.logger.stage_start("Stage 1: NoM Extraction")
                success = stage_nom_extraction(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 1: NoM Extraction", success)
                if success:
                    self.update_status("nom_extracted", True)
                else:
                    print("[Stage 1] Extraction failed or incomplete. State not updated.")
            else:
                print("[Stage 1] extract_nom module not available. Skipping.")
        else:
            print("[Stage 1] NoM Extraction already COMPLETE. Skipping.")

        # Stage 2: Annual Reports Download
        if not self.state["pipeline_status"]["annual_reports_downloaded"] or force_reprocess:
            if stage_ar_download:
                stage_dir = self.supporting_data_dir / "stage2_ar_download"
                self.logger.stage_start("Stage 2: Annual Reports Download")
                success, all_codes = stage_ar_download(self.ticker, int(self.year), supporting_data_dir=stage_dir, logger=self.logger)
                if all_codes and len(all_codes) > 1:
                    self.state["metadata"]["legacy_codes"] = all_codes[1:]
                    self.logger.info(f"Legacy codes discovered: {all_codes[1:]}")
                self.logger.stage_end("Stage 2: Annual Reports Download", success)
                if success:
                    self.update_status("annual_reports_downloaded", True)
                else:
                    print("[Stage 2] AR download incomplete. State not updated.")
            else:
                print("[Stage 2] fetch_annual_reports module not available. Skipping.")
        else:
            print("[Stage 2] Annual Reports already COMPLETE. Skipping.")

        # Stage 3: VI Reports Download
        if not self.state["pipeline_status"]["vi_reports_downloaded"] or force_reprocess:
            if stage_vi_reports:
                stage_dir = self.supporting_data_dir / "stage3_vi_reports"
                self.logger.stage_start("Stage 3: VI Reports Download")
                legacy_codes = self.state.get("metadata", {}).get("legacy_codes", [])
                if legacy_codes:
                    self.logger.info(f"Using legacy codes for VI lookup: {legacy_codes}")
                success = stage_vi_reports(self.ticker, self.workspace_dir, target_year=int(self.year), legacy_codes=legacy_codes or None, supporting_data_dir=stage_dir, logger=self.logger)
                self.logger.stage_end("Stage 3: VI Reports Download", success)
                if success:
                    self.update_status("vi_reports_downloaded", True)
                else:
                    print("[Stage 3] VI Reports download incomplete. State not updated.")
            else:
                print("[Stage 3] fetch_past_vi_reports module not available. Skipping.")
        else:
            print("[Stage 3] VI Reports already COMPLETE. Skipping.")

        # Stage 4: Financial Performance Table
        # Check both state flag AND output file validity to prevent double-run
        performance_output = self.extracted_json_dir / "performance_history_5yr.json"
        performance_built = self.state["pipeline_status"]["performance_table_built"]
        performance_valid = False
        
        if performance_built and performance_output.exists():
            try:
                with open(performance_output, "r", encoding="utf-8") as f:
                    perf_data = json.load(f)
                # Valid if extraction_status is not "failed" and has some data
                status = perf_data.get("extraction_status", "unknown")
                years = perf_data.get("years", {})
                ceo_pay = perf_data.get("ceo_realised_pay", {})
                performance_valid = status != "failed" and (bool(years) or bool(ceo_pay))
            except (json.JSONDecodeError, OSError):
                performance_valid = False
        
        if not performance_valid or force_reprocess:
            if performance_built and not performance_valid:
                print("[Stage 4] Previous run produced invalid output — re-running.")
                self.logger.warning("Previous Stage 4 output invalid — re-running.")
                self.state["pipeline_status"]["performance_table_built"] = False
                self.save_state()
            
            if stage_performance_table:
                stage_dir = self.supporting_data_dir / "stage4_performance_table"
                self.logger.stage_start("Stage 4: Financial Performance Table")
                success = stage_performance_table(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 4: Financial Performance Table", success)
                if success:
                    self.update_status("performance_table_built", True)
                else:
                    print("[Stage 4] Performance table extraction failed or incomplete. State not updated.")
            else:
                print("[Stage 4] performance_table module not available. Skipping.")
        else:
            print("[Stage 4] Performance Table already COMPLETE. Skipping.")

        # Stage 5: Remuneration Report Assessment
        if not self.state["pipeline_status"]["remuneration_assessed"] or force_reprocess:
            if stage_remuneration_assessment:
                stage_dir = self.supporting_data_dir / "stage5_remuneration"
                self.logger.stage_start("Stage 5: Remuneration Report Assessment")
                success = stage_remuneration_assessment(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 5: Remuneration Report Assessment", success)
                if success:
                    self.update_status("remuneration_assessed", True)
                else:
                    print("[Stage 5] Remuneration assessment failed or incomplete. State not updated.")
            else:
                print("[Stage 5] remuneration_assess module not available. Skipping.")
        else:
            print("[Stage 5] Remuneration Report Assessment already COMPLETE. Skipping.")

        # Stage 6: Director Election Assessment
        if not self.state["pipeline_status"]["director_assessments_complete"] or force_reprocess:
            if stage_director_assessment:
                stage_dir = self.supporting_data_dir / "stage6_director_election"
                self.logger.stage_start("Stage 6: Director Election Assessment")
                success = stage_director_assessment(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 6: Director Election Assessment", success)
                if success:
                    self.update_status("director_assessments_complete", True)
                else:
                    print("[Stage 6] Director assessment failed or incomplete. State not updated.")
            else:
                print("[Stage 6] director_assess module not available. Skipping.")
        else:
            print("[Stage 6] Director Election Assessment already COMPLETE. Skipping.")

        # Stage 7: Other Resolutions Assessment
        if not self.state["pipeline_status"]["other_resolutions_assessed"] or force_reprocess:
            if stage_other_resolutions:
                stage_dir = self.supporting_data_dir / "stage7_other_resolutions"
                self.logger.stage_start("Stage 7: Other Resolutions Assessment")
                success = stage_other_resolutions(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 7: Other Resolutions Assessment", success)
                if success:
                    self.update_status("other_resolutions_assessed", True)
                else:
                    print("[Stage 7] Other resolutions assessment failed or incomplete. State not updated.")
            else:
                print("[Stage 7] other_resolutions_assess module not available. Skipping.")
        else:
            print("[Stage 7] Other Resolutions Assessment already COMPLETE. Skipping.")

        # Stage 7 Audit: Post-resolution assessment data quality check
        if not self.state["pipeline_status"]["audit_passed"] or force_reprocess:
            self.logger.stage_start("Stage 7 Audit: Data Quality Audit")
            self.logger.stage_end("Stage 7 Audit: Data Quality Audit", False, "Not yet implemented")
            print("[Stage 7 Audit] Running data quality audit...")
            print("[Stage 7 Audit] Not yet implemented. Skipping.")
        else:
            print("[Stage 7 Audit] Data quality audit already COMPLETE. Skipping.")

        # Stage 8: Text Summaries
        if not self.state["pipeline_status"]["text_summaries_generated"] or force_reprocess:
            if stage_text_summaries:
                stage_dir = self.supporting_data_dir / "stage8_text_summaries"
                self.logger.stage_start("Stage 8: Text Summaries")
                success = stage_text_summaries(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 8: Text Summaries", success)
                if success:
                    self.update_status("text_summaries_generated", True)
                else:
                    print("[Stage 8] Text summary generation failed or incomplete. State not updated.")
            else:
                print("[Stage 8] text_summaries module not available. Skipping.")
        else:
            print("[Stage 8] Text Summaries already COMPLETE. Skipping.")

        # Stage 9: Board Questions Generator
        if not self.state["pipeline_status"]["board_questions_generated"] or force_reprocess:
            if stage_board_questions:
                stage_dir = self.supporting_data_dir / "stage9_board_questions"
                self.logger.stage_start("Stage 9: Board Questions Generator")
                success = stage_board_questions(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 9: Board Questions Generator", success)
                if success:
                    self.update_status("board_questions_generated", True)
                else:
                    print("[Stage 9] Board questions generation failed or incomplete. State not updated.")
            else:
                print("[Stage 9] board_questions module not available. Skipping.")
        else:
            print("[Stage 9] Board Questions already COMPLETE. Skipping.")

        # Stage 10: Word Report Rendering
        if not self.state["pipeline_status"]["report_rendered"] or force_reprocess:
            if stage_report_rendering:
                stage_dir = self.supporting_data_dir / "stage10_report_rendering"
                self.logger.stage_start("Stage 10: Word Report Rendering")
                success = stage_report_rendering(self.workspace_dir, stage_dir, self.logger)
                self.logger.stage_end("Stage 10: Word Report Rendering", success)
                if success:
                    self.update_status("report_rendered", True)
                else:
                    print("[Stage 10] Report rendering failed or incomplete. State not updated.")
            else:
                print("[Stage 10] report_rendering module not available. Skipping.")
        else:
            print("[Stage 10] Report Rendering already COMPLETE. Skipping.")

        print(f"\nPipeline state saved to: {self.state_file.relative_to(BASE_DIR)}")
        print(f"Technical log saved to: {self.technical_log_path.relative_to(BASE_DIR)}")
        print(f"Supporting data saved to: {self.supporting_data_dir.relative_to(BASE_DIR)}")

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