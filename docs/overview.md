# Pipeline Overview

Generates ASA-style Voting Intentions (VI) reports for ASX-listed companies ahead
of their AGMs. Given a ticker and year, it pulls the Notice of Meeting, the last
five annual reports, and prior ASA/VI material, then runs a sequence of LLM
assessment stages against them and renders a final Word report.

**Run a single company:**

```bash
cd src && python orchestrator.py SUN 2026
```

**Daily batch:** `python orchestrator.py` with no arguments scans ASX for
today's NoM announcements and spawns one pipeline per company found.

---

## Execution model

- **Strictly sequential.** `CompanyPipeline.run_pipeline()` (src/orchestrator.py:241)
  calls the stages in the order below. Nothing runs in parallel at the pipeline
  level; a stage only runs after the one before it has finished.
- **Stage numbers are labels, not logic.** Order is the source order of the
  `if`-blocks in `run_pipeline()`. Nothing parses a stage number: state flags
  are named (`news_prefetched`, `remuneration_assessed`, ...), every
  inter-stage hand-off is a filename glob (`*Remuneration_Assessment.json`) or
  a directory scan, and `news_search_results.load_news_block` iterates all
  `supporting_data/stage*` directories. Numbers appear only in print/log
  strings and in the `stageN_*` supporting-data directory names, which no
  consumer reads by name. Repositioning a stage (e.g. moving 7.5 ahead of 5,
  2026-10-05) is a pure cut-and-paste of its block; no renumbering is required
  and no other file changes.
- **State-gated and resumable.** Each stage has a flag in
  `data/companies/{TICKER}_{YEAR}/state.json` (`pipeline_status`). A stage runs
  only if its flag is `false`; a successful run sets it to `true` and it is
  skipped on subsequent invocations. `run_pipeline(force_reprocess=True)`
  re-runs everything.
- **Validity re-checks beat the flag.** Stage 4 additionally re-validates its
  output file (an `extraction_status: failed` or empty `years`/`ceo_realised_pay`
  resets `performance_table_built`), and stale flags from older workspaces are
  migrated on load (`_load_or_init_state`, orchestrator.py:158).
- **Failure stops the flag, not the loop.** If a stage returns `False`, its
  flag stays `false` and the pipeline proceeds to the next stage; downstream
  stages degrade to "*Not available*" payload sections rather than aborting.
- **Every stage** logs into `reports/technical_log.md` (via `TechnicalLogger`)
  and writes intermediate artifacts under `supporting_data/stageN_*/`.

## Workspace layout

```
data/companies/{TICKER}_{YEAR}/
├── state.json                      # pipeline_status flags + metadata (atomic writes)
├── notices/                        # Stage 0 NoM PDFs; processed/ + failed/ subdirs
├── annual_reports/                 # Stage 2 AR PDFs ({TICKER}_{FY}_AR.pdf)
├── governance/                     # Stage 2 latest CGS ({TICKER}_{FY}_CGS.pdf)
├── vi_reports/                     # Stage 3 past VI/AGM/EGM report PDFs
├── extracted_json/
│   ├── notices/                    # Stage 1 NoM extractions (per-notice JSON)
│   ├── notices_archived/           # Stage 6 moves JSONs here on success
│   └── performance_history_5yr.json
├── reports/                        # final-stage .md / .docx outputs
├── supporting_data/
│   └── stageN_*/                   # manifests, payloads, raw LLM output, audit results
└── failed/                         # unprocessable inputs
```

Shared, cross-company resources live outside the workspace:
`data/_cache/annual_report_text/{TICKER}_{FY}.json` (page-text cache,
SHA-256-validated), `data/asa_guidelines/`, `data/_reference/` (comparator CSV,
`word_templates/VI_Report_Blank.docx`), and `prompts/*.txt`.

---

## Stages, in call order

### Stage 0 — NoM Discovery & Download
`find_nom.run_stage0_discovery` (src/find_nom.py:314) · flag `nom_discovered`

- **Inputs:** ticker + target year; ASX announcements pages (requests session
  with browser UA; headline must match `notice ... (meeting|agm|egm|gm)`,
  "letter to shareholders" / "sample proxy" / "voting form only" excluded).
