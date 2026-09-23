# Session Summary: ASA Voting Intentions Report Pipeline — Architecture Review & Stage 4 Diagnosis

**Purpose of this document:** a record of what was discussed and decided in this
conversation, for Robert's own project reference. This is a plain summary, not a
build instruction — the actual work order given to KiloCode is in
`kilocode_instructions_stage4_reliability.md`.

---

## 1. The project, in brief

An automated pipeline for producing ASA Voting Intentions Reports, triggered by the
release of a company's Notice of Meeting (NoM). Orchestrated by a Python script that
calls a mix of Python code and LLM APIs (via OpenRouter) across a 10-stage workflow
plus an audit stage, concluding in a Word document draft for human review.

**Stages:**
1. NoM Extraction
2. Annual Reports Download (last 5)
3. VI Reports Download (optional, last 5 if they exist)
4. Financial Performance Table (5-year history of key metrics)
5. Remuneration Report Assessment (For/Against)
6. Director Election Assessment (For/Against)
7. Other Resolutions Assessment (For/Against)
   - **Stage 7 Audit** — data quality audit (not yet implemented)
8. Text Summaries from Annual Reports
9. Board Questions Generator (separate from the VI Report itself)
10. Word Report Rendering

**Status:** Stages 1–4 and 6 are complete and working. The rest is in progress.

**Tooling:** KiloCode inside VS Code as the coding assistant; OpenAI-compatible API
via OpenRouter for pipeline LLM calls; models are configured per-stage via a
top-level `.env` file; prompts live as separate `.txt` files in their own
project-level directory.

**Scale and timing:** ~300 ASX companies' AGMs per year, unevenly spread with a
reporting-season peak around August (many companies have a 30 June financial
year-end). Stage 1 polls daily for new NoM releases. Currently tested one company
at a time; the intended design is for the orchestrator to fork a separate
processing stream per company when multiple NoMs land on the same day.

**End goal:** reach enough confidence in the pipeline to produce complete reports
without human intervention. This would enable (a) backfilling reports for companies
not previously monitored, and (b) building a queryable historical database from
extracted report data, across both new and old reports.

---

## 2. Architecture assessment (overall)

The staged pipeline, per-company workspace structure, append-only technical log, and
`state.json`-based stage tracking are all sound design choices — this is a standard,
well-suited pattern for this kind of multi-stage, resumable workflow. Points raised
for ongoing attention as the build continues:

- **Resumability:** confirmed already handled — `state.json` stores enough to skip
  completed stages on rerun.
- **Audit placement:** rather than relying solely on the single Stage 7 Audit,
  lightweight validation (does output parse, are required fields present, are
  values in sane ranges) should be added at every stage boundary going forward,
  built in as each stage is written rather than retrofitted later. For the stages
  already complete (1–4, 6), this can be added as a quick follow-up rather than a
  rewrite.
- **Human review point:** currently sits at the very end (pipeline produces a draft,
  a person reviews/approves it). A more formal document tracking/approval workflow
  around this step is a deliberately deferred, later-phase idea — not part of the
  current build.
- **Per-stage model selection:** already implemented via `.env` configuration —
  good practice, since risk/accuracy needs differ a lot between extraction stages
  and the vote-assessment stages.
- **Concurrency/scale:** because the 300-companies figure is an annual total with an
  August peak rather than a single batch, the actual concurrency requirement is
  "handle a burst of same-day NoMs," not "handle 300 at once." This has two design
  implications for later: (a) the daily NoM-detection step needs its own
  "already-processed" registry, separate from per-company `state.json`, so re-running
  the daily check doesn't reprocess an already-actioned announcement; (b) if/when the
  orchestrator forks concurrent per-company streams, LLM API rate limits (RPM/TPM)
  are shared across all concurrent streams and need a shared limiter, not just
  per-pipeline retry logic. Not part of the current work order — flagged for later.
- **Schema stability:** because of the long-term database goal, Stage 4–7's JSON
  output schemas are worth treating as durable data structures (not just scratch
  files for the Word doc) and versioning deliberately (e.g. a `schema_version`
  field) so future backfilling doesn't require reconciling incompatible old and new
  formats.

---

## 3. Diagnosis of the `technical_log.md` test run (RIO)

Two distinct problems were identified in Stage 4 (Financial Performance Table),
which together accounted for most of that stage's ~48-minute runtime:

