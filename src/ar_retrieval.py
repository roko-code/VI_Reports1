"""Targeted lexical retrieval over cached Annual Report page text.

This module is the single implementation of RAG Lite page selection. It was
promoted out of ``performance_table.py`` (Stage 4) so that Stages 5, 6, 7 and 9
can retrieve the sections they actually need instead of concatenating the
document from page 1 and truncating to a character budget.

Why this exists
---------------
Annual reports run to 150-450 pages. A prefix cut of 80,000 characters reaches
between 1% and 17% of the document depending on the report's text density.
Measured across all 14 cached reports, the statutory key management personnel
pay table, the realised remuneration disclosure and the malus/clawback
disclosure all sit beyond that cut in every single report. The consequence was
the model citing page ranges it had never been shown.

How selection works
-------------------
1. Each stage declares a tuple of :class:`SectionSpec`. A spec is a named
   section with keywords, a priority tier and a page cap.
2. Pages matching a spec's keywords are expanded by a radius, grouped into
   clusters, and ranked. Table-of-contents pages are down-ranked so that a
   contents listing does not masquerade as the real section.
3. The character budget is divided between specs by priority tier, so a dense
   section cannot starve a sparse one. Leftover budget is redistributed
   round-robin.
4. The result carries a manifest recording, per section, what was matched, what
   was included and what was dropped, plus the list of mandatory sections that
   could not be found at all.

The manifest is the point of the exercise: silent truncation is what allowed
unverifiable page citations to pass the audit unnoticed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Priority tiers. Lower is more important and is funded first.
TIER_MANDATORY = 1
TIER_HIGH = 2
TIER_SUPPORTING = 3

_TIER_WEIGHT = {TIER_MANDATORY: 4, TIER_HIGH: 2, TIER_SUPPORTING: 1}

# A contents line looks like "Remuneration report .......... 84". Several of
# these on one page is a reliable signal that the page is a contents listing.
_TOC_LINE = re.compile(
    r"^[^\n]{3,80}?\s*(?:\.{2,}|\s\.{2,}\s)\s*\d{1,4}\s*$",
    re.MULTILINE,
)
_TOC_HEADING = re.compile(r"\b(table of contents|contents|index)\b", re.IGNORECASE)

# Glyph noise. When a PDF embeds a font without a usable ToUnicode CMap, the
# extractors emit raw character identifiers such as "(cid:36)" instead of
# letters. This is not repairable at read time: pypdf, pdfminer and MuPDF all
# resolve the same broken mapping. Such pages are detected and excluded rather
# than allowed to consume the character budget and pollute keyword matching.
_CID_TOKEN = re.compile(r"\(cid:\d+\)")

# A single pathological page must never be able to swallow a whole budget.
# RIO_2022 measured 59,175 characters on one page, which is 74% of the old
# Stage 5 budget. Pages longer than this are truncated with a marker.
MAX_PAGE_CHARS = 20_000

# Share of a page that must be glyph noise before it is treated as unusable.
CID_GARBAGE_THRESHOLD = 0.5


def cid_garbage_ratio(text: str) -> float:
    """Fraction of a page's characters that are undecodable glyph tokens."""
    if not text:
        return 0.0
    return sum(len(m) for m in _CID_TOKEN.findall(text)) / len(text)


def is_unusable_text(text: str) -> bool:
    """True when a page's extracted text cannot be read.

    Checked at retrieval time as well as at extraction time so that caches
    written before this was known are still handled correctly.
    """
    if not text:
        return True
    return cid_garbage_ratio(text) > CID_GARBAGE_THRESHOLD


def usable_page_text(text: str) -> str:
    """Page text with glyph noise stripped and oversize content capped."""
    if not text or is_unusable_text(text):
        return ""
    if len(text) > MAX_PAGE_CHARS:
        return (
            text[:MAX_PAGE_CHARS]
            + f"\n[PAGE TRUNCATED BY RETRIEVAL: original length {len(text):,} chars]"
        )
    return text


