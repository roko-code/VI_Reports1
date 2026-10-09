# Known ways the validator can be fooled

Read this before trusting any validation pass, self-test, or cross-check in this
pipeline. Every entry below was found the hard way, and in each case the defect
was in the *checking* layer, not in the extraction. That is a property of this
setup rather than a coincidence: a validator is code that has never been
adversarially tested against the specific failure it claims to catch.

Treat a "pass" as weaker evidence than its pass rate suggests until the specific
validator has been stress-tested against a case designed to fool it. Add to this
list whenever a new one turns up.

---

## 1. Digit matching is currency-blind

`_page_contains_value(t, 8.45)` matches `8,450` in a page headed
`Single total figure of remuneration (GBP'000)` and reports a pass. The digits
match; the currency does not. The figure is roughly 2x wrong once converted, and
the check waved it through.

Compounding this: in a dual-listed issuer the currency is a **per-row**
attribute, not a per-page one. RIO FY24 p.138 puts `A$10,476` and `GBP8,450`
in the same column; p.140 puts `GBP941`, `SGD981`, `A$1,188`, `C$258` in a
single STIP table. Any currency logic anchored at page level is wrong by
construction.

**Test that would have caught it:** assert a `$m` claim is rejected when the
matching row is anchored to a non-AUD currency, even though the digits match.
Covered by `tests/test_currency_anchor.py` section 2 and
`tests/test_validator_currency.py` section A.

### 1a. Trying every scale until one matches

The check this replaced did not only ignore currency. It tested each printed
number against `printed/1e3`, `printed/1e6` and `printed` and accepted on
whichever matched. So a page declaring `(\u00a3'000)` could satisfy a claim by
way of a units reading, or a `m` reading, with no reference to what the page
actually said. Currency *and* declared scale must both come from the source.

### 1b. Rejecting every bare `$` breaks every ASX filer

The first currency fix marked an unqualified `$` ambiguous and refused it. That
would reject SUN, NAB and every other Australian issuer, whose reports write
`$` for AUD throughout - including the case that was already correct. A bare
`$` now resolves via an explicit `bare_currency="AUD"`, read from the
comparator's `Currency Future Rem` column rather than assumed, so an ASX filer
that reports in USD is not silently tagged AUD. It is recorded as `assumed`.

### 1c. "No scale found" is not "scale confirmed"

The first version set `scale_from = "declared"` when no scale could be resolved
at all, so `scale_is_declared` returned True for unlabelled figures. That
inversion silently excluded exactly the near-misses the validator exists to
report. Unresolved scale is now a distinct state, never a confirmation.

### 1d. The scale detector matched the "000" inside a number

`_scale_of` searched each line for a thousands marker. A data row such as
`Andrew 2025 2,500,000 1,035,000 3,535,000 7,566,768` contains "000" four times
inside comma-grouped figures, so the page registered as declaring a
thousands scale. Every row on it was then normalised 1000x wrong, and pages
that declared nothing at all looked as though they had. Found by forcing the
`figure_fy` branch, which had never fired.

Numbers are now stripped before the pattern is applied.

### 1e. A terminal "not disclosed" is an escape hatch, and it was used

The live model reported `no_realised_disclosure` for NAB FY2024. p.145 of that
same report is a voluntary non-statutory realised-remuneration table whose CEO
row reads `Andrew Irvine (for part year) 2024 1,237,544 506,250 1,743,794`.
The page was in the retrieved slice. The figure was disclosed and the stage
reported that it was not.

Two causes, both mine. I inferred NAB's shape from the *statutory* table on
p.151 without reading the *realised* table beside it - the same
inference-from-a-statistic error as trap 5. And I had listed `part_year_ceo`
as a valid `not_disclosed` reason, which is a trap: a part-year CEO still has a
disclosed figure, so the reason offered the model a way to withhold one that
exists. NAB FY24's answer is $1.74m with a part-year caveat, not a
non-disclosure.

`not_disclosed` is now checked against the supplied pages: if a realised CEO
row is present, the answer is rejected and the page is named.
`part_year_ceo` is out of the enum, replaced by a `ceo_served_part_year` flag
that qualifies a real figure.

### 1f. Refusing every undeclared scale made a third of the corpus unverifiable

The first attempt required a *declared* scale to accept a match. NAB publishes
its realised tables in full dollars with no scale marker, so p.145, p.151 and
p.121 could never validate - four of fourteen reports permanently unverifiable,
which would have surfaced as hard failures rather than as anything useful.

