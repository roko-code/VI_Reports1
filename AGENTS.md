# AGENTS.md - Kilo Code Agent Instructions

## Python Environment
- All Python scripts run from the `src/` directory
- Use `cd src` before running Python commands
- Dependencies are in `requirements.txt` at project root

## Development Commands

### Syntax Check
```bash
cd src && python -c "import ast; [ast.parse(open(f).read()) for f in ['orchestrator.py', 'news_prefetch.py', 'news_search_results.py', 'text_summaries.py', 'board_questions.py', 'report_rendering.py'] if True]" && echo "All syntax OK"
```

### Import Verification
```bash
cd src && python -c "from orchestrator import CompanyPipeline; print('All imports OK')"
```

### Run Single Company Pipeline
```bash
cd src && python orchestrator.py SUN 2026
```

### Run Individual Stage
```bash
# News Prefetch (Stage 7.5)
cd src && python news_prefetch.py SUN

# Text Summaries (Stage 8)
cd src && python text_summaries.py ../data/companies/SUN_2026

# Remuneration Assessment (Stage 5)
cd src && python remuneration_assess.py SUN 2026 "C:\Users\jurok\VI_Reports1\data\companies\SUN_2026"

# Board Questions (Stage 9)
cd src && python board_questions.py ../data/companies/SUN_2026

# Report Rendering (Stage 10)
cd src && python report_rendering.py ../data/companies/SUN_2026
```

Stage modules that take a ticker/year also accept an explicit workspace path.
Omitting it resolves `data/companies/{TICKER}_{YEAR}` relative to the current
directory, which silently does nothing when run from `src/`.

## Project Structure
- `src/` - Python modules for each pipeline stage
- `prompts/` - LLM system prompts for each assessment
- `data/_reference/` - Reference data (comparator CSV, Word templates)
- `data/asa_guidelines/` - ASA guideline PDFs
- `data/_cache/` - AR text cache
- `data/companies/{TICKER}_{YEAR}/` - Per-company workspace

## Key Patterns
- All stage modules expose `run_pipeline(workspace_dir, supporting_data_dir, logger)` 
- Stage modules use `TechnicalLogger` from `logging_utils.py`
- Use `write_supporting_json()` and `write_supporting_text()` for saving intermediate outputs
- Use `ar_text_cache.get_ar_pages()` for AR text extraction (cached)
- Rate limiter: `TokenBucket(20)` (20 RPM) in each LLM-calling module
- Models configured via `.env` stage-specific variables

## Annual Report Retrieval (important)
Annual reports run 150-450 pages. Stages 5, 6, 7 and 9 must **never** concatenate
AR text and cut it to a character budget: the statutory KMP pay table, realised
remuneration and malus/clawback disclosures sit well past any such cut, and the
model responds by citing page ranges it was never shown (observed: 11 of 11 AR
page citations in `SUN_2026_Remuneration_Assessment.md` pointed outside an
80,000 character context, and the audit passed them).

Instead use `ar_retrieval.build_context(pages, STAGE_N_SPECS, budget_chars)`:

```python
from ar_retrieval import STAGE5_SPECS, build_context, write_manifest
result = build_context(ar_pages, STAGE5_SPECS, budget_chars=AR_BUDGET_CHARS, logger=logger)
write_manifest(result, supporting_data_dir, "stage5", ticker, year, logger=logger)
```

- Section manifests (`STAGE5_SPECS`, `STAGE6_SPECS`, `STAGE7_SPECS`, `STAGE9_SPECS`)
  are declarative. Add keywords there rather than widening a page cap.
- Budgets are set by `AR_BUDGET_CHARS` / `PRIOR_AR_BUDGET_CHARS` in `.env`
  (defaults 250k/120k). They bound prompt size, not how much of the report is read.
- Always pass `result.coverage_note()` into the prompt. It tells the model which
  sections were not supplied so unverifiable rules resolve to Uncertain.
- A `retrieval_manifest_stageN.json` is written per stage. Check
  `missing_mandatory` when reviewing a report.

### Regression check
```bash
cd src && python retrieval_selftest.py
```
Asserts every mandatory section is located for every cached report. Currently
all 56 report/stage combinations pass. Run this after touching any retrieval
keyword, budget or extraction change.

