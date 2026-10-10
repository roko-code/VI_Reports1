# Workflow Summary

End-to-end pipeline that turns an ASX ticker + year into an ASA-style Voting
Intentions (VI) Word report. Strictly sequential; each stage is state-gated in
`data/companies/{TICKER}_{YEAR}/state.json` and only runs if its flag is false.
This document is the compact step-by-step reference; the full history and
fix-log lives in `docs/overview.md`.

Run one company: `cd src && python orchestrator.py SUN 2026`.

## Pipeline at a glance

| # | Stage | Module / entry | State flag | LLM? | Primary input(s) | Primary output(s) |
|---|-------|----------------|-----------|------|------------------|-------------------|
| 0 | NoM discovery/download | `find_nom` | `nom_discovered` | no | ticker, year, ASX announcements | `notices/{T}_{Y}_NOM.pdf` |
| 1 | NoM extraction | `extract_nom` | `nom_extracted` | yes | NoM PDF(s) | `extracted_json/notices/*.json` |
| 2 | AR + CGS download | `fetch_annual_reports` | `annual_reports_downloaded` | no | ticker, year, ASX (Playwright) | `annual_reports/{T}_{FY}_AR.pdf` ×5, `governance/{T}_{FY}_CGS.pdf` |
| 3 | Prior VI/AGM/EGM download | `fetch_past_vi_reports` | `vi_reports_downloaded` | no | ticker, ASA page, legacy codes | `vi_reports/{T}_{YYYY}_{VIR,AGM,EGM}.pdf` |
| 4 | Financial perf. table + CEO pay | `performance_table` | `performance_table_built` | yes | AR PDFs ×5 | `extracted_json/performance_history_5yr.json` |
| 7.5 | News prefetch | `news_prefetch` | `news_prefetched` | no | company name, Tavily | `supporting_data/stage7_5_news_prefetch/*` |
| 5 | Remuneration assessment | `remuneration_assess` | `remuneration_assessed` | yes | AR, prior AR, NoM, comparator CSV, news | `reports/{T}_{Y}_Remuneration_Assessment.{md,json}` |
| 6 | Director assessment | `director_assess` | `director_assessments_complete` | yes | NoM JSONs, AR, ASA guidelines, news | `reports/{stem}_Director_Evaluation.md` |
| 7 | Other-resolutions assessment | `other_resolutions_assess` | `other_resolutions_assessed` | yes | NoM JSON, AR, news | `reports/{T}_{Y}_Other_Resolutions_Assessment.json` |
| 7A | Data-quality audit (+remediation) | `stage7_audit` / `stage7_remediation` | `audit_passed` | yes | every output above + news | `reports/{T}_{Y}_Audit_Report.json` |
| 8 | Analysis section | `text_summaries` | `text_summaries_generated` | yes | perf + 5/6/7 outputs + news | `reports/{T}_{Y}_Analysis_Section.md` |
| 9 | Board questions | `board_questions` | `board_questions_generated` | yes | AR, CGS, rem/other outputs, news, prior VI/AGM | `reports/{T}_{Y}_Board_Questions.md` |
| 10 | Word report render | `report_rendering` | `report_rendered` | no | template + all upstream outputs | `reports/{T}_{Y}_VI_Report.docx` |

Stage numbers are labels only — execution order is the source order of the
`if`-blocks in `orchestrator.run_pipeline`. 7.5 runs ahead of 5 so the news block
is on disk for every assessment stage.

## Step-by-step inputs and outputs

### Stage 0 — NoM Discovery & Download
- **In:** ticker + target year (CLI or daily-batch ASX scan); ASX announcement pages.
- **Out:** `notices/{TICKER}_{YEAR}_NOM.pdf`.
- No LLM.

### Stage 1 — NoM Extraction
- **In:** every `notices/*.pdf`; `prompts/extract_nom.txt`; OpenRouter model.
- **Process:** pypdf text → LLM structured AGM JSON (company, date, resolutions,
  voting items, director candidates).
- **Out:** `extracted_json/notices/{stem}.json` (one per NoM). Source moved to
  `notices/processed/`, failures to `failed/`.