- **Processing:** locates the NoM announcement, downloads the PDF (handles the
  ASX wrapper page and the "Agree and proceed" form), verifies it is a NoM and
  extracts the actual meeting year from the cover page (pypdf) to guard against
  year mismatches.
- **Outputs:** `notices/{TICKER}_{YEAR}_NOM.pdf`.
- No LLM. Daily-batch mode (`scan_today_announcements`, find_nom.py:219) walks
  today's ASX announcements instead of a specific ticker.

### Stage 1 — NoM Extraction
`extract_nom.run_batch_pipeline` (src/extract_nom.py:113) · flag `nom_extracted`

- **Inputs:** every `notices/*.pdf` (Stage 0), `prompts/extract_nom.txt`,
  OpenRouter (primary `EXTRACT_NOM_MODEL`, default `stealth/ox-alpha`;
  fallback `gemini-2.0-flash-exp:free`).
- **Processing:** pypdf text extraction → LLM structured extraction of AGM
  details (company name, meeting date/venue, resolutions, voting items,
  director candidates and their details).
- **Outputs:** `extracted_json/notices/{notice-stem}.json` (one JSON per NoM),
  raw LLM text in `supporting_data/`; the source PDF is moved to
  `notices/processed/`, failures to `failed/`.
- Schema-invalid responses trigger a retry with the fallback model.

### Stage 2 — Annual Reports Download
`fetch_annual_reports.fetch_annual_reports` (src/fetch_annual_reports.py:69) ·
flag `annual_reports_downloaded`

- **Inputs:** ticker + target year; ASX announcement pages via
  **Playwright** (headless browser; `safe_goto` retries on HTTP2/network
  failures). `count=5` — the five most recent annual reports.
- **Processing:** finds each company's AR announcements, resolves the direct
  `/asxpdf/` URL from the HTML wrapper, downloads the PDFs, counts pages.
  Also detects name/code-change announcements to discover **legacy codes**
  (returned alongside the result and stored in `state.json → metadata.legacy_codes`).
  Then a CGS pass (target year only): matches announcements headed
  "Appendix 4G" / "CGC 4G" / "Corporate Governance Statement" (combined
  "Annual Report and CGS" lodgements are skipped — that document is the AR
  itself). Many issuers embed the CGS in the AR and file no standalone
  announcement; a miss is recorded as `not_found` / assumed embedded and does
  **not** fail the stage. The CGS is saved outside `annual_reports/` because
  the later stages glob that directory assuming every PDF there is an AR.
- **Outputs:** `annual_reports/{TICKER}_{FY}_AR.pdf` (five FYs),
  `governance/{TICKER}_{FY}_CGS.pdf` (latest, when a standalone CGS exists),
  and `supporting_data/.../cgs_summary.json`.
- No LLM.

### Stage 3 — VI Reports Download
`fetch_past_vi_reports.fetch_vi_reports` (src/fetch_past_vi_reports.py:112) ·
flag `vi_reports_downloaded`

- **Inputs:** ticker + target year, going back `years_back=5` plus any
  `legacy_codes` from Stage 2.
- **Processing:** scrapes the company's ASA page, which keeps the two report
  families in sibling divs: `voting-intentions` (VI reports) and
  `meeting-reports` (AGM and EGM reports, told apart from the link text).
- **Outputs:** `vi_reports/{TICKER}_{YYYY}_VIR.pdf`,
  `vi_reports/{TICKER}_{YYYY}_AGM.pdf` and
  `vi_reports/{TICKER}_{YYYY}_EGM.pdf` (not every company publishes an
  AGM/EGM report every year; EGMs are rare), plus
  `supporting_data/.../vi_reports_summary.json` with per-type
  `downloaded` / `agm_downloaded` / `egm_downloaded` maps. Missing-year
  reporting and `status` stay VI-based.
- No LLM. Gaps are tolerated: a year with no downloadable report is recorded
  and skipped.

### Stage 4 — Financial Performance Table
`performance_table.run_pipeline` (src/performance_table.py:1375) · flag
`performance_table_built` · model `PERFORMANCE_MODEL` (default
`openai/gpt-4.1-mini`, fallback `gemini-3.1-flash-lite`)