### Known data limitation
`RIO_2022` embeds a font with no usable character map: 141 of its 424 pages
extract as `(cid:N)` glyph noise. pypdf, pdfminer and MuPDF all resolve it the
same way, so it cannot be recovered by swapping extractors. `ar_text_cache`
flags these pages `usable: False` and drops the text; `ar_retrieval` refuses to
let them match keywords or consume budget. Any rule depending on those pages
must be reported as Uncertain.

## Validator traps (read before trusting any "pass")
`docs/validator_traps.md` records how each validation control in this repo has
been fooled, with the regression test for each. Three separate defects have been
in the *checking* layer rather than the extraction, so treat a validator pass as
weaker evidence than its pass rate suggests until it has been stress-tested
against a case designed to fool it.

The current currency check is `src/currency_anchor.py`. It resolves currency
and scale **per row**, because currency is a per-row attribute in a dual-listed
issuer's remuneration tables - Rio Tinto FY24 p.138 carries `A$10,476` and
`GBP8,450` in one column, p.140 carries `GBP`/`SGD`/`A$`/`C$` in one STIP
table. Anything that anchors currency at page level is wrong by construction.
Tests: `tests/test_currency_anchor.py`, `tests/test_validator_currency.py`.

Two further shared helpers, both created after a live model got something wrong
that no check was looking for:

- `src/disclosure_check.py` - finds a *disclosed* realised-remuneration CEO row.
  Exists because the model reported `no_realised_disclosure` for NAB FY2024
  while p.145 of that report publishes a realised figure. A terminal
  `not_disclosed` is an escape hatch and must be provable from the pages.
  `tests/test_branch_coverage.py`.
- `src/citation_check.py` - checks that `[p.N]` citations in Stage 5/6 markdown
  refer to pages actually supplied. Existence only, not accuracy; Stages 5 and
  6 emit prose, so verifying a quoted number would need structured fields.

A `not_disclosed` answer must be rejected when a realised disclosure is present
on a supplied page. And a part-year CEO is a qualifier on a real figure, not a
reason to withhold one: NAB FY2024's answer is $1.74m with `ceo_served_part_year`
set, not a non-disclosure.

## Performance table facts (Stage 4)

Four things in the performance table are not what their names suggest, and
each was wrong in a live AMP FY2026 report before these rules were added.

- **An AR published in year Y covers FY Y-1.** `AMP_2026_AR.pdf` is FY2025.
  Stage 4 resolves the AR for a fiscal year as publication year + 1 and falls
  back to the following year when the issuer publishes on a different cycle.
- **`dividend` is the dividend DECLARED for the FY.** A five-year summary
  reports dividends *paid during* the year, which is a different figure
  (AMP FY2025: 4.0c declared, 3.0c paid). `dividend` is taken from the
  individual AR's Appendix 4E; `dividend_paid` keeps the summary row and is
  what the TSR uses.
- **Simple TSR is a total shareholder return.** It includes the dividends paid
  during the year, because that is how issuers define TSR in their own
  glossary (AMP FY2025 AR p.173). A price return understates it by the
  dividend yield. `share_prices.compute_simple_tsr(closes, dividends)`.
- **EPS is read from the column header, not from the model.** An AR prints the
  current year and the comparative side by side; AMP FY2024 was reported as
  9.3c, which is FY2023's number. `src/eps_extraction.py` keys the value to
  the year column and wins over the model's reading.

`src/remuneration_framework.py` does the same for CEO pay and the remuneration
framework: it reads the realised-pay and STI-outcomes tables and the
percentage-of-fixed-remuneration rules out of the AR, and rejects the result
unless it reconciles with the remuneration mix the issuer publishes. Stage 4
uses it to fill CEO realised and statutory pay when the model call is dropped
or answers "not disclosed".

Tests: `tests/test_eps_extraction.py`, `tests/test_remuneration_framework.py`,
`tests/test_share_prices.py`.

### Running Stage 4
```bash
python run_stage4_fresh.py AMP 2026
```
Stage 4 is checkpointed, so a re-run resumes from the last completed model
call. The checkpoint database lives under the workspace's
`supporting_data/stage4_performance_table/`, so the supporting data directory
must be supplied - invoking `performance_table.py` directly skips
checkpointing.


