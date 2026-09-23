# KiloCode Work Order: Stage 4 Reliability & Efficiency

**Context for KiloCode:** This work order is based on a diagnosis of `technical_log.md`
from the most recent test run (ticker: RIO, run date 2026-09-16). That log revealed
two distinct problems in Stage 4 (Financial Performance Table) — one reliability bug
and one efficiency/architecture gap. Do not attempt to fix these by guessing; ask the
user (Robert) for the relevant source files before making changes if anything below
is ambiguous. Robert is not a programmer and wants to stay in control of design
decisions — flag any non-trivial design choice back to him rather than silently
picking one.

Work through the priorities in order. Priorities 1 and 2 are the ones that matter
most; 3 builds on 2; 4 is an ongoing habit to adopt from now on; 5 is a later-phase
idea to prototype narrowly, not to build out fully yet.

---

## Priority 1 (fix first): Stage 4's JSON output reliability

### What's broken, evidenced in the log

Two LLM calls in Stage 4 fail with the same error:

```
[INFO] [prompt_1a] success: model=google/gemini-3.1-flash-lite ... tokens=27542 cost=0.00812925
[INFO] LLM call 1a took 6.0s, response: 2645 chars
[ERROR] Prompt 1a failed: LLM returned str, expected JSON object
```

```
[INFO] [prompt_4] success: model=google/gemini-3.1-flash-lite ... tokens=1362 cost=0.0011155
[ERROR] Prompt 4 failed: LLM returned str, expected JSON object
```