- **Inputs:** all `annual_reports/*.pdf` (sorted latest-first), via
  `ar_text_cache.get_ar_pages` (cached extraction; unusable pages, e.g.
  RIO_2022's `(cid:N)` glyph pages, are excluded). Page selection is
  keyword-scoped with a 50-page cap and radius expansion.
- **Sub-steps (run in this order inside the stage):**
  - **4a** — 5-year financial summary table from the *latest* AR
    (`PROMPT_1A_SCHEMA`: npat, underlying npat, share price, dividend,
    simple TSR, EPS, revenue, net assets, ROE/ROCE, each with a source page).
  - **4b** — cross-checks the share prices against the
    remuneration-performance link table (`PROMPT_1B_SCHEMA` overrides).
  - **4c** — fills any missing year/metric from the individual ARs
    (`PROMPT_2`).
  - **4d** — **CEO realised remuneration, disclosure-first**, one LLM call per
    AR year (`PROMPT_3_SCHEMA` with `currency`, `figure_fy`,
    `not_disclosed_reason` fields). Responses pass through the semantic
    validator built by `_make_ceo_pay_validator`, which enforces
    `src/currency_anchor.py` (per-row currency/scale resolution —
    `contains_aud_million`, `aud_million_anchors`) and `src/disclosure_check.py`
    (rejects `not_disclosed` when a disclosed realised figure is visible on
    supplied pages; part-year service is a qualifier, not a non-disclosure).
    See `docs/stage4d_ab_postfix.json` and `docs/validator_traps.md` for the
    measurement history of these checks.
  - **Step 5** — compile and validate the assembled result
    (`PROMPT_4_SCHEMA`).
- **Outputs:** `extracted_json/performance_history_5yr.json`
  (`years{...}` + `ceo_realised_pay{...}` + `audit_notes`), plus supporting
  manifests. The orchestrator re-validates this file on load; an invalid one
  resets the flag and re-runs the stage.

### Stage 7.5 — News Prefetch (runs 5th, before the assessment stages)
`news_prefetch.run_news_prefetch` (src/news_prefetch.py:296) · flag
`news_prefetched`

- **Inputs:** company name (from the Stage 1 NoM JSON; ticker as fallback),
  `TAVILY_API_KEY`, a fixed set of concern-oriented search queries (news
  topic, 12-month window). No LLM.
- **Processing:** each query runs independently against Tavily's REST API with
  isolated failure handling; results are deduplicated. **No
  relevance/company-match filtering is done here by design** — downstream
  prompts are instructed to verify each item is about the right company.
- **Outputs:** `supporting_data/stage7_5_news_prefetch/news_search_raw.json`,
  `news_search_results.txt` (the formatted block consumers read),
  `news_prefetch_metadata.json`, `news_prefetch_status.txt`, and a copy in
  `reports/{TICKER}_{YEAR}_News_Search_Results.txt`.
- **Position:** moved ahead of Stage 5 (2026-10-05) so the results are on disk
  before the resolution assessments run and any of them may use them. The
  stage still carries its 7.5 label: stage numbers are labels only (see the
  execution-model note), so repositioning cost no renumbering.
- **Consumers (all via `news_search_results.load_news_block`, which scans
  every `supporting_data/stage*` directory and does not depend on the stage
  number):** Stages 5, 6 and 7 (assessment context), the Stage 7 Audit
  (supplementary, non-audited context), Stages 8 and 9 (as before), and Stage
  10 (renders the block into the report).

### Stage 5 — Remuneration Assessment
`remuneration_assess.run_remuneration_assessment` (src/remuneration_assess.py:496) ·
flag `remuneration_assessed` · `REM_ASSESS_PRIMARY_MODEL` (default
`openai/gpt-4.1-mini`, fallback `gemini-3.1-flash-lite`)

- **Inputs (retrieval-scoped, never whole-document concatenation):**
  - current-year AR — `ar_retrieval.build_context(pages, STAGE5_SPECS, 250k
    chars)` → `retrieval_manifest_stage5.json`
  - prior-year AR — same specs, 120k chars → second manifest
  - Notice of Meeting (from Stage 1 JSON + PDF) — 120k chars
  - `data/_reference/ceo_kmp_comparator.csv`, `prompts/rem_assess.txt`
  - pre-fetched news block (Stage 7.5, DOCUMENT 8) — supplementary context;
    the prompt verifies company match and treats items as unverified leads
  - `coverage_note()` from the retrieval result is passed into the prompt so
    un-supplied sections resolve to *Uncertain* rather than being fabricated.
- **Processing:** LLM assessment of the remuneration resolution against the ASA
  Consolidated Voting and Assessment Guidelines (pay-for-performance,
  realised remuneration, malus/clawback, comparator positioning).
- **Outputs:** `reports/{TICKER}_{YEAR}_Remuneration_Assessment.md` and
  `extracted_json/{TICKER}_{YEAR}_Remuneration_Assessment.json`; raw LLM output
  and both retrieval manifests in `supporting_data/`. Inline `[p.N]`
  citations are existence-checked by `src/citation_check.py` (warnings only —
  existence, not accuracy).
- Emits a document inventory (which source docs were supplied) with the result.

### Stage 6 — Director Election Assessment
`director_assess.run_pipeline` (src/director_assess.py:265) · flag
`director_assessments_complete` · `DIRECTOR_ASSESS_PRIMARY_MODEL` (default
`stealth/ox-alpha`, fallback `gemini-2.0-flash-exp:free`)

- **Inputs (per notice JSON):** one LLM call per pending
  `extracted_json/notices/*.json`, the matching AR
  (`STAGE6_SPECS`, 200k chars, own retrieval manifest), the full
  `data/asa_guidelines/` text, `prompts/director_assess.txt`, and the
  pre-fetched news block (Stage 7.5, INPUT 4 - external due-diligence
  context; omitted when absent).
- **Processing:** director-by-director evaluation (qualifications,
  independence, tenure, disclosure alignment) against the ASA guidelines,
  producing a per-director recommendation.
- **Outputs:** `reports/{notice-stem}_Director_Evaluation.md` **per notice
  JSON**, raw LLM text in `supporting_data/`. Successful notice JSONs are
  moved to `extracted_json/notices_archived/`; errors go to `failed/`.
- Citations existence-checked by `citation_check` (warning level).

### Stage 7 — Other Resolutions Assessment
`other_resolutions_assess.run_other_resolutions_assessment`
(src/other_resolutions_assess.py:547) · flag `other_resolutions_assessed` ·
`OTHER_RESOLUTIONS_PRIMARY_MODEL` (default `openai/gpt-4.1-mini`, fallback
`gemini-3.1-flash-lite`)

- **Inputs:** the Stage 1 NoM JSON, the current-year AR (`STAGE7_SPECS`, 250k
  chars, own manifest), `prompts/other_resolutions_assess.txt`, and the
  pre-fetched news block (Stage 7.5, INPUT 3 - resolution-relevant context).
- **Processing:** assesses every non-director, non-remuneration resolution
  (capital changes, auditors, constitutional amendments, ...) against ASA
  governance / transparency / capital-management guidelines.
- **Outputs:** `reports/{TICKER}_{YEAR}_Other_Resolutions_Assessment.json`,
  mirrored as `supporting_data/.../other_resolutions_result.json`.

### Stage 7 Audit — Data Quality Audit
`stage7_audit.run_audit` (src/stage7_audit.py:309) · flag `audit_passed` ·
`AUDIT_PRIMARY_MODEL` (default `openai/gpt-4.1-mini`)

- **Inputs:** every output produced so far (performance JSON, NoM JSON,
  assessment outputs, `state.json`), plus the pre-fetched news block
  (Stage 7.5) as **supplementary, non-audited** context - it is not listed as
  a missing document and its (non-)use is not a data-quality issue.
- **Processing:** automatic checks (schema validity, range sanity,
  cross-field consistency) + an LLM qualitative audit scored 0–100.
  Loop: *audit → score below `AUDIT_PASS_THRESHOLD` (default 75) → targeted
  remediation (src/stage7_remediation.py re-queries the flagged values
  against cached AR text) → re-audit*, up to 2 remediation cycles.
- **Outputs:** `reports/{TICKER}_{YEAR}_Audit_Report.json`,
  `supporting_data/.../audit_result.json`, `auto_audit_issues_{n}.json`,
  `audit_summary_{n}.txt` per cycle.
- **Pass gate:** the score threshold (`_audit_passes`, stage7_audit.py:310) —
  same condition the remediation loop uses to stop remediating. The qualitative
  `overall_assessment` is recorded but does not gate. Fixed 2026-10-04: the
  gate previously read `audit_result.get("aurdit_passed_threshold")` (a typo),
  always returned `None`, and silently degenerated to
  `overall_assessment != "FAIL"` — a below-threshold PARTIAL passed. Regression
  test: `tests/test_audit_gate.py`.

### Stage 8 — Text Summaries
`text_summaries.run_text_summaries` (src/text_summaries.py:354) · flag
`text_summaries_generated` · `TEXT_SUMMARY_MODEL` (default
`openai/gpt-4.1-mini`, fallback `gemini-3.1-flash-lite`)

- **Inputs (assembled payload, saved as `stage8_payload.txt`):**
  financial performance (performance_history_5yr.json) → remuneration
  assessment → director election assessments → other resolutions →
  pre-fetched news block. Each section degrades to "*Not available*" if its
  source stage failed.
- **Processing:** one LLM call (`prompts/analysis_section.txt`) producing the
  human-readable governance + financial analysis section.
- **Outputs:** `reports/{TICKER}_{YEAR}_Analysis_Section.md`
  (+ supporting `analysis_section.md`, `text_summaries_metadata.json`).

### Stage 9 — Board Questions Generator
`board_questions.run_board_questions` (src/board_questions.py:409) · flag
`board_questions_generated` · `BOARD_QUESTIONS_MODEL` (default
`openai/gpt-4.1-mini`)

- **Inputs (documented inventory, saved as `stage9_payload.txt`):**
   1. current-year AR — `STAGE9_SPECS`, 150k-char retrieval + manifest
   2. Corporate Governance Statement — assumed to sit within the AR text; a
      standalone CGS PDF may now exist in `governance/` (Stage 2) but is not
      parsed into this payload
   3. remuneration assessment (md + json)
   4. other-resolutions assessment
   5. pre-fetched news results (Stage 7.5)
   6–7. prior-year ASA Voting Intentions / AGM report — availability is now
      detected from `vi_reports/` (`*.pdf` / `*AGM*` globs, so doc 7 reports
      "available" once AGM PDFs exist), but parsing the PDFs into the payload
      is not implemented
- **Processing:** LLM (`prompts/board_questions.txt`) generates **10 targeted
  AGM board questions with voting rationales**, each grounded in the supplied
  documents.
- **Outputs:** `reports/{TICKER}_{YEAR}_Board_Questions.md` (+ supporting
  `board_questions.md`, `board_questions_metadata.json`).

### Stage 10 — Word Report Rendering
`report_rendering.run_report_rendering` (src/report_rendering.py:693) · flag
`report_rendered` · no LLM

- **Inputs:** `data/_reference/word_templates/VI_Report_Blank.docx` and every
  upstream output: NoM details/resolutions (Stage 1), performance JSON
  (Stage 4), remuneration assessment md+json (Stage 5), other-resolutions
  JSON (Stage 7), analysis section (Stage 8), board questions (Stage 9),
  news results (Stage 7.5).
- **Processing:** python-docx template fill-in. Generated content is inserted
  **at the template's existing section anchors** (lxml paragraph movement into
  AGM Summary, Resolution Voting, Five-year Financial History,
  CEO Remuneration Framework, and "5. Voting Rationales") rather than appended
  at the end. Tables populated: AGM summary, resolution voting (vote
  recommendations joined from NoM + Stage 5/7 assessments), five-year
  financial history, CEO remuneration framework.