Undeclared scale is now inferred, but only under a constraint: both the units
and thousands readings are computed, and only a reading that lands inside
CEO_TOTAL_BAND_M counts. `7,566,768` is 7.57m or 7,566.8m - units wins. A share
count of `95,363` is 0.10m or 95.4m - neither is a plausible CEO total, so
nothing is accepted. Where both readings are plausible the figure is left
unresolved rather than guessed.

Note this is *not* trap 1a again. There, the code tried every scale until one
matched the claim. Here the claim is not consulted: the band is a prior about
CEO pay, and a unique hit is required.

The first version set `scale_from = "declared"` when no scale could be resolved
at all, so `scale_is_declared` returned True for unlabelled figures. That
inversion silently excluded exactly the near-misses the validator exists to
report - NAB FY24 p.151 prints `2,982,645` with no scale anywhere, and the
validator had nothing to say about it. Unresolved scale is now `unknown`, and
a claim is tested against both a units and a thousands reading so the rejection
can explain itself.

## 2. Substring token lists match ordinary prose

A page-context rule meant to detect "this page is about remuneration" used a
substring list containing `pay` and `equity`. Those match `payments` and
standard accounting text, so the rule silently never fired — a check that
appears to exist and does nothing.

**Always match on word boundaries** (`\bpay\b`), not substrings. If a rule has
never actually rejected anything, you do not know it works.

## 3. A cross-check that agrees is not a cross-check

The advisory `cross_check_calculated_m` was expected to provide independent
corroboration. In practice the model computed the component sum, got the same
answer as the disclosed figure, and reported `delta 0.0` with
`explained_by: "unrelated"`. It agreed because it was not independent.

A pass rate built on agreement between two values that share an author measures
nothing. Independent corroboration requires a different derivation, not a second
copy.

## 4. The verification script is itself unverified

SUN FY25 was reported as a false attribution — a 6.33m figure on a page the
checker believed held no remuneration content. The original check searched for
caption and values on the same line; p.72 is a wide table where they are on
separate lines, so the check found nothing and concluded the page was empty.

The page was not empty, and that was the second half of the error. p.72 is
captioned **"Executive statutory remuneration"** under "9. Statutory
disclosures" and prints `6,325`. The figure this stage wants is on p.67,
captioned **"FY25 actual remuneration realised"**, which prints `7,783` for
Steve Johnston against a target opportunity of `7,481`.

A verification script that reports a negative result is asserting a fact about
the document. Confirm the script can return a positive on a known-good case
before believing a negative.

## 4a. Statutory is not realised, and I keep reading the wrong table

This has now happened three times, on three different reports, and the third
time I wrote the wrong answer into this file as though it were settled:

| Report | Statutory table | Realised table | Wrong value taken |
|---|---|---|---|
| NAB FY24 | p.149 `2,801` | p.145 `1,743,794` | 2.80 |
| SUN FY26 | p.99 `6,031` | p.94 `6,634` | 6.03 |
| SUN FY25 | p.72 `6,325` | p.67 `7,783` | 6.33 |

In every case the statutory total is *lower* than the realised one, because
realised pay includes vesting of prior-year awards at their value on the vesting
date. So the error is always in the same direction and always looks plausible:
a clean, tidy, slightly smaller number.

Neither table is captioned with the word "statutory" in the flattened text near
the total, so a search for "realised" finds only the realised table and a search
for the CEO's name finds either. Read the caption above the table, not just the
row.

Two of the three were caught only because the model disagreed with my expected
value. A model that is wrong in the same direction as you is invisible; treat a
disagreement with your own ground truth as evidence about the ground truth
first, and only secondarily about the model.

## 5. Statistical rate is not evidence

Retrieval "hit 6/6" and "the tail pages are not relevant" were both inferred
from *why* pages were selected, not from reading them. The inference was wrong:
the truncated tail of RIO FY24 held a ten-year CEO remuneration series.

Never state a document's contents from a selection statistic. Open the pages.

## 6. Scope creep between what a fix does and what it is claimed to fix

Raising Stage 4d's page cap recovered RIO FY24's multi-year CEO remuneration
series (p.138) and pay ratio (p.140). It did **not** change RIO FY24's Stage 5
answer, which remains `year_mismatch` because that report covers FY2023. A
commit message that says "RIO 2024 fixed" would let the next reader infer the
current-FY figure was recovered. It was not.

State separately: what the change fixed, and what it merely improved as a side
effect.

