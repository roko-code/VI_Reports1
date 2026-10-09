"""Regression harness for Annual Report targeted retrieval.

Asserts that every mandatory section for a stage is actually located and
included in the assembled context, for every cached report. This is the check
that would have caught the original defect: silent prefix truncation leaving
the statutory pay table, realised remuneration and malus/clawback disclosures
entirely unseen.

Run:  cd src && python retrieval_selftest.py
Exit code 0 = all mandatory sections present, 1 = at least one missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ar_retrieval import STAGE_SPECS, build_context

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "_cache" / "annual_report_text"

# Budgets sized for targeted retrieval, not prefix truncation. gpt-4.1-mini
# has a 1M token context, so these are cheap by comparison; the previous
# values (80k/100k/200k/40k) were chosen to bound a concatenation of the
# whole document, which is what caused the defect.
STAGE_BUDGETS = {
    "stage5": 250_000,
    "stage6": 200_000,
    "stage7": 250_000,
    "stage9": 150_000,
}


class _NullLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def load_pages(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("pages", [])


def main() -> int:
    if not CACHE_DIR.is_dir():
        print(f"No cache directory at {CACHE_DIR}")
        return 0

    cache_files = sorted(CACHE_DIR.glob("*.json"))
    if not cache_files:
        print(f"No cached reports in {CACHE_DIR}")
        return 0

    failures: list[str] = []
    warnings: list[str] = []
    for stage in ("stage5", "stage6", "stage7", "stage9"):
        specs = STAGE_SPECS[stage]
        budget = STAGE_BUDGETS[stage]
        mandatory = [s for s in specs if s.mandatory]

        print("=" * 78)
        print(f"{stage.upper()}   budget {budget:,} chars   "
              f"{len(mandatory)} mandatory sections   {len(cache_files)} reports")
        print("=" * 78)

        for path in cache_files:
            pages = load_pages(path)
            if not pages:
                continue
            result = build_context(pages, specs, budget, logger=_NullLogger())

            missing = result.missing_mandatory
            partial = result.partial_mandatory
            status = "PASS" if not missing else "FAIL"
            cov = result.to_manifest()["document_coverage_pct"]
            span = result.page_range
            span_s = f"pp{span[0]}-{span[1]}" if span else "none"
            unread = (
                f"  unreadable {result.unreadable_pages}"
                if result.unreadable_pages
                else ""
            )
            print(
                f"  {status}  {path.stem:<10} "
                f"{len(result.included_pages):>4}/{len(pages):<4} pp "
                f"({cov:>5.1f}%)  span {span_s:<12} "
                f"used {result.chars_used / budget * 100:>5.1f}% of budget"
                f"{unread}"
            )
            for outcome in result.sections:
                if outcome.status == "missing" and outcome.mandatory:
                    detail = outcome.note or outcome.status
                    print(f"          MISSING MANDATORY: {outcome.label} ({detail})")
                    failures.append(
                        f"{stage} {path.stem}: missing mandatory "
                        f"'{outcome.label}' ({detail})"
                    )
                elif outcome.status == "partial" and outcome.mandatory:
                    print(
                        f"          partial (warning): {outcome.label} "
                        f"-- {outcome.note}"
                    )
                    warnings.append(f"{stage} {path.stem}: {outcome.label} ({outcome.note})")

    print()
    if warnings:
        print(f"{len(warnings)} partial-coverage warning(s) "
              f"(section located but partly excluded by budget):")
        for w in warnings[:20]:
            print(f"  - {w}")
        if len(warnings) > 20:
            print(f"  ... and {len(warnings) - 20} more")
        print()

    if failures:
        print(f"{len(failures)} FAILURE(S) - mandatory section entirely absent:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All mandatory sections located in every cached report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