Note that the API call itself succeeded (billed, response returned) — the failure is
in the *parsing/validation* step after the response comes back. The model returned a
string that our code expected to be a JSON object (or the JSON was wrapped in prose /
a markdown code fence and our parser didn't unwrap it).

### What to build

1. **Locate the call sites for `prompt_1a` (Stage 4a, single-pass 5-year table
   extraction) and `prompt_4` (Stage 4e, compile & validate performance table).**
   Show Robert the current request-construction and response-parsing code for both
   before changing anything, so he can confirm what's actually being sent today.

2. **Switch both calls to enforce a JSON schema on the model output, not just ask for
   JSON in the prompt.** Concretely:
   - If calling an OpenAI-compatible endpoint (including via OpenRouter) that
     supports it, use `response_format: {"type": "json_schema", "json_schema": {...},
     "strict": true}` rather than `{"type": "json_object"}` or no `response_format`
     at all. Strict schema mode constrains generation so the model cannot emit
     anything except JSON matching the given schema — this is a stronger guarantee
     than "JSON mode," which only biases the output.
   - Define an explicit JSON Schema for what `prompt_1a` and `prompt_4` are each
     expected to return (field names, types, required fields). If a schema doesn't
     already exist in the codebase for these two prompts, write one and show it to
     Robert before wiring it in.
   - Confirm whether `google/gemini-3.1-flash-lite` via OpenRouter actually supports
     strict schema enforcement end-to-end. If it does not (or support is unreliable
     through OpenRouter specifically), fall back to the retry-and-reask pattern in
     step 3 as the primary defence for this model, and flag this limitation to
     Robert — it may factor into his model choice for these two calls.

3. **Add a validate-then-reask retry loop around every JSON-producing LLM call in
   Stage 4** (not just these two — apply the same pattern to `prompt_2` and
   `prompt_3` as well, since they are structurally the same kind of call):
   - After receiving a response, attempt to parse/validate it against the expected
     schema.
   - If it fails, do **not** immediately fall through to a fallback/gap-fill path.
     Instead, send a follow-up message to the model containing (a) its own prior
     response and (b) the specific validation error, and ask it to return corrected
     JSON only. Retry this once or twice (make the retry count configurable) before
     giving up.
   - Only after retries are exhausted should the code fall through to whatever
     failure/fallback behaviour exists today.
   - Log each retry attempt distinctly in `technical_log.md` (e.g.
     `[WARN] prompt_1a: JSON validation failed, retrying (attempt 1/2)`) so future
     log review can distinguish "succeeded on retry" from "failed outright."

4. **Do not change what happens after a genuine, final failure** (i.e. after
   retries are exhausted) without checking with Robert first — the current fallback
   behaviour (triggering Stage 4c gap-fill) is intentional and should stay as the
   last resort, just not the first response to a fixable parsing hiccup.

---

## Priority 2 (build next): Persistent, cross-run annual report text cache

### The problem this solves

The log shows the same annual report PDF (e.g. `RIO_2022_AR.pdf`, 424 pages) being
opened and scanned page-by-page **multiple times** within a single run, for
different keyword lists:
- Once during Stage 4c gap-fill (financial metrics keywords)
- Again during Stage 4d (remuneration/CEO pay keywords)

Each full-document scan took anywhere from ~100 to ~580 seconds in the log. This
repeated scanning is the single biggest driver of Stage 4's ~48-minute runtime in
the test run, and — separate from the JSON bug in Priority 1 — the remuneration
scan (Stage 4d) happens on **every run, every year, regardless of whether anything
else fails**, because CEO remuneration is not part of the optional 5-year summary
table and must always be sourced from each individual year's AR.

### What to build

1. **Extract each annual report's full text exactly once, cache it, and have every
   Stage 4 sub-stage read from the cache instead of re-opening the PDF.**
   - Within a single run: if `RIO_2022_AR.pdf` needs to be scanned for both
     financial-metric keywords (4c) and remuneration keywords (4d), extract its
     text once and run both keyword searches against the cached text.
   - Preserve page numbers in the cache structure (e.g. a list of `{page_number,
     text}` objects), since downstream slicing logic depends on page numbers.

2. **Make the cache persistent across runs, not just within one script invocation.**
   Store it at a **project level**, keyed by ticker and fiscal year (and ideally a
   content hash or file size/modified-date check, so a reissued/corrected AR can
   force a fresh extraction) — for example:
   ```
   data/_cache/annual_report_text/{TICKER}_{FY}.json
   ```
   This is deliberately **outside** the per-company-year workspace
   (`data/companies/{TICKER}_{YEAR}/`), because the underlying AR document doesn't
   change from one year's report cycle to the next — `RIO_2022_AR.pdf` extracted
   during the `RIO_2026` run should be directly reusable when `RIO_2027` needs
   FY2022–2026 history again next year.

3. **Update Stage 4a, 4c, and 4d to check this cache before attempting any PDF scan**,
   and only fall through to actually opening/parsing the PDF (and writing to the
   cache) on a cache miss.

4. **Do not commit this cache to git.** It's fully regenerable derived data — treat
   it like the downloaded PDFs themselves (presumably already gitignored). Confirm
   with Robert where cache files should physically live if disk space or backup
   location matters.

5. **Note for Robert, not necessarily for KiloCode to act on unprompted:** this
   cache is also the natural first building block toward the longer-term goal of a
   queryable historical database across companies — worth keeping the cache's JSON
   structure clean and stable rather than throwaway, since it may get reused for
   more than just Stage 4 later.

---

## Priority 3 (depends on Priority 2): Refactor Stage 4's scan/slice calls to use the cache

Once the cache from Priority 2 exists, go through Stage 4a, 4b, 4c, and 4d and
replace their current "open PDF → scan page-by-page → slice pages" logic with
"load cached page text → keyword search against cached text → slice from cached
text." The keyword lists and page-selection logic themselves should not need to
change — only the source of the page text.

Confirm with Robert once this is done that runtime for a repeat run against
already-cached ARs has dropped substantially, as a sanity check that the caching
is actually being hit rather than silently bypassed.

---

## Priority 4 (ongoing habit from now on): Validate at every stage boundary, not just at the end

Robert and I discussed that the existing single audit stage (Stage 7 Audit) sits
after Stages 5, 6, and 7 have all already run on whatever upstream data they were
given — so a data-quality problem introduced earlier (e.g. in Stage 4's output)
isn't caught until three expensive assessment stages have already run against it.

Going forward, as you (KiloCode) build or touch any stage that produces structured
JSON output:
- Add a lightweight validation pass immediately after that stage's output is
  produced: does it parse, are required fields present and non-null, are numeric
  fields in sane ranges (e.g. not negative where a positive value is expected).
- This does not replace the more substantive Stage 7 Audit (which is not yet
  implemented, per the log: `Stage 7 Audit ... Not yet implemented`) — it's a
  cheaper, earlier check that should stop a bad output from silently propagating.
- For Stages 1–4 and 6, which Robert says are already complete and working, this
  can be added as a fast follow-up rather than a blocking rewrite — a thin
  validation function bolted onto existing output, not a redesign.
- For Stage 7 itself (still to be implemented) and Stages 8, 9, and 10 (not yet
  built), build this validation in from the start as part of writing the stage,
  not as a separate pass afterward.

---

## Priority 5 (prototype only — do not build out fully yet): "RAG-lite" page relevance ranking

This is a future-phase idea Robert wants to explore, not a full build request yet.
If and when Robert asks for this specifically:

- The goal is to replace "select every page containing any keyword match" (which
  currently pulls 20–50 pages / 100,000–250,000+ characters per prompt) with a
  smaller, better-ranked set of pages — e.g. the top 8–12 pages by relevance —
  using either keyword density scoring or a lightweight embedding-similarity
  comparison against a short description of the target content.
- This does **not** require a vector database, a chunking framework, or any of the
  infrastructure associated with full RAG — a simple scoring pass over the
  already-cached page text (from Priority 2) computed once per document is
  sufficient at this scale (a handful of known documents, not an open-ended
  corpus).
- **If asked to prototype this, start with the remuneration/CEO-pay extraction
  specifically (the keyword list currently used in Stage 4d)**, since that section
  is fairly well-defined and contiguous within an AR, and because it's the part of
  Stage 4 that runs unconditionally on every single company/year — meaning it's
  both the easiest target to validate against and the one with the most
  repeated payoff.
- Do not extend this to other stages or generalize it into a shared library until
  it's been proven out on this one case and Robert has reviewed the results.

---

## Explicitly out of scope for this work order (do not build)

To avoid scope creep, the following were discussed in the broader conversation but
are **not** part of this work order and should not be built without a separate,
explicit request from Robert:

- Any change to the daily NoM-detection trigger (Stage 1) or a fan-out/concurrency
  model for handling multiple same-day NoMs. This is a real future requirement
  (particularly around the August reporting-season peak) but is a separate design
  effort, not part of fixing Stage 4.
- A dedicated per-company-run cost/token-usage tracking file. Worth having
  eventually; not required for this work order.
- A confidence-scoring field added to every stage's JSON output, in support of the
  longer-term goal of unattended report generation. Worth designing in when a stage
  is next touched substantively, but not a reason to open already-working stages
  now.
- Any document tracking/approval workflow around the human review step at the end
  of the pipeline. Robert has explicitly deferred this to a later project phase.

---

## Suggested order of work

1. Show Robert the current `prompt_1a` / `prompt_4` request and parsing code.
2. Implement strict JSON schema enforcement + validate-then-reask retry loop
   (Priority 1) for `prompt_1a`, `prompt_2`, `prompt_3`, and `prompt_4`.
3. Re-run the same RIO test case and confirm the `LLM returned str, expected JSON
   object` failures are gone (or, if the underlying model can't support strict
   schema mode, confirm the retry loop is recovering from them).
4. Build the persistent AR text cache (Priority 2) and wire Stages 4a/4c/4d to use
   it (Priority 3).
5. Re-run the same RIO test case again and compare Stage 4's total runtime against
   the original ~48-minute baseline in `technical_log.md`.
6. Report back to Robert with before/after timings and the updated log before
   proceeding to implement Stage 7 Audit or later stages.
