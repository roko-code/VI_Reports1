# Session Summary: ASA Voting Intentions Report Pipeline — Architecture 

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

See: `kilocode_instructions_stage4_reliability.md`
