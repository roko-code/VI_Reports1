File Matching Convention

- Raw file naming (Step 1): Use a common prefix with the entity, year and type.
	Example: BHP_2026_NOM.pdf -> produces BHP_2026_NOM.json

- Directory structure: All company-specific files live under a per-company
  workspace directory:
	data/companies/BHP_2026/
	  notices/
	    BHP_2026_NOM.pdf
	    processed/
	  annual_reports/
	    BHP_2026_AR.pdf
	  extracted_json/
	    notices/
	      BHP_2026_NOM.json
	    notices_archived/
	  vi_reports/
	    BHP_2026_VIR.pdf
	  reports/
	  failed/

- Annual report naming and location (Step 2): Save the Annual Report PDF using the
  same prefix and an _AR suffix under the company workspace annual_reports/ directory.
  Example: data/companies/BHP_2026/annual_reports/BHP_2026_AR.pdf

- General rules:
	- Prefix: ENTITY_YEAR (e.g., BHP_2026)
	- Suffix: document type (e.g., NOM, AR, FIN, SUP)
	- Extensions: raw files as .pdf, generated metadata as .json
	- Keep file names ASCII-safe, use underscores, no spaces.

- Mapping examples:
	- BHP_2026_NOM.pdf  -> data/companies/BHP_2026/notices/BHP_2026_NOM.pdf
	- BHP_2026_NOM.json -> data/companies/BHP_2026/extracted_json/notices/BHP_2026_NOM.json
	- BHP_2026_AR.pdf   -> data/companies/BHP_2026/annual_reports/BHP_2026_AR.pdf
	- BHP_2026_VIR.pdf  -> data/companies/BHP_2026/vi_reports/BHP_2026_VIR.pdf

- VI Reports are reference documents (like Annual Reports).
  They are downloaded from the ASA website and stored flat
  in the vi_reports/ subdirectory. No raw/processed split.

- The VI Reports search window is [target_year - years_back, target_year - 1],
  i.e. the current AGM year's VI Report is never downloaded because this
  project creates the current VI Report.

- Shared reference documents (e.g., ASA Guidelines) remain at the project level:
  data/asa_guidelines/

- Legacy ticker codes (discovered via name/code change announcements during AR
  download) are saved in state.json metadata.legacy_codes.
  Downstream stages use these to fetch data under the old ASX code as well.
  Example: state.json -> metadata.legacy_codes

- Pipeline numbering (can be extended with additional stages/audits as needed):
  Stage 1  NoM Extraction
  Stage 2  Annual Reports Download
  Stage 3  VI Reports Download
  Stage 4  Financial Performance Table
  Stage 5  Remuneration Report Assessment
  Stage 6  Director Election Assessment
  Stage 7  Other Resolutions Assessment
  Stage 7 Audit  Data quality audit (inserted after Stage 7)
  Stage 8  Text Summaries from Annual Reports
  Stage 9  Board Questions Generator
  Stage 10 Word Report Rendering

- Output structure per company workspace:
  data/companies/{TICKER}_{YEAR}/
    reports/
      technical_log.md                  # Append-only technical log for the entire pipeline
    supporting_data/
      stage1_nom_extraction/
      stage2_ar_download/
      stage3_vi_reports/
      stage5_remuneration/
      stage6_director_election/
      stage7_other_resolutions/
      stage7_audit/
      stage8_text_summaries/
      stage9_board_questions/
      stage10_report_rendering/

- Each stage module may write raw LLM responses, intermediate JSON,
  and QA breakdowns into its supporting_data subdirectory.
  The orchestrator writes stage start/end/error entries to technical_log.md.

Ensure consistency across steps so automated pipelines can match raw PDFs to their
generated JSON metadata by filename prefix and suffix.