- **Outputs:** `reports/{TICKER}_{YEAR}_VI_Report.docx` (the deliverable),
  `report_rendering_metadata.json`, `report_rendering_summary.txt`.

---

## Shared infrastructure

- **`ar_text_cache`** — pdfplumber page extraction cached as
  `data/_cache/annual_report_text/{TICKER}_{FY}.json`; cache validity by
  SHA-256 (size/mtime fallback), `EXTRACTOR_VERSION` gate. Unusable pages
  (RIO_2022's unrecoverable `(cid:N)` glyph pages) are flagged `usable: false`
  and never consume retrieval budget.
- **`ar_retrieval`** — lexical "RAG-lite": declarative section specs
  (`STAGE5/6/7/9_SPECS`), keyword matching with ±1-page radius, clustering,
  ranking, and character-budgeted context assembly. Budgets are per-stage env
  vars (default 250k / 120k / 200k / 250k / 150k chars). A
  `retrieval_manifest_stageN.json` is written per call; check
  `missing_mandatory` when reviewing output. `build_context`'s
  `coverage_note()` goes into every prompt that uses it.
  **Regression gate:** `cd src && python retrieval_selftest.py` (all mandatory
  sections located across every cached report; currently 56 report/stage
  combinations pass).
- **Validators (the checking layer — read `docs/validator_traps.md` before
  trusting any pass):**
  - `src/currency_anchor.py` — per-row currency + scale resolution
    (currency is a per-row attribute in dual-listed issuers).
  - `src/disclosure_check.py` — proves a *disclosed* realised-remuneration row
    exists before `not_disclosed` is accepted.
  - `src/citation_check.py` — `[p.N]` citations in Stages 5/6 refer to pages
    that were actually supplied. **Existence only; accuracy is not verified.**
- **LLM plumbing** — OpenRouter for all LLM stages, per-stage model env vars
  with fallback models, `TokenBucket` rate limiting, JSON-schema validation
  with retry, primary → fallback model failover. Models are configured in
  `.env` (stage-specific variables, see `.env.example`).
- **`state.json`** — atomic writes (`os.replace` over a temp file);
  `extracted_data` accumulates stage payloads for the audit stage.

## Data flow (one line per hand-off)

```
ASX announcements ──▶ NoM PDF (0) ──▶ NoM JSON (1) ──▶ Stages 5, 6, 7, 8, 9, 10
ASX announcement pages ──▶ AR PDFs x5 + latest CGS PDF (2)
                            ARs ──▶ cached page text ──▶ retrieval slices
                                                                     ├─▶ Stage 4 (table + CEO pay)
                                                                     ├─▶ Stage 5 (remuneration)
                                                                     ├─▶ Stage 6 (directors)
                                                                     ├─▶ Stage 7 (other resolutions)
                                                                     └─▶ Stage 9 (board questions)
                            CGS ──▶ governance/ (not yet consumed by any stage)
Company website ──▶ prior VI/AGM/EGM PDFs (3)  [kept for reference; not yet parsed]
All of the above  ──▶ Stage 7 Audit (+ remediation loop)
Tavily API        ──▶ news block (7.5) ──▶ Stages 5, 6, 7 (assessment context), 8, 9
Everything        ──▶ Stage 8 (analysis) ──▶ Stage 10
All outputs       ──▶ Stage 10 ──▶ {TICKER}_{YEAR}_VI_Report.docx
```

## Known limitations (as of this writing)

1. **Citation accuracy unverified** — `citation_check` proves cited pages were
   supplied, not that they support the statement. Stages 5/6 emit prose, so
   this needs structured output fields.
2. **Statutory vs realised table families** — no rule distinguishes the two
   captions; the model still reads the statutory total as realised in
   `docs/stage4d_ab_postfix.json` (5 of 72 post-fix runs).
3. **Stage 9 documents 6–7 (prior-year ASA reports)** — the PDFs are now
   downloaded (VI/AGM/EGM, Stage 3) and the inventory detects doc 7 via
   `*AGM*` in `vi_reports/`, but parsing the PDFs into the Stage 9 payload is
   not implemented. A standalone CGS is now downloaded to `governance/`
   (Stage 2) but is likewise not yet parsed into the payload.
4. **RIO_2022** — 141/424 pages unrecoverable (no usable character map);
   dependent rules must resolve to Uncertain.
5. **No parallelism** — stages are sequential; the five AR PDFs in Stage 4c
   and the news queries in 7.5 each process serially.

## Fixed since the overview was first drafted

- **Review-fix Batch 0-1 (2026-10-05)** — Stage 9 crashed with a `NameError`:
  `_build_board_questions_payload` used `supporting_data_dir` without the
  parameter; Stages 8/9 read Stage 7's `in_scope_resolutions` with pre-schema
  keys (`res_number`/`assessment_result`) so document 4 rendered "Resolution ? :
  N/A"; the Stage 7 news block was numbered INPUT 3 where the prompt says
  DOCUMENT 4; the .docx voting table applied one global F/U remuneration signal
  to every row (now resolution-specific); the 4d validator rejected all
  `ceo_realised_rem < 0.8m` with no part-year exception. Regression test:
  `tests/test_review_fixes_batch1.py`; Stages 8-10 re-run on SUN_2026 confirm.
- **Review-fix Batch 2 (2026-10-05)** — F6: `news_prefetch` skip path now returns
  `False` (was `True`), so a no-`TAVILY_API_KEY` run no longer permanently locks
  `news_prefetched` and skips forever on the next run. F7: the Stage 7 audit's
  VI-coverage check (Check 7) and Stage 9's doc-6 availability both count only
  `*_VIR.pdf` in `vi_reports/` — AGM/EGM reports (Stage 3) no longer mask
  missing VI reports. F8: new `src/ar_file_lookup.py` (`find_ar_file` +
  `parse_year_from_stem`) is the single AR-lookup source of truth for
  remuneration, board-questions, other-resolutions and director modules;
  exact-year match wins, most-recent-available fallback is opt-in and logged; the
  FY-(N-1) comparison pair is exact-match only. F9/F11: new `src/llm_json.py`
  `parse_llm_json` replaces the inline fence/quote/comma parsers in
  performance_table, remuneration_assess, other_resolutions_assess and
  stage7_audit (the audit's unhardened two-block parse is gone); the recovery
  scan is now O(L) (jump to each `{`), not O(L^2). Regression test:
  `tests/test_batch2_shared_helpers.py`; Stages 5-10 re-run live on SUN_2026.
- **Review-fix Batch 3 (2026-10-05)** — F12: `ar_retrieval.build_context`
  precomputes the per-page lowercased-text index once (`_build_lowered_index`)
  instead of re-lowercasing the whole annual report for every `SectionSpec`
  (~5x per company across the 14 cached reports); `find_section_pages` now
  delegates to the indexed `_find_section_pages_indexed`. F13: the 10 Tavily
  news queries in `news_prefetch.fetch_company_news` now run via a
  `ThreadPoolExecutor` (was serial) — `_run_single_query` takes an explicit
  `logger` for thread safety. F14: `fetch_annual_reports` caches the
  target-year announcement rows during Pass 1 (`announcement_rows_cache`) and
  reuses them in the CGS pass instead of refetching the same ASX page; F14 also
  removed the duplicate `safe_goto`. F15: a pre-existing CGS file is now
  validated with a `%PDF` magic-byte check before reuse, and a corrupt
  zero-byte file is deleted and re-fetched rather than trusted. Regression:
  `tests/test_batch2_shared_helpers.py` (covers F8/F9/F11); Stages 5, 6, 9
  re-run live on SUN_2026.
- **Dead-import sweep (2026-10-05)** — closed out the dead-code review. Removed
  21 unused imports across 11 modules (orchestrator, news_search_results,
  text_summaries, report_rendering, fetch_past_vi_reports, find_nom,
  other_resolutions_assess, stage7_audit, stage7_remediation, ar_text_cache,
  logging_utils); an AST + word-boundary pass confirms 0 unused imports remain.
  `report_rendering.py` additionally dropped the shadowed first
  `_replace_placeholders`, its orphaned `_load_remuneration_json` helper, and an
  unused `rem_json` load; `text_summaries.py` gained the missing
  `news_search_results.load_news_block` import that Stage 8's F3 test exercises.
  Verified: 24 modules parse, all modules import, 9 test suites pass, and the
  retrieval self-test still locates every mandatory section.
- **AGM/EGM reports + standalone CGS now downloaded** (2026-10-05) — Stage 3
  scrapes the ASA `meeting-reports` div alongside `voting-intentions` and saves
  `{TICKER}_{YYYY}_AGM.pdf` / `_EGM.pdf` next to the VI reports; Stage 2 fetches
  the target-year standalone CGS ("Appendix 4G" / "CGC 4G" / "Corporate
  Governance Statement" announcements, combined AR+CGS lodgements skipped) into
  `governance/{TICKER}_{FY}_CGS.pdf` with a `cgs_summary.json` per run. A CGS
  miss is expected (many issuers embed it in the AR) and does not fail Stage 2.
- **Stage 7 Audit threshold now enforced** (2026-10-04) — the
  `aurdit_passed_threshold` typo made the score gate fall through to
  `overall_assessment != "FAIL"`. The gate is now `_audit_passes()`
  (stage7_audit.py:310), matching the remediation loop's own stop condition;
  pinned by `tests/test_audit_gate.py`.