---

## 7. A missing check is worse than a wrong one

Stage 4d had a value-against-page validator. Stages 5 and 6 had nothing at all:
they emit markdown with inline `[p.N]` citations and nothing ever confirmed
those page numbers referred to pages the model was actually given. Every result
those stages have produced is unverified on that axis.

This was initially written up as lower severity than a false pass, on the
reasoning that a missing check cannot admit something wrong. That reasoning is
backwards. A false pass fails loudly the once somebody checks it - and in this
pipeline that has happened three times, because someone did check. A missing
check fails silently, forever, on every result, because nobody is looking.

`src/citation_check.py` closes the existence half for both stages. It does not
close accuracy: verifying that a number quoted beside `[p.145]` appears on
p.145 needs structured output fields, which Stages 5 and 6 do not emit. That
is a larger change and is not attempted.

## 8. A measurement that ran 24 times and produced nothing

The first retry-rate measurement passed `schema=` to `_call_llm`, which does not
accept it. All 24 runs raised `TypeError` and the script reported a confident
0% retry rate across three models and both arms. The tell was in the output:
every row read `failed 4`, `correct 0/2`, and a 0% retry rate with a 100%
failure rate is not a plausible result.

Worse, the same script named its calls `ccy_...`, and `_get_schema_for_call`
resolves the schema by `prompt_3_fy` prefix - so **no schema validation was
running in the earlier live runs either**. The retries observed there were
semantic-validator rejections, not schema-complexity failures. Those are
different things and would have been conflated.

A number that comes back too clean, from a script whose every row also says
"failed", is a measurement failure, not a result.

## 9. A retry rate with no cause attribution measures the harness, not the prompt

The second attempt at this measurement fixed the schema-resolution bug and got
plausible-looking numbers: Full 75% vs Stripped 42%, and gemini-flash-lite at
25% against 100% for both OpenAI models. Those numbers were then used to argue
that schema complexity causes retries, and that cheap models handle it better.

Neither claim survives inspection.

**The causes were never recorded.** The harness counted occurrences of
"retrying" and nothing else, so a genuine complexity tax was indistinguishable
from a bug in the schema under test. It was the latter. Two of my own defects
accounted for most of the Full arm's retries:

- `lti_grant_years_tested` was `{"type": "string"}` while every model returned
  `2019` as an integer. It fired on nearly every run that populated the field.
- `cross_check_explained_by` in `{"unrelated", "not_comparable"}` demanded a
  `cross_check_calculated_m` to exist, though those two values are precisely the
  answers given when there is *nothing* to reconcile. Self-contradictory
  responses were being escalated to hard failures.

Both are pinned by `tests/test_measured_defects.py`.

**The sample size was 4 per cell.** "100%" was 4/4. "25%" was 1/4. Those are
not different rates; they are the same coin landing differently. Reporting them
as percentages invited reading a model-capability ranking into noise.

**The bug was not obviously differential, but that was luck, not design.**
`strip_schema` removed only the three RULE 5 fields and never touched the LTI
field, so that defect was common to both arms rather than inflating Full
specifically. But Full carries a 15,047-character prompt against Stripped's
10,754, and a shorter prompt plausibly populates fewer *optional* fields - so
Full would hit an optional-field type bug more often even when the field itself
is shared. The rerun keeps `lti_grant_years_tested` in **both** arms
deliberately, so that the comparison stays honest about this.

The correct shape for the rerun is: report `k/n` counts, never a bare
percentage; record the classified cause of every retry; and hold the conclusion
until the fixes are in. If a number cannot survive being restated as a count
with its n printed next to it, it is not a result.

### What the post-fix rerun actually showed

Rerun after both fixes, 3 reps x 4 cases = 12 runs per (model, arm) cell, 72
total. Counts, not percentages. Per-run rows: `docs/stage4d_ab_postfix.json`.

| | retried | of | hard fail | correct |
|---|---|---|---|---|
| gpt-4o-mini FULL | 8 | 12 | 2 | 2/9 |
| gpt-4o-mini STRIPPED | 11 | 12 | 5 | 2/9 |
| gpt-4.1-mini FULL | 7 | 12 | 0 | 9/9 |
| gpt-4.1-mini STRIPPED | 7 | 12 | 5 | 4/9 |
| gemini-flash-lite FULL | 1 | 12 | 0 | 9/9 |
| gemini-flash-lite STRIPPED | 3 | 12 | 0 | 9/9 |
| **OVERALL FULL** | **16** | **36** | **2** | **20/27** |
| **OVERALL STRIPPED** | **21** | **36** | **10** | **15/27** |

