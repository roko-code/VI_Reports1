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

- Comparator datasets (e.g., CEO/KMP compensation) stored at project level:
  data/_reference/ceo_kmp_comparator.csv

- AR text cache stored at project level (not committed to git,
  regenerable from AR PDFs):
  data/_cache/annual_report_text/{TICKER}_{FY}.json
  Cache format: list of {page_number: int, text: str}
  Invalidation: file_size + mtime check on every read
  Metadata: extractor_version, file_hash (SHA256), page_char_counts

- Legacy ticker codes (discovered via name/code change announcements during AR
  download) are saved in state.json metadata.legacy_codes.
  Downstream stages use these to fetch data under the old ASX code as well.
  Example: state.json -> metadata.legacy_codes

- Pipeline numbering (can be extended with additional stages/audits as needed):
  Stage 0  NoM Discovery & Download (find_nom.py - search ASX by ticker/year)
  Stage 1  NoM Extraction (extract_nom.py)
  Stage 2  Annual Reports Download (fetch_annual_reports.py)
  Stage 3  VI Reports Download (fetch_past_vi_reports.py)
  Stage 4  Financial Performance Table (performance_table.py)
  Stage 5  Remuneration Report Assessment (remuneration_assess.py)
  Stage 6  Director Election Assessment (director_assess.py)
  Stage 7  Other Resolutions Assessment (other_resolutions_assess.py)
  Stage 7 Audit  Data quality audit with remediation loop (stage7_audit.py)
  Stage 8  Text Summaries from Annual Reports
  Stage 9  Board Questions Generator
  Stage 10 Word Report Rendering

- Output structure per company workspace:
  data/companies/{TICKER}_{YEAR}/
    reports/
      {TICKER}_{YEAR}_Remuneration_Assessment.json
      {TICKER}_{YEAR}_Other_Resolutions_Assessment.json
      {TICKER}_{YEAR}_Audit_Report.json
      {TICKER}_*_Director_Evaluation.md
      performance_table.md
      technical_log.md                  # Append-only technical log for the entire pipeline
    supporting_data/
      stage0_nom_discovery/
      stage1_nom_extraction/
      stage2_ar_download/
      stage3_vi_reports/
      stage4_performance_table/
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

## Model Configuration

- All model selections are in .env, using stage-specific env vars:
  EXTRACT_NOM_MODEL / EXTRACT_NOM_FALLBACK_MODEL
  DIRECTOR_ASSESS_PRIMARY_MODEL / DIRECTOR_ASSESS_FALLBACK_MODEL
  REM_ASSESS_PRIMARY_MODEL / REM_ASSESS_FALLBACK_MODEL
  OTHER_RESOLUTIONS_PRIMARY_MODEL / OTHER_RESOLUTIONS_FALLBACK_MODEL
  PERFORMANCE_MODEL / PERFORMANCE_FALLBACK_MODEL
  AUDIT_PRIMARY_MODEL / AUDIT_FALLBACK_MODEL
  REMEDIATION_PRIMARY_MODEL / REMEDIATION_FALLBACK_MODEL

- Each module has its own TokenBucket rate limiter (20 RPM default).
- All modules use OpenRouter base URL with OpenAI-compatible client.

## Schema Validation & Retry Logic

- All assessment modules output JSON with schema validation.
- _parse_json_from_response() strips markdown fences, fixes common
  JSON issues (single quotes, trailing commas), and validates dict type.
- JSON validation retries (2 retries default) with re-ask on failure.
- Fallback to configured fallback model after retries exhausted.
- Strict JSON mode (response_format) available for simple schemas.
- Integer/string flexibility: schemas accept both "integer" and "string"
  types for fields where LLMs often return strings.

## Stage 7 Audit & Remediation Loop

- Stage 7 Audit performs automated checks (8 categories) +
  LLM qualitative review of all pipeline outputs.
- If score < AUDIT_PASS_THRESHOLD (default: 75), remediation loop triggers.
- Remediation sends targeted value-verification requests to LLM
  with source document excerpts. No assumptions about units -
  every value verified against cited source pages.
- Auto-applies corrections with technical log notes.
- Max 2 remediation cycles (3 total audit passes).
- All remediation history (before/after) appended to final audit report.

## Assessment Script Architecture

- Three assessment scripts (remuneration, director, other_resolutions)
  are kept separate (different prompts, schemas, source docs).
- All assessment scripts standardized on JSON output format.
- Future: share common utilities via assessment_base.py.

## State Management

- state.json uses atomic writes (write to .tmp, then os.replace()).
- Migration logic in _load_or_init_state automatically adds
  missing pipeline_status keys from DEFAULT_STATE_SCHEMA.
- Stale performance_table_built flag automatically reset if
  output has no years extracted.
- All pipeline_status flags default to False on fresh workspace.

Ensure consistency across steps so automated pipelines can match raw PDFs to their
generated JSON metadata by filename prefix and suffix.