### Stage 2 — AR + CGS Download
- **In:** ticker + year; ASX announcement pages (Playwright).
- **Process:** resolve `count=5` AR announcements, download PDFs; detect legacy
  codes; target-year CGS pass ("Appendix 4G"/"CGC 4G"/"Corporate Governance
  Statement"; combined AR+CGS lodgements skipped).
- **Out:** `annual_reports/{T}_{FY}_AR.pdf` (5 years),
  `governance/{T}_{FY}_CGS.pdf` (when a standalone CGS exists), `cgs_summary.json`.
- No LLM.

### Stage 3 — Prior VI/AGM/EGM Download
- **In:** ticker + year, `years_back=5`, legacy codes.
- **Process:** scrape the ASA company page (`voting-intentions` +
  `meeting-reports` sibling divs).
- **Out:** `vi_reports/{T}_{YYYY}_VIR.pdf` / `_AGM.pdf` / `_EGM.pdf`
  (not every type is published every year; missing years are tolerated) +
  `vi_reports_summary.json`.
- No LLM.

### Stage 4 — Financial Performance Table + CEO Pay
- **In:** all `annual_reports/*.pdf` via `ar_text_cache` (cached, unusable pages
  dropped); models `PERFORMANCE_MODEL` / fallback.
- **Sub-steps:** 4a 5-year table (latest AR) → 4b share-price cross-check →
  4c fill missing years from individual ARs → 4d CEO realised remuneration
  (disclosure-first, one call per AR year) → step 5 compile/validate.
  CEO-pay responses are checked by the currency/scale + disclosure validators
  (see Validation layer below).
- **Out:** `extracted_json/performance_history_5yr.json`
  (`years{...}`, `ceo_realised_pay{...}`, `audit_notes`).

### Stage 7.5 — News Prefetch
- **In:** company name (NoM JSON; ticker fallback), `TAVILY_API_KEY`, fixed
  concern queries (~12 months). No LLM.
- **Out:** `supporting_data/stage7_5_news_prefetch/news_search_results.txt`
  (+ `news_search_raw.json`, metadata/status, copy in `reports/`).

### Stage 5 — Remuneration Assessment
- **In:** current AR (`STAGE5_SPECS`, 250k), prior AR (120k), NoM, comparator CSV,
  news block; `prompts/rem_assess.txt`.
- **Process:** LLM assessment vs ASA guidelines (pay-for-performance, realised
  remuneration, malus/clawback, comparator positioning).
- **Out:** `reports/{T}_{Y}_Remuneration_Assessment.md` and
  `extracted_json/{T}_{Y}_Remuneration_Assessment.json`. Citations existence-
  checked; a false `not_disclosed` realised-rem answer is flagged by
  `disclosure_check.check_disclosed_realised`.

### Stage 6 — Director Assessment
- **In:** NoM JSONs, AR (`STAGE6_SPECS`), full `data/asa_guidelines/` text, news.
- **Process:** per-director evaluation (independence, tenure, workload,
  shareholding alignment, skills) → recommendation.
- **Out:** `reports/{stem}_Director_Evaluation.md` per notice JSON; success
  moves JSONs to `notices_archived/`.

### Stage 7 — Other-Resolutions Assessment
- **In:** NoM JSON, AR (`STAGE7_SPECS`), news.
- **Out:** `reports/{T}_{Y}_Other_Resolutions_Assessment.json`.

### Stage 7 Audit — Quality Audit + Remediation
- **In:** every prior output + `state.json` + news (supplementary).
- **Process:** automatic checks + LLM audit (0–100); loop audit → remediation →
  re-audit (≤2 cycles) when below `AUDIT_PASS_THRESHOLD` (75).
- **Out:** `reports/{T}_{Y}_Audit_Report.json`, cycle artifacts.

### Stage 8 — Text Summaries
- **In:** perf JSON + Stage 5/6/7 outputs + news (each degrades to "*Not available*").
- **Out:** `reports/{T}_{Y}_Analysis_Section.md`.

### Stage 9 — Board Questions
- **In (documented inventory):** 1 current AR, 2 CGS, 3 remuneration output,
  4 other-resolutions, 5 news, 6 prior VI report, 7 prior AGM report.
- **Out:** `reports/{T}_{Y}_Board_Questions.md` (10 questions + rationales).

### Stage 10 — Word Report Rendering
- **In:** `word_templates/VI_Report_Blank.docx` + NoM, perf JSON, Stage 5/7/8/9
  outputs, news.
- **Out:** `reports/{T}_{Y}_VI_Report.docx` (the deliverable), metadata/summary.
- No LLM.

## Available sources vs. consumers

Sources are produced by the fetch stages (0–3, 7.5) and shared among the
assessment stages. The table shows which source exists and which stages
actually read it.

| Source | Produced by | Consumed by | Status |
|--------|-------------|-------------|--------|
| NoM JSON | 1 | 5, 6, 7, 8, 9, 10 | ✓ wired |
| Current AR text | 2 (cache) | 4, 5, 6, 7, 9 | ✓ wired |
| Prior ARs (5 yr) | 2 | 4 (5-yr), 5 (prior-yr) | ✓ wired |
| ASA guidelines PDF | shared | 6 (`load_asa_guidelines`) | ⚠ 5 & 7 use prompt-embedded text, not the PDF |
| Comparator CSV | shared | 5 (+ `currency_anchor` reporting currency) | ✓ wired |
| News block | 7.5 | 5, 6, 7, 7A, 8, 9, 10 | ✓ wired |
| **Standalone CGS** | 2 → `governance/` | **none** | ✗ parsed by no stage (Stage 9 says "within AR") |
| **Prior VI report** | 3 → `vi_reports/*_VIR.pdf` | **none** (9 detects only) | ✗ not parsed |
| **Prior AGM/EGM report** | 3 → `vi_reports/*_AGM.pdf` | **none** (9 detects only) | ✗ not parsed |

## Source-gap review (inputs missing sources that now exist)

Adding the download stages exposed documents that no assessment stage yet
consumes. These are the concrete gaps to close:

1. **Standalone CGS → Stage 9 doc 2 (and Stage 6).** Stage 2 downloads
   `governance/{T}_{FY}_CGS.pdf`, but `_build_board_questions_payload`
   (src/board_questions.py:331) always writes "Included within the Annual Report
   text above" and never loads the standalone PDF. When a standalone CGS exists
   it should be extracted and supplied as DOCUMENT 2 (with the AR-inlined text as
   fallback). Stage 6 (director independence/skills) retrieves those sections
   from the AR already, so the standalone CGS is a secondary input there — Supply
   it to Stage 9 first.

2. **Prior VI report → Stage 5 REM-M03 and Stage 9 doc 6.** Stage 3 downloads
   `vi_reports/*_VIR.pdf`, but Stage 5's
   `prior_asa_report` input flag is hard-coded `False` (src/remuneration_assess.py)
   even though the Stage 5 prompt lists the "prior ASA Voting Intentions/AGM
   report" as *"also needed for REM-M03"* (prior issues resolved). Stage 9 doc 6
   only globs `*_VIR.pdf` to report availability and never adds the report text.
   Both should load the prior VI report text (scoped to the remuneration /
   prior-issues sections).

3. **Prior AGM/EGM report → Stage 9 doc 7.** Stage 3 downloads `*_AGM.pdf`
   (and `*_EGM.pdf`); Stage 9 doc 7 only globs `*AGM*` for the availability note.
   The AGM report text (prior-year meeting outcomes) should be parsed into
   DOCUMENT 7.

4. **ASA guidelines single-source-of-truth (consistency, not a data gap).**
   Only Stage 6 loads `data/asa_guidelines/`; Stages 5 and 7 embed the guideline
   text in their prompts. If the PDFs are authoritative, Stages 5/7 drift from
   6 when the PDFs change. Consider loading the same PDF text for 5/7 (or state
   explicitly that 5/7 pin a frozen prompt version).

Lower-priority / not a gap:
- **`peer_reports` (Stage 5) is hard-coded `False`** and is correct today: peer
  company remuneration reports are not downloaded anywhere; the comparator CSV
  fills the peer-quantum role instead.

## Validation layer (not a stage, applies to 4/5)

- `currency_anchor.py` — per-row currency + scale resolution (currency is
  per-row in dual-listed issuers: RIO p.138 carries A$ and GBP in one column).
- `disclosure_check.py` — proves a *disclosed* realised row exists before
  `not_disclosed` is accepted; `check_disclosed_realised` flags the prose-level
  false `not_disclosed` in Stages 4/5.
- `citation_check.py` — `[p.N]` citations reference supplied pages (existence
  only, not accuracy).
- Checkpointing — `supporting_data/stage4_performance_table/pipeline.db`
  (SQLite: `extraction_checkpoints`, `call_log`, `facts`, `pages`); resume is
  keyed on a deterministic `run_id` = workspace name.
- Gold set — `tests/gold_set.json` (hand-verified CEO pay cells, pending human
  sign-off) is the ground truth the Stage-4 validators are scored against.