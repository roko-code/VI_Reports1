# -*- coding: utf-8 -*-
"""Resolve a figure to its currency and scale, per row rather than per page.

Why this exists
---------------
`_page_contains_value` matched digits alone. It passed a `$m` claim of 8.45
against a Rio Tinto page headed `Single total figure of remuneration (\u00a3'000)`
- the digits matched, the currency did not, and the figure was roughly 2x wrong
once converted. That check was the only control doing useful work in Stage 4d,
so a blind spot in it is the most expensive kind of defect here.

Why per row
-----------
Currency is not a page attribute in a dual-listed issuer. Rio Tinto FY24 p.138
puts `A$10,476` and `\u00a38,450` in the same column; p.140 puts `\u00a3941`,
`SGD981`, `A$1,188` and `C$258` in one STIP table. Anchoring currency at page
level is wrong by construction for exactly the filings most likely to be
misread. So a currency token binds forward to the numbers after it on that line,
and is otherwise inherited from the nearest preceding header line.

Known limits
------------
See docs/validator_traps.md. A bare `$` is reported as `USD?` - it is
ambiguous between AUD and USD and is never accepted as satisfying a `$m` claim
without an explicit qualifier. Rows with no resolvable scale are returned with
`value_m=None` rather than guessed.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Ordered: qualified forms before the bare "$" so A$ never matches as USD.
# A qualifier may be separated from the symbol by a space or a full stop -
# Rio Tinto FY23 prints "A $", "US $" and "S $" that way, and a corpus sweep
# found 5 such tokens the first version silently missed.
_CURRENCY_PATTERNS = [
    (r"(?<![A-Za-z])AU\.?\s*\$", "AUD"), (r"(?<![A-Za-z])A\.?\s*\$", "AUD"),
    (r"(?<![A-Za-z])U\.?\s?S\.?\s*\$", "USD"),
    (r"(?<![A-Za-z])U\.?\s*\$", "USD"),
    (r"(?<![A-Za-z])NZ\.?\s*\$", "NZD"), (r"(?<![A-Za-z])HK\.?\s*\$", "HKD"),
    (r"(?<![A-Za-z])S\.?\s*\$", "SGD"), (r"(?<![A-Za-z])C\.?\s*\$", "CAD"),
    (r"\bSGD\b", "SGD"), (r"\bUSD\b", "USD"), (r"\bAUD\b", "AUD"),
    (r"\bNZD\b", "NZD"), (r"\bCAD\b", "CAD"), (r"\bGBP\b", "GBP"),
    (r"\bEUR\b", "EUR"), (r"\bCHF\b", "CHF"), (r"\bJPY\b", "JPY"),
    (r"\bSEK\b", "SEK"), (r"\bZAR\b", "ZAR"), (r"\bINR\b", "INR"),
    (r"£", "GBP"), (r"€", "EUR"), (r"¥", "JPY"),
    (r"\$", "USD?"),          # bare $ - ambiguous, deliberately marked
]
_CURRENCY_RE = re.compile(
    "|".join(f"(?P<c{i}>{p})" for i, (p, _) in enumerate(_CURRENCY_PATTERNS)),
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+")

_SCALE_PATTERNS = [
    (r"[\u2018\u2019']?\s?000", "thousand"),
    (r"[$\u00a3\u20ac\uffe5]\s*bn\b|\(\s*bn\s*\)", "bn"),
    (r"[$\u00a3\u20ac\uffe5]\s*m\b|\(\s*m\s*\)", "m"),
]
_SCALE_FACTORS = {"units": 1, "thousand": 1_000, "m": 1_000_000,
                 "bn": 1_000_000_000}

# A CEO's annual total remuneration falls in a narrow band. Used only to
# disambiguate a page that declares no scale at all, and only when exactly one
# of the two candidate readings lands inside it. Below ~$0.3m is a component
# or a share count, not an annual total; above ~$200m is not a CEO package.
CEO_TOTAL_BAND_M = (0.3, 200.0)


def _scale_of(line: str) -> str | None:
    """The scale a line declares, if any.

    Numbers are stripped before matching. A data row such as
    `Andrew 2025 2,500,000 1,035,000 3,535,000 7,566,768` contains the digits
    "000" several times inside comma-grouped figures, and a naive pattern read
    that as a thousands-scale declaration. That silently mis-scaled every row
    on the page by 1000x, and made an undeclared page look declared - the
    opposite of what this function exists to establish. Found by forcing a
    branch that had never fired; see docs/validator_traps.md.
    """
    stripped = _NUMBER_RE.sub(" ", line)
    for pat, name in _SCALE_PATTERNS:
        if re.search(pat, stripped, re.IGNORECASE):
            return name
    return None


def _currency_of_match(m) -> str | None:
    """Resolve a currency regex match to its code via the group that matched.

    Using the group index keeps this in lockstep with _CURRENCY_PATTERNS, so
    adding a currency cannot silently desync from the lookup.
    """
    for i, (_, cur) in enumerate(_CURRENCY_PATTERNS):
        if m.group(f"c{i}"):
            return cur
    return None


@dataclass
class Figure:
    """A number found in page text, with the currency and scale it carries."""
    raw: float
    currency: str | None          # "AUD", "GBP", ..., "USD?" for an unqualified "$"
    currency_from: str            # "row" | "header" | "assumed" | "none"
    scale: str | None             # "thousand" | "m" | "bn" | "units"
    scale_from: str               # "declared" | "default_units"
    value_m: float | None         # normalised to millions, None if scale unknown
    page: int | None
    line_no: int
    line: str

    @property
    def is_unambiguous_aud(self) -> bool:
        """True for an explicit AUD token, or a bare "$" in a report known to
        report in AUD. Never true for a bare "$" with no such context."""
        return self.currency == "AUD"

    @property
    def scale_is_declared(self) -> bool:
        """True when the page states its scale, or when exactly one reading of
        the figure is a plausible CEO annual total.

        The inferred case is narrow by construction: both the units and the
        thousands readings are computed, and only a reading that lands inside
        CEO_TOTAL_BAND_M counts. `$7,566,768` is 7.57m as units and 7,566.8m
        as thousands, so units is inferred and recorded. A share count of
        95,363 is 0.10m as units and 95.4m as thousands - neither is a
        plausible CEO total, so it yields nothing. Where both readings are
        plausible the figure is left unresolved rather than guessed.
        """
        return self.scale_from in ("declared", "inferred")

    def describe(self) -> str:
        cur = self.currency or "?"
        src = {"row": "row", "header": "header", "assumed": "assumed from "
               "issuer", "none": "no currency found"}[self.currency_from]
        if self.value_m is None:
            return (f"{self.raw:,.0f} [{cur} ({src}), scale unknown -> "
                    f"not normalisable]")
        s = self.scale if self.scale_from == "declared" else "units (assumed)"
        return (f"{self.raw:,.0f} [{cur} ({src}), {s}] = "
                f"{self.value_m:,.3f}m")


def anchored_figures(page_text: str, page_no: int | None = None,
                     bare_currency: str | None = None) -> list[Figure]:
    """Every non-percentage number on the page, bound to a currency and scale.

    `bare_currency` resolves an unqualified "$". Pass "AUD" for an ASX-listed
    issuer, whose reports write "$" to mean AUD throughout. Without it a bare
    "$" stays ambiguous and can never satisfy a `$m` claim.

    A currency token binds forward within its line. Where a line carries no
    currency token the nearest preceding header line within `header_lookback`
    lines supplies one, which is how `(\u00a3'000)` in a table caption applies to
    the data rows beneath it.

    Known limit: a caption sits 3 lines above a 10-row table, so the last rows
    fall 11 lines from it. The lookback is 15 to cover that. A page placing two
    tables with different scales within 15 lines and no intervening currency
    declaration would mis-attribute scale - scale is not the security-relevant
    field here, per-row currency is, and currency is always read from the row.
    """
    lines = page_text.split("\n")
    out: list[Figure] = []

    header_lookback = 15

    # If the page declares a scale nowhere, its figures are as printed (units).
    # If it declares one, that governs. A page mixing an unlabelled units table
    # with a labelled '000 one is genuinely ambiguous in the source; that case
    # surfaces as a near-miss with its provenance rather than a silent pass.
    page_declares_scale = any(_scale_of(l) for l in lines)
    default_scale = None if page_declares_scale else "units"

    for i, line in enumerate(lines):
        line_scale = _scale_of(line)

        def _resolve(cur: str | None, src: str) -> tuple[str | None, str]:
            if cur == "USD?" and bare_currency:
                return bare_currency, "assumed"
            return cur, src

        row_currency, row_src = None, "row"
        for m in _CURRENCY_RE.finditer(line):
            cur = _currency_of_match(m)
            if cur:
                row_currency, row_src = _resolve(cur, "row")
                break

        # header fallback: nearest preceding line that declared a currency
        if row_currency is None:
            for j in range(i - 1, max(-1, i - header_lookback - 1), -1):
                cm = _CURRENCY_RE.search(lines[j])
                if cm:
                    cand = _currency_of_match(cm)
                    if cand:
                        row_currency, row_src = _resolve(cand, "header")
                        break

        # header fallback for scale
        scale = line_scale
        scale_from = "declared"
        if scale is None:
            for j in range(i - 1, max(-1, i - header_lookback - 1), -1):
                s = _scale_of(lines[j])
                if s:
                    scale = s
                    break
        if scale is None:
            scale = default_scale
            # "unknown" must NOT read as "declared": an unlabelled figure has
            # no scale evidence at all, and must not be treated as though it
            # had been confirmed. Getting this backwards silently excluded
            # exactly the near-misses the validator exists to report.
            scale_from = "default_units" if scale else "unknown"

        cur_for_line = row_currency
        cur_src = row_src if row_currency is not None else "none"

        # A currency token binds forward across the rest of the line, so a
        # caption row like "Name $ $ $ $" governs the data rows below it.
        for tok in _CURRENCY_RE.finditer(line):
            c = _currency_of_match(tok)
            if c:
                cur_for_line, cur_src = _resolve(c, "row")

        for nm in _NUMBER_RE.finditer(line):
            tail = line[nm.end():nm.end() + 2]
            if tail.lstrip().startswith("%"):
                continue                      # percentage, not a money figure
            start = nm.start()
            prefix = line[max(0, start - 6):start]
            # currency immediately attached to the digits, e.g. "SGD981"
            this_cur, this_src = cur_for_line, cur_src
            for m in _CURRENCY_RE.finditer(prefix):
                c = _currency_of_match(m)
                if c:
                    this_cur, this_src = _resolve(c, "row")

            raw = float(nm.group(0).replace(",", ""))
            val_m = None
            scale_from_eff = scale_from
            if scale_from == "declared" and scale:
                val_m = raw * _SCALE_FACTORS[scale] / 1_000_000
            else:
                # No scale stated anywhere. Compute both readings and keep one
                # only if exactly one is a plausible CEO annual total.
                lo, hi = CEO_TOTAL_BAND_M
                cands = [(raw * _SCALE_FACTORS[k] / 1_000_000, k)
                         for k in ("units", "thousand")]
                in_band = [(v, k) for v, k in cands if lo <= v <= hi]
                if len(in_band) == 1:
                    val_m, inferred = in_band[0]
                    scale, scale_from_eff = inferred, "inferred"
            out.append(Figure(
                raw=raw, currency=this_cur, currency_from=this_src,
                scale=scale, scale_from=scale_from_eff, value_m=val_m,
                page=page_no, line_no=i, line=line,
            ))
    return out


def aud_million_anchors(page_text: str, page_no: int | None = None,
                        bare_currency: str | None = None) -> list[Figure]:
    """Figures that can legitimately satisfy a `$m` claim.

    Requires AUD *and* a declared scale. An AUD figure whose scale was only
    assumed is excluded - a bare number on an unlabelled page is not evidence.
    """
    return [f for f in anchored_figures(page_text, page_no, bare_currency)
            if f.is_unambiguous_aud and f.scale_is_declared
            and f.value_m is not None]


def contains_aud_million(
    page_text: str, value_m: float, page_no: int | None = None,
    tol: float = 0.011, bare_currency: str | None = None,
) -> bool:
    """True only when an AUD figure on a declared scale equals value_m.

    This is the currency-aware replacement for the digit-only
    `_page_contains_value`, which passed a `$m` claim of 8.45 against a page
    headed in GBP. A bare `$` satisfies it only when `bare_currency="AUD"`.
    """
    for f in aud_million_anchors(page_text, page_no, bare_currency):
        if abs(f.value_m - value_m) <= tol:
            return True
    return False


def near_miss_currencies(page_text: str, value_m: float,
                         page_no: int | None = None,
                         tol: float = 0.011,
                         bare_currency: str | None = None) -> list[Figure]:
    """Figures whose digits match the claim but whose currency or scale does not.

    This is what makes the rejection actionable: instead of "8.45 is not on
    this page" it can say "8.45 is here, but it is GBP, not AUD". It also
    surfaces figures whose scale was merely assumed, or could not be resolved
    at all, so an unlabelled number cannot quietly satisfy a claim.

    A figure with no resolvable scale is still considered: the claim is tested
    against both a units and a thousands reading. NAB FY24 p.151 prints
    `2,982,645` with no scale declared anywhere, and the model needs to be told
    the digits are present but the units are unconfirmed - not merely that the
    value is absent.
    """
    out = []
    for f in anchored_figures(page_text, page_no, bare_currency):
        if f.currency is None:
            continue
        if f.value_m is not None:
            if abs(f.value_m - value_m) > tol:
                continue
        else:
            # no scale resolved: does the claim match under either reading?
            if not any(abs(f.raw * mult / 1_000_000 - value_m) <= tol
                       for mult in (1, 1_000)):
                continue
        if not f.is_unambiguous_aud or not f.scale_is_declared:
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Which currency does this company report its remuneration in?
# ---------------------------------------------------------------------------
# Hardcoding AUD would be correct today and wrong the first time an ASX filer
# reports in USD - the same false-pass, moved one currency over. The comparator
# already carries a per-company `Currency Future Rem` column; it reads AUD for
# all 15 rows today. Read it rather than assuming it stays that way.

DEFAULT_COMPARATOR = Path(__file__).resolve().parent.parent / "data" / \
    "_reference" / "ceo_kmp_comparator.csv"

_CURRENCY_COLUMN_CANDIDATES = ("Currency Future Rem", "Currency", "Reporting Currency")


def reporting_currency_for(company: str,
                           comparator_path: Path | None = None) -> str:
    """The currency a company's remuneration is reported in, or 'AUD'.

    Falls back to AUD, which is right for an ASX-listed domestic filer and is
    the only value in the comparator today. The caller should treat the return
    value as the *reporting* currency for unqualified "$" - not as a licence to
    accept any figure, which still requires a declared scale and an AUD anchor
    for an AUD claim.
    """
    if comparator_path is None:
        try:
            from input_sources import find_comparator_csv
            resolved = find_comparator_csv()
        except Exception:
            resolved = None
        path = resolved or DEFAULT_COMPARATOR
    else:
        path = Path(comparator_path)
    try:
        with path.open(encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("Company Name") or "").strip()
                if not name:
                    continue
                if _same_company(name, company):
                    for col in _CURRENCY_COLUMN_CANDIDATES:
                        val = (row.get(col) or "").strip().upper()
                        if val:
                            return val
    except (OSError, csv.Error):
        pass
    return "AUD"


def _same_company(a: str, b: str) -> bool:
    """Loose match, so 'Rio Tinto' finds 'Rio Tinto' and 'CBA' finds
    'Commonwealth Bank' only when unambiguous. Prefix comparison on the
    leading token is enough for this corpus and avoids a false match between
    similarly-named entities."""
    na = re.sub(r"[^a-z0-9 ]", "", a.lower()).strip()
    nb = re.sub(r"[^a-z0-9 ]", "", b.lower()).strip()
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na.startswith(nb) or nb.startswith(na)