### 3a. JSON reliability failure (the more urgent fix)

`prompt_1a` (primary single-pass 5-year table extraction) and `prompt_4` (final
compile/validation) both failed with `LLM returned str, expected JSON object` —
using a paid model (`google/gemini-3.1-flash-lite`), not a free one. This confirms
the JSON-reliability problem first seen with free OpenRouter models has persisted
after switching to paid models; it's a structured-output enforcement issue, not
purely a "cheap model" issue. This is consistent with earlier testing that found
only ~33% JSON reliability across a range of free models.

The `prompt_1a` failure specifically triggered an expensive fallback path
(Stage 4c gap-fill), which re-scans all 5 years' annual reports from scratch for
financial metrics — work that would likely be unnecessary if the primary extraction
had succeeded.

**Direction agreed:** enforce strict JSON-schema output (not just loose JSON mode)
on these calls where the provider supports it, and add a validate-then-reask retry
loop as a backstop for every JSON-producing call in Stage 4, so a fixable parsing
hiccup doesn't immediately trigger the expensive fallback.

### 3b. Redundant PDF scanning (the compounding inefficiency)

Separately from the JSON bug, the same annual report PDFs were being opened and
scanned page-by-page multiple times within a single run — once for financial-metric
keywords, again for remuneration keywords. Clarified during the discussion: this
second scan (Stage 4d, CEO remuneration) is **not** an error-path artifact — it runs
on every company, every year, regardless of whether the Stage 4a shortcut succeeds,
because CEO pay is a mandatory disclosure but isn't part of the optional 5-year
summary table that Stage 4a looks for.

**Direction agreed:** build a persistent, cross-run text cache for each annual
report — extracted once, keyed by ticker + fiscal year (plus ideally a content
hash), stored at the project level (not inside the per-company-year workspace) so
next year's report cycle can reuse this year's extractions rather than re-scanning
the same static documents. This was judged the higher long-term-value fix of the
two, since the remuneration scan's cost recurs on every single run indefinitely,
while the JSON-reliability fix only removes a worst-case cost.

### 3c. On RAG

Confirmed that a full RAG system (vector database, chunking, embeddings across an
open-ended corpus) is not warranted here — the problem is narrower than what RAG is
built for (a single known document, not retrieval across many unknown ones). A
lighter "RAG-lite" approach — page-relevance scoring against the already-cached
text, to select ~8–12 pages instead of 20–50 per LLM call — was discussed as a
future refinement, best prototyped first on the remuneration extraction specifically
(Stage 4d), since it runs unconditionally on every company/year and covers a
fairly well-defined section of each AR.

---

## 4. Tooling note: KiloCode model choice

Robert currently uses the free "NVIDIA Nemotron 3 Ultra" model within KiloCode for
coding assistance, and asked whether four other free models on offer (Dots3-Note
Preview, Nex-N2.5-Pro, Laguna S 2.1, Ling 3.0 Flash VL) might perform better.
Comparison found Nemotron 3 Ultra scores competitively against these alternatives
on general intelligence/agentic benchmarks and has direct validation within Kilo
Code specifically — no change recommended, with a practical note to give it more
explicit, spelled-out instructions on expected output format, as it's been observed
to be terse and less likely to ask clarifying questions.

For the pipeline's own JSON-reliability problem (a separate question from the
KiloCode assistant model), the recommendation was to continue with `gpt-4o-mini`
(already found to work acceptably) and specifically confirm use of OpenAI's strict
Structured Outputs mode, rather than switching models — with DeepSeek V4 Flash and
GLM-5.3-Flash flagged as budget-fitting alternatives worth A/B testing later
(cost ceiling: <$0.30/1M input tokens, <$2/1M output tokens).

---

## 5. Output of this session

A detailed, priority-ordered work order was produced for KiloCode, covering:
strict JSON schema enforcement + retry logic (Priority 1), the persistent AR text
cache (Priority 2), refactoring Stage 4 to use that cache (Priority 3), adopting
per-stage validation going forward (Priority 4), and a narrowly-scoped RAG-lite
prototype for later (Priority 5) — with an explicit "out of scope for now" list
covering concurrency/fan-out design, cost tracking, confidence scoring, and the
document approval workflow, so those ideas aren't accidentally built ahead of when
they're actually needed.

See: `kilocode_instructions_stage4_reliability.md`