@dataclass
class SectionSpec:
    """A named section of an Annual Report to retrieve."""

    key: str
    label: str
    keywords: list[str]
    priority: int = TIER_HIGH
    max_pages: int = 10
    radius: int = 1
    mandatory: bool = False

    @property
    def weight(self) -> int:
        return _TIER_WEIGHT.get(self.priority, 1)


@dataclass
class SectionOutcome:
    """What happened to one section during retrieval."""

    key: str
    label: str
    priority: int
    mandatory: bool
    matched_pages: list[int] = field(default_factory=list)
    selected_pages: list[int] = field(default_factory=list)
    chars: int = 0
    status: str = "missing"  # included | partial | missing | budget_exhausted
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "priority": self.priority,
            "mandatory": self.mandatory,
            "status": self.status,
            "matched_pages": self.matched_pages,
            "selected_pages": self.selected_pages,
            "chars": self.chars,
            "note": self.note,
        }


@dataclass
class RetrievalResult:
    """Assembled context plus the manifest describing how it was assembled."""

    text: str
    sections: list[SectionOutcome]
    budget_chars: int
    chars_used: int
    total_pages: int
    page_range: tuple[int, int] | None
    unreadable_pages: int = 0

    @property
    def missing_mandatory(self) -> list[str]:
        return [
            s.label
            for s in self.sections
            if s.mandatory and s.status in ("missing", "budget_exhausted")
        ]

    @property
    def partial_mandatory(self) -> list[str]:
        return [
            s.label
            for s in self.sections
            if s.mandatory and s.status == "partial"
        ]

    @property
    def included_pages(self) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for s in self.sections:
            for p in s.selected_pages:
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return sorted(out)

    def to_manifest(self) -> dict:
        return {
            "total_pages": self.total_pages,
            "unreadable_pages": self.unreadable_pages,
            "budget_chars": self.budget_chars,
            "chars_used": self.chars_used,
            "utilisation_pct": round(
                self.chars_used / self.budget_chars * 100, 1
            )
            if self.budget_chars
            else 0.0,
            "page_span": list(self.page_range) if self.page_range else None,
            "document_coverage_pct": round(
                len(self.included_pages) / self.total_pages * 100, 1
            )
            if self.total_pages
            else 0.0,
            "missing_mandatory": self.missing_mandatory,
            "partial_mandatory": self.partial_mandatory,
            "sections": [s.to_dict() for s in self.sections],
        }

    def coverage_note(self) -> str:
        """Plain-text summary for injection into the LLM prompt.

        Tells the model which sections it was *not* given, so that rules
        depending on them resolve to Uncertain rather than being guessed.
        """
        lines = ["RETRIEVAL COVERAGE (generated by the pipeline, not the report):"]
        for s in sorted(self.sections, key=lambda x: (x.priority, x.key)):
            if s.status == "included":
                span = (
                    f"pages {min(s.selected_pages)}-{max(s.selected_pages)}"
                    if s.selected_pages
                    else ""
                )
                lines.append(f"  - SUPPLIED: {s.label} ({span})")
            elif s.status == "partial":
                lines.append(
                    f"  - PARTIAL: {s.label} ({s.note or 'some pages excluded by budget'})"
                )
            elif s.status == "budget_exhausted":
                lines.append(
                    f"  - NOT SUPPLIED: {s.label} (excluded - no budget remained)"
                )
            else:
                lines.append(
                    f"  - NOT SUPPLIED: {s.label} (section not located in this report)"
                )
        lines.append(
            "Any rule that depends on a section marked NOT SUPPLIED or PARTIAL must be "
            "assessed as Uncertain. Do not infer the content of a section you were not "
            "given, and do not cite page numbers outside the supplied page ranges."
        )
        if self.unreadable_pages:
            lines.append(
                f"Note: {self.unreadable_pages} of {self.total_pages} pages in this report "
                "could not be text-extracted (embedded font without a usable character "
                "map) and were withheld. Any rule that depends on those pages must be "
                "assessed as Uncertain."
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Low-level page selection
# --------------------------------------------------------------------------


def contents_score(page_text: str) -> int:
    """Heuristic score for how table-of-contents-like a page is. Higher = more so."""
    if not page_text:
        return 0
    score = 0
    if _TOC_HEADING.search(page_text[:400]):
        score += 5
    score += min(len(_TOC_LINE.findall(page_text)), 10)
    return score


def find_section_pages(pages: list[dict], keywords: list[str]) -> list[int]:
    """Return page numbers whose text contains any of the keywords.

    Unreadable pages are skipped. Letting them match would manufacture
    "evidence" from glyph noise.
    """
    lowered = _build_lowered_index(pages)
    return _find_section_pages_indexed(pages, keywords, lowered)


def _build_lowered_index(pages: list[dict]) -> list[str]:
    """One lowercased copy of every page's text, computed once.

    ``build_context`` calls ``find_section_pages`` once per spec, and without
    this cache each call re-lowercased the entire annual report (up to ~5x per
    company across the 14 cached reports). Caching it here makes the per-spec
    lookup O(matched) instead of O(all_pages x keywords).
    """
    lowered: list[str] = []
    for page in pages:
        text = page.get("text") or ""
        lowered.append(text.lower() if text and not is_unusable_text(text) else "")
    return lowered


def _find_section_pages_indexed(pages: list[dict], keywords: list[str], lowered: list[str]) -> list[int]:
    """Keyword scan using a precomputed lowercased-text index (_build_lowered_index)."""
    if not keywords:
        return []
    lowered_kw = [k.lower() for k in keywords if k]
    matches = []
    for page, low in zip(pages, lowered):
        if low and any(k in low for k in lowered_kw):
            matches.append(page["page_number"])
    return matches


def expand_pages(page_numbers: list[int], total_pages: int, radius: int = 1) -> list[int]:
    """Add neighbouring physical pages around each match."""
    expanded: set[int] = set()
    for num in page_numbers:
        start = max(1, num - radius)
        end = min(total_pages, num + radius)
        expanded.update(range(start, end + 1))
    return sorted(expanded)


def cluster_pages(page_numbers: list[int], gap: int = 3) -> list[list[int]]:
    """Group nearby pages so broad keyword scans can be ranked by section."""
    clusters: list[list[int]] = []
    for num in sorted(set(page_numbers)):
        if not clusters or num > clusters[-1][-1] + gap:
            clusters.append([num])
        else:
            clusters[-1].append(num)
    return clusters


def rank_section_pages(
    pages: list[dict],
    page_numbers: list[int],
    max_pages: int,
    contents_floor: int = 3,
) -> list[int]:
    """Pick the best pages for a section, down-ranking contents pages.

    Clusters are ranked by size, but a cluster that looks like a table of
    contents is pushed behind real prose of comparable size. Without this, a
    keyword like "remuneration report" resolves to the contents page and the
    actual section is never read.
    """
    if not page_numbers:
        return []
    if len(page_numbers) <= max_pages:
        return sorted(page_numbers)

    by_num = {p["page_number"]: p for p in pages}
    clusters = cluster_pages(page_numbers, gap=3)
    clusters.sort(
        key=lambda c: (contents_score((by_num.get(c[0], {}).get("text") or "")), len(c), -c[0]),
        reverse=True,
    )

    selected: list[int] = []
    deferred: list[int] = []
    for cluster in clusters:
        is_contents = contents_score((by_num.get(cluster[0], {}).get("text") or "")) >= contents_floor
        target = deferred if is_contents else selected
        for num in cluster:
            if len(target) >= max_pages:
                break
            target.append(num)
        if len(selected) >= max_pages:
            break

    if len(selected) < max_pages:
        selected.extend(deferred)
    return sorted(selected[:max_pages])


def page_text(pages: list[dict], page_num: int) -> str:
    """Text of a 1-indexed page, or empty string if out of range."""
    if page_num < 1 or page_num > len(pages):
        return ""
    return pages[page_num - 1].get("text") or ""


# --------------------------------------------------------------------------
# Budgeted assembly
# --------------------------------------------------------------------------


def build_context(
    pages: list[dict],
    specs: tuple[SectionSpec, ...],
    budget_chars: int,
    logger=None,
) -> RetrievalResult:
    """Assemble a budgeted context covering every spec.

    Budget is allocated per tier so that a single dense section cannot consume
    the whole allowance. Any budget left after every spec is satisfied is
    redistributed round-robin, weighted by tier.
    """
    total_pages = len(pages)
    if total_pages == 0:
        return RetrievalResult("", [], budget_chars, 0, 0, None)

    def log(msg: str) -> None:
        if logger:
            logger.info(msg)

    # Pass 1: resolve each spec to a ranked page list.
    # Pre-compute the lowercased-text index once; without this the per-spec
    # keyword scan re-lowercased the whole annual report every call.
    lowered_index = _build_lowered_index(pages)
    resolved: list[tuple[SectionSpec, list[int], list[int]]] = []
    outcomes: list[SectionOutcome] = []
    for spec in specs:
        matched = _find_section_pages_indexed(pages, spec.keywords, lowered_index)
        expanded = expand_pages(matched, total_pages, spec.radius)
        selected = rank_section_pages(pages, expanded, spec.max_pages)
        resolved.append((spec, matched, selected))
        outcomes.append(
            SectionOutcome(
                key=spec.key,
                label=spec.label,
                priority=spec.priority,
                mandatory=spec.mandatory,
                matched_pages=matched[:80],
                selected_pages=selected,
            )
        )
        if not selected:
            outcomes[-1].status = "missing"
            outcomes[-1].note = "no page matched any keyword"
        log(
            f"  [{spec.key}] matched {len(matched)} pages, selected {len(selected)}"
            + (f" -> {selected}" if selected else "")
        )

    # Pass 2: allocate budget per tier.
    total_weight = sum(s.weight for s, _, sel in resolved if sel) or 1
    per_section_cap: dict[str, int] = {}
    for spec, _, sel in resolved:
        if sel:
            per_section_cap[spec.key] = max(
                4_000, int(budget_chars * spec.weight / total_weight)
            )

    added: set[int] = set()
    page_cost: dict[int, int] = {}
    spent_by_section: dict[str, int] = {s.key: 0 for s, _, _ in resolved}
    chunks: list[tuple[int, str]] = []
    used = 0

    def try_add(spec_key: str, num: int, cap: int) -> bool:
        """Add a page if it fits this section's cap and the global budget."""
        nonlocal used
        if num in added:
            # Already present elsewhere in the context: free, count for both.
            return True
        raw = page_text(pages, num)
        if is_unusable_text(raw):
            return False
        text = raw if len(raw) <= MAX_PAGE_CHARS else usable_page_text(raw)
        if not text:
            return False
        cost = len(text) + len(f"\n--- Page {num} ---\n")
        if used + cost > budget_chars:
            return False
        if spent_by_section[spec_key] + cost > cap and spent_by_section[spec_key] > 0:
            return False
        added.add(num)
        page_cost[num] = cost
        spent_by_section[spec_key] += cost
        used += cost
        chunks.append((num, text))
        return True

    # Tier-ordered first pass: every spec gets funded before any gets extra.
    for spec, _, selected in sorted(resolved, key=lambda x: (x[0].priority, x[0].key)):
        if not selected:
            continue
        cap = per_section_cap.get(spec.key, 0)
        for num in selected:
            if used >= budget_chars:
                break
            try_add(spec.key, num, cap)

    # Pass 3: redistribute leftover budget round-robin across all specs.
    if used < budget_chars:
        for spec, _, selected in sorted(resolved, key=lambda x: (x[0].priority, x[0].key)):
            if not selected:
                continue
            cap = per_section_cap.get(spec.key, 0) * 3
            for num in selected:
                if used >= budget_chars:
                    break
                if num in added:
                    continue
                try_add(spec.key, num, cap)

    # Render in page order with explicit page markers.
    chunks.sort(key=lambda x: x[0])
    parts: list[str] = []
    for num, text in chunks:
        parts.append(f"\n--- Page {num} ---\n{text}")
    text = "".join(parts)

    for (spec, _, selected), outcome in zip(resolved, outcomes):
        outcome.selected_pages = [n for n in selected if n in added]
        outcome.chars = spent_by_section.get(spec.key, 0)
        if not outcome.selected_pages:
            if outcome.status != "missing":
                outcome.status = "budget_exhausted"
                outcome.note = "matched but no budget remained"
        elif len(outcome.selected_pages) < len(selected):
            outcome.status = "partial"
            outcome.note = (
                f"{len(selected) - len(outcome.selected_pages)} of {len(selected)} "
                "pages excluded by budget"
            )
        else:
            outcome.status = "included"

    page_range = (min(added), max(added)) if added else None
    unreadable = sum(1 for p in pages if is_unusable_text(p.get("text") or ""))
    log(
        f"  Assembled {len(added)} pages, {used:,}/{budget_chars:,} chars "
        f"({used / budget_chars * 100:.0f}%), span {page_range}, "
        f"{unreadable} unreadable pages, "
        f"missing mandatory: {len([o for o in outcomes if o.mandatory and o.status == 'missing'])}"
    )

    return RetrievalResult(
        text=text,
        sections=outcomes,
        budget_chars=budget_chars,
        chars_used=used,
        total_pages=total_pages,
        page_range=page_range,
        unreadable_pages=unreadable,
    )


def write_manifest(
    result: RetrievalResult,
    supporting_data_dir: Path,
    stage: str,
    ticker: str,
    year: int,
    logger=None,
) -> Path:
    """Persist the retrieval manifest next to the stage's other artifacts."""
    supporting_data_dir = Path(supporting_data_dir)
    supporting_data_dir.mkdir(parents=True, exist_ok=True)
    path = supporting_data_dir / f"retrieval_manifest_{stage}.json"
    payload = {"stage": stage, "ticker": ticker, "year": year}
    payload.update(result.to_manifest())
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if logger:
        logger.info(f"  Retrieval manifest written: {path.name}")
    return path


# --------------------------------------------------------------------------
# Per-stage section manifests
# --------------------------------------------------------------------------
#
# Mandatory sections are those an ASA rule cannot be assessed without. If one
# of these cannot be located, the stage logs a warning and the coverage note
# tells the model to answer Uncertain rather than infer.

_FRONT_MATTER = SectionSpec(
    key="front_matter",
    label="Chair's statement, CEO review and contents",
    keywords=[
        "chair's statement", "chair s statement", "chairman statement",
        "chief executive review", "ceo review", "president and chief executive",
        "our year at a glance", "operating and financial review",
    ],
    priority=TIER_SUPPORTING,
    max_pages=6,
    radius=0,
)

STAGE5_SPECS: tuple[SectionSpec, ...] = (
    _FRONT_MATTER,
    SectionSpec(
        key="rem_report",
        label="Statutory remuneration report",
        keywords=[
            "statutory remuneration report", "remuneration report",
            "remuneration and employees", "remuneration policy",
        ],
        priority=TIER_MANDATORY,
        max_pages=24,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="kmp_pay_table",
        label="Statutory key management personnel pay table",
        keywords=[
            "key management personnel", "kmp", "statutory key management",
            "short term post-employment benefits", "post-employment benefits",
        ],
        priority=TIER_MANDATORY,
        max_pages=10,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="realised_pay",
        label="Realised remuneration and actual pay received",
        keywords=[
            # Anchor phrases verified against all 14 cached reports. Australian
            # issuers say "actual remuneration"; UK-format issuers say
            # "remuneration actually received"; several say "realised".
            "actual remuneration",
            "actual and maximum",
            "percentage of maximum",
            "remuneration actually received",
            "actually received",
            "realised remuneration",
            "realised pay",
            "realized remuneration",
            "target remuneration",
            "actual pay received",
            "remuneration actually",
        ],
        priority=TIER_MANDATORY,
        max_pages=10,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="malus_clawback",
        label="Malus and clawback provisions",
        keywords=[
            "malus", "clawback", "claw-back", "claw back",
        ],
        priority=TIER_MANDATORY,
        max_pages=8,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="sti_detail",
        label="Short-term incentive design and outcomes",
        keywords=[
            "short-term incentive", "short term incentive", "sti",
            "annual incentive", "performance against targets", "scorecard",
        ],
        priority=TIER_HIGH,
        max_pages=10,
        radius=1,
    ),
    SectionSpec(
        key="lti_detail",
        label="Long-term incentive design, hurdles and outcomes",
        keywords=[
            "long-term incentive", "long term incentive", "lti",
            "performance rights", "performance conditions", "hurdles",
            "vesting conditions", "equity incentive",
        ],
        priority=TIER_HIGH,
        max_pages=10,
        radius=1,
    ),
    SectionSpec(
        key="prior_year_rem",
        label="Prior year remuneration and comparison figures",
        keywords=[
            "prior year remuneration", "previous financial year",
            "year-on-year comparison", "remuneration comparison",
        ],
        priority=TIER_SUPPORTING,
        max_pages=6,
        radius=1,
    ),
    SectionSpec(
        key="rem_governance",
        label="Remuneration committee, policy setting and voting",
        keywords=[
            "remuneration committee", "remuneration and people committee",
            "remuneration policy statement", "say on pay", "first vote",
            "information circular",
        ],
        priority=TIER_HIGH,
        max_pages=8,
        radius=1,
    ),
)

STAGE7_SPECS: tuple[SectionSpec, ...] = (
    _FRONT_MATTER,
    SectionSpec(
        key="constitution",
        label="Constitution, share capital and amendment provisions",
        keywords=[
            "constitution", "memorandum and articles", "articles of association",
            "amendment of the constitution", "share capital", "issued capital",
            "alteration to the constitution", "special resolutions",
        ],
        priority=TIER_MANDATORY,
        max_pages=14,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="buybacks",
        label="Share buyback, capital return and capital management",
        keywords=[
            "share buyback", "share repurchase", "buy-back", "on-market buyback",
            "capital return", "capital management", "capital structure",
            "return of capital", "franking credits",
        ],
        priority=TIER_MANDATORY,
        max_pages=12,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="dividends",
        label="Dividend policy and dividend history",
        keywords=[
            "dividend policy", "dividends", "final dividend", "interim dividend",
            "dividend payout", "dividend history",
        ],
        priority=TIER_HIGH,
        max_pages=8,
        radius=1,
    ),
    SectionSpec(
        key="sustainability",
        label="Sustainability, climate and emissions reporting",
        keywords=[
            "sustainability", "climate change", "climate-related", "emissions",
            "net zero", "net-zero", "environment, social and governance",
            "esg", "greenhouse gas", "tcfd", "sasb", "task force on climate",
        ],
        priority=TIER_HIGH,
        max_pages=12,
        radius=1,
    ),
    SectionSpec(
        key="audit_tax",
        label="Auditor, audit fees and tax transparency",
        keywords=[
            "audit fees", "auditor remuneration", "independent auditor",
            "tax transparency", "tax contribution", "global minimum tax",
        ],
        priority=TIER_SUPPORTING,
        max_pages=6,
        radius=1,
    ),
    SectionSpec(
        key="members",
        label="Members, shareholdings and voting rights",
        keywords=[
            "substantial shareholders", "shareholdings", "voting rights",
            "beneficial ownership", "director interests", "related party",
        ],
        priority=TIER_SUPPORTING,
        max_pages=6,
        radius=1,
    ),
)

STAGE9_SPECS: tuple[SectionSpec, ...] = (
    _FRONT_MATTER,
    SectionSpec(
        key="strategy",
        label="Strategy, purpose and business model",
        keywords=[
            "our strategy", "our purpose", "business model", "strategic priorities",
            "value creation model", "operating model", "how we create value",
        ],
        priority=TIER_MANDATORY,
        max_pages=10,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="risk",
        label="Risk management and material risks",
        keywords=[
            "risk management", "material risks", "principal risks",
            "our risks", "risk appetite", "risk oversight", "emerging risk",
            "enterprise risk",
        ],
        priority=TIER_MANDATORY,
        max_pages=14,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="board_composition",
        label="Board composition, skills and diversity",
        keywords=[
            "board composition", "board skills", "board diversity", "our board",
            "board effectiveness", "skills matrix", "diversity policy",
            "board diversity policy", "lead independent director",
        ],
        priority=TIER_MANDATORY,
        max_pages=12,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="financial_performance",
        label="Financial performance and results",
        keywords=[
            "financial performance", "review of operations", "operating results",
            "revenue", "statutory results", "financial results",
            "performance against guidance", "net profit",
        ],
        priority=TIER_HIGH,
        max_pages=10,
        radius=1,
    ),
    SectionSpec(
        key="rem_governance",
        label="Remuneration policy and governance",
        keywords=[
            "remuneration policy", "remuneration report", "remuneration committee",
            "short-term incentive", "long-term incentive",
        ],
        priority=TIER_HIGH,
        max_pages=8,
        radius=1,
    ),
    SectionSpec(
        key="engagement",
        label="Shareholder and stakeholder engagement",
        keywords=[
            "shareholder engagement", "stakeholder engagement", "engagement",
            "shareholder consultation", "proxy advisers", "vote against",
        ],
        priority=TIER_HIGH,
        max_pages=8,
        radius=1,
    ),
    SectionSpec(
        key="governance",
        label="Governance framework and ethics",
        keywords=[
            "corporate governance", "governance framework", "code of conduct",
            "ethics", "compliance", "whistleblowing",
        ],
        priority=TIER_SUPPORTING,
        max_pages=6,
        radius=1,
    ),
)

STAGE6_SPECS: tuple[SectionSpec, ...] = (
    _FRONT_MATTER,
    SectionSpec(
        key="directors_section",
        label="Directors, board and profile disclosures",
        keywords=[
            "our directors", "the board", "board of directors", "director profiles",
            "directors and officers", "executive directors", "non-executive",
        ],
        priority=TIER_MANDATORY,
        max_pages=20,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="ethics_conflicts",
        label="Conflicts of interest, ethics and related party",
        keywords=[
            "conflict of interest", "conflicts of interest", "code of conduct",
            "ethics and integrity", "related party", "related parties",
            "indulgences and related party",
        ],
        priority=TIER_MANDATORY,
        max_pages=10,
        radius=1,
        mandatory=True,
    ),
    SectionSpec(
        key="attendance",
        label="Board attendance and meeting records",
        keywords=[
            "attendance at meetings", "board meetings", "meetings attended",
            "committee meetings", "director attendance",
        ],
        priority=TIER_HIGH,
        max_pages=8,
        radius=1,
    ),
    SectionSpec(
        key="succession",
        label="Succession planning and diversity",
        keywords=[
            "succession planning", "succession", "board renewal", "diversity",
        ],
        priority=TIER_HIGH,
        max_pages=8,
        radius=1,
    ),
)

STAGE_SPECS: dict[str, tuple[SectionSpec, ...]] = {
    "stage5": STAGE5_SPECS,
    "stage6": STAGE6_SPECS,
    "stage7": STAGE7_SPECS,
    "stage9": STAGE9_SPECS,
}