The pre-fix complexity gap largely closed, and reversed sign: Full went 75% ->
16/36, Stripped went 42% -> 21/36. Fixing `lti_grant_years_tested` removed the
dominant retry driver, which had been inflating Full.

**Two of my earlier claims do not survive this rerun.**

1. *The GBP false pass is prevented by the prompt.* No. It is prevented by
   `currency_anchor`, and it is prevented in **both arms**: 0/9 RIO runs
   published `8,45O` as AUD under Full and 0/9 under Stripped. The validator
   fires on the schema-level value regardless of what the prompt says. Attributing
   that protection to the prompt was wrong, and it is a better outcome than the
   prompt-only story - a validator does not degrade if a model ignores it.
2. *`not_disclosed` is refused only when the prompt explains why.* Also wrong.
   `disclosure_check` fires in both arms; the messages quoted p.145's actual
   `1,743,794` back to the model in Stripped runs just as they did in Full.

**What does survive.** gemini-flash-lite at 1/12 retries and 9/9 correct in
*both* arms, against 7-8/12 for the OpenAI models, is a real difference at
n=12 rather than the 4/4-versus-1/4 noise of the pre-fix run. It also read
SUN FY25 as `7.78` twelve times out of twelve where gpt-4o-mini read the
statutory `6.33` three times out of three.

**What the rerun still failed to deliver.** Per-retry cause attribution. 56 of
68 recorded causes are `unclassified_retry`, because the retry warning line
carries no reason text - only the terminal hard-failure message does. So hard
failures are attributed (`currency_anchor` 8, `crosscheck_contract` 1, other 3)
and retries are not. Instrumenting `_call_llm` itself is the only way to close
this, and it is not done.

**The Stripped arm is not a historical baseline.** It pairs the old prompt and
old schema with the *new* semantic validator, because `_make_ceo_pay_validator`
is passed identically to both arms. So in Stripped runs the validator demands
`not_disclosed_reason` and AUD anchoring that the stripped prompt never asks
for - which is why Stripped hard-fails 10 times to Full's 2. No real
configuration ever ran in that hybrid. It is a valid *controlled* contrast for
"does adding these three schema fields and the RULE 5 block help", and an
invalid one for "what did the code do before".

**An error no validator here catches.** Five runs still read the statutory
total as realised: SUN FY25 `6,33x` where the disclosed realised figure is
`7,783` (3 Full, 2 Stripped). Page grounding passes it, because `6,325` *is*
genuinely printed on the cited page. Distinguishing the statutory table from the
realised table is a caption-family question, not a page-presence question, and
no rule here asks it. See trap 4a.

## 10. Ground truth encoded in the harness is still ground truth

The second run scored SUN FY25 `7.78` as WRONG against an expected `6.33`
hardcoded in the script - the same statutory-for-realised error as trap 4a,
this time baked into the measurement harness where it silently mislabelled the
correct answer in every model/arm cell.

A wrong expected value does not produce an error. It produces a confident,
wrong score column. Check the expected values against the page the same way you
check the actual values.

## When adding a new validator

1. Build a fixture from a real gnarly case, not a synthetic tidy one. Commit it.
2. Write the test for the *failure mode you just found*, not the happy path.
3. Construct one more adversarial input the test author did not think of.
   (`GBP8,450` immediately followed by a footnote marker digit, no space.)
4. Confirm the rule can actually reject something before believing it passes.
5. Record the new trap in this file.
6. Before quoting any rate it produced: print `n`, and confirm the harness can
   tell you *why* each attempt failed.
7. Confirm the harness returns what the function under test returns. The
   post-fix rerun's first draft wrote `res, status = _call_llm(...)` against a
   function that returns a single dict; all 72 runs raised `ValueError`, were
   swallowed by a broad `except`, and reported `got=None` throughout - which
   reads exactly like "the new schema breaks every model". Check that a known
   case produces a known non-trivial value before launching the batch.

## Known unresolvable ambiguity

`GBP9412` with no separator could be 9,412 or 941 with a footnote marker `2`
jammed on. There is no way to tell, so the parser reads the digits as printed
under the page's declared scale. The comma-grouped form `GBP8,4501` *is*
separable and splits correctly. This is a limit of the source, not a bug to fix;
do not write a test asserting a different answer without a better disambiguator.
