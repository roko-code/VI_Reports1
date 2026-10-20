"""
CEO remuneration framework extraction from the Annual Report.

Stage 10 renders a "CEO Remuneration Framework" table showing the target and
maximum opportunity for fixed pay, STI cash, STI equity and LTI.  The two
sources used so far each fail for some issuers:

* the model's Section 4 prose - the unit varies by issuer ("$1,715 k",
  "$21.0m", "$3,885,000") and the wording varies from run to run, so the
  renderer produced 0.00 in every cell for AMP;
* the issuer's "Awarded Remuneration" table - only exists for issuers that
  publish that exact heading, so it returns nothing for AMP.

This module derives the framework from the Annual Report itself:

  fixed pay       the CEO row of the "actual remuneration received" table
  STI target/max  "Target STI opportunity is X% of ... FR" and
                  "Maximum STI opportunity is Y% of target STI"
  STI cash/equity "Z% of the STI is delivered as cash" (or the awarded row)
  LTI             "The face value of the ... LTI ... opportunity is X% of FR"

Every derived figure is reconciled against the remuneration mix the issuer
discloses at maximum opportunity, so a mis-parsed table is rejected rather
than rendered.  No LLM call is involved, so this does not consume the
free-tier rate limit budget.

The figures are returned in $millions.
"""

import re
from typing import Optional

# Dash placeholders used in AR tables for "nil" / "not applicable".
_DASH_CHARS = "\u2013\u2014\u2212-"
_NUM_RE = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
_PCT_RE = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?%$")
_YEAR_MIN, _YEAR_MAX = 1980, 2100


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

def _tokenize_row(text: str):
    """Split a table row into positional values.

    Returns a list of ``(value, is_percent, is_dash)``.  A dash ("-", en
    dash) is a nil placeholder and yields ``(None, False, True)``; a token
    that is not a number yields ``(None, False, False)`` and is dropped
    later, so the numeric columns stay aligned.
    """
    values = []
    for token in text.split():
        stripped = token.strip("(),;")
        if not stripped:
            continue
        if all(ch in _DASH_CHARS for ch in stripped):
            values.append((None, False, True))
            continue
        if _PCT_RE.match(stripped):
            try:
                values.append((float(stripped.strip("()%").replace(",", "")), True, False))
            except ValueError:
                values.append((None, False, False))
            continue
        if _NUM_RE.match(stripped):
            try:
                values.append((float(stripped.strip("()").replace(",", "")), False, False))
            except ValueError:
                values.append((None, False, False))
            continue
        values.append((None, False, False))
    return values


def _numeric_run(tokens):
    """Return the numeric/dash portion of a token list.

    Text tokens are dropped (they cannot be part of a numeric column) but dash
    placeholders are kept so the surviving columns stay aligned.
    """
    run = []
    for value, is_pct, is_dash in tokens:
        if value is None and not is_dash:
            continue
        run.append((value, is_pct))
    return run


def _match_name(line: str, name: str):
    """Locate the CEO's name in a table row.

    Returns ``(start, end)`` of the matched text, or None.  The AR may refer
    to the CEO by first name only ("Alexis was appointed Chief Executive
    Officer"), so a single-word name matches a row that starts with that
    word.
    """
    if not name:
        return None
    stripped = line.lstrip()
    offset = len(line) - len(stripped)
    index = stripped.find(name)
    if index >= 0:
        return offset + index, offset + index + len(name)
    first = name.split()[0]
    match = re.match(re.escape(first) + r"\b", stripped)
    if match:
        return offset, offset + match.end()
    return None


def _row_name(line: str, start: int, end: int) -> str:
    """The name as the row states it, e.g. "Alexis George"."""
    text = line[start:end]
    extra = re.match(r"\s+([A-Z][a-z'’\-]+)", line[end:])
    if extra:
        text += extra.group(1)
    return text.strip()


def _row_values_for_name(line: str, name: str):
    """Tokenise the part of ``line`` that follows the CEO's name."""
    matched = _match_name(line, name)
    if not matched:
        return [], None
    return _tokenize_row(line[matched[1]:]), _row_name(line, matched[0], matched[1])


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def _find_ceo_row(pages, name: str, page_filter, min_tokens: int):
    """Return (page_number, values, row_name) for the CEO's row."""
    for page in pages:
        text = page.get("text") or ""
        if not page_filter(text):
            continue
        for line in text.splitlines():
            if name and not _match_name(line, name):
                continue
            values, row_name = _row_values_for_name(line, name)
            values = _numeric_run(values)
            if len(values) < min_tokens:
                continue
            return page.get("page_number"), values, row_name
    return None, [], None


def _find_realised_pay(pages, name: str, target_fy=None):
    """Extract fixed pay from the "actual remuneration received" table.

    Returns a dict with the CEO's fixed remuneration plus the component
    breakdown, in the AR's own unit, or None.  ``target_fy`` rejects a row
    for a different year - the table shows the current year and the prior
    year, and only the current year's row carries the CEO's name.
    """
    def qualifies(text):
        low = text.lower()
        if not re.search(r"remuneration received|actual remuneration|realised remuneration", low):
            return False
        return "fixed" in low and "total" in low

    page_number, values, row_name = _find_ceo_row(pages, name, qualifies, min_tokens=4)
    if not values:
        return None

    # Drop a leading year token (the row is usually "Name <year> ...").
    row_year = None
    if values and values[0][0] is not None:
        first = values[0][0]
        if float(first).is_integer() and _YEAR_MIN <= first <= _YEAR_MAX and len(values) >= 5:
            row_year = int(first)
            values = values[1:]
    if target_fy is not None and row_year is not None and row_year != int(target_fy):
        return None

    numeric = [v for v, _ in values if v is not None]
    if len(numeric) < 2:
        return None

    fixed = numeric[0]
    total = numeric[-1]
    components = numeric[1:-1]

    # The row's own total must reconcile against its components, otherwise the
    # column mapping is wrong and the figures cannot be trusted.
    if abs(total - (fixed + sum(components))) > max(1.0, 0.01 * abs(total)):
        return None

    return {
        "page": page_number,
        "fixed": fixed,
        "cash_sti": components[0] if len(components) > 0 else None,
        "total": total,
        "components": components,
        "name": row_name,
    }


def _find_sti_outcomes(pages, name: str):
    """Extract the CEO's STI target and maximum opportunity.

    Uses the issuer's "STI outcomes" table, which normally reports the
    pro-rated target opportunity, the amount awarded, its cash / deferred
    split, and the award as a percentage of the target and the maximum.
    """
    def qualifies(text):
        low = text.lower()
        if "sti" not in low:
            return False
        if "opportunity" not in low:
            return False
        if "cash" not in low:
            return False
        return bool(re.search(r"share right|deferred|equity", low))

    page_number, values, row_name = _find_ceo_row(pages, name, qualifies, min_tokens=4)
    if not values:
        return None

    numeric = [v for v, _ in values if v is not None]
    if len(numeric) < 4:
        return None

    target, awarded, cash, deferred = numeric[0], numeric[1], numeric[2], numeric[3]

    # cash + deferred must reconcile against the amount awarded.
    if abs(awarded - (cash + deferred)) > max(1.0, 0.02 * abs(awarded)):
        return None
    if target <= 0 or awarded <= 0:
        return None

    result = {
        "page": page_number,
        "target": target,
        "awarded": awarded,
        "cash": cash,
        "deferred": deferred,
        "cash_pct": 100.0 * cash / awarded,
        "name": row_name,
    }

    # Percentage columns: "... as % of pro rated target" and "... as % of
    # ... target STI max".  Use them to recover the maximum opportunity.
    pcts = [v for v, is_pct in values if v is not None and is_pct]
    if len(pcts) >= 2:
        pct_of_target, pct_of_max = pcts[-2], pcts[-1]
        if 0 < pct_of_target <= 100:
            derived = awarded / (pct_of_target / 100.0)
            if abs(derived - target) <= max(2.0, 0.02 * abs(target)):
                result["target_check"] = derived
        if 0 < pct_of_max <= 100:
            result["max"] = awarded / (pct_of_max / 100.0)
    return result


def _percent_rules(text: str):
    """Extract the framework's percentage-of-fixed-remuneration rules."""
    rules = {"sti_target_pct_of_fr": None, "sti_max_pct_of_target": None,
             "lti_pct_of_fr": None, "sti_cash_pct": None}
    flat = re.sub(r"\s+", " ", text)

    # "Target STI opportunity is 100% of fixed remuneration (FR)"
    for match in re.finditer(
        r"target\s+STI\s+opportunity\s+is\s+(\d+(?:\.\d+)?)\s*%\s*of\s+[^.]*?FR",
        flat, re.IGNORECASE,
    ):
        rules["sti_target_pct_of_fr"] = float(match.group(1))
        break

    # "Maximum STI opportunity is 150% of target STI"
    for match in re.finditer(
        r"[Mm]aximum\s+STI\s+opportunity\s+is\s+(\d+(?:\.\d+)?)\s*%\s*of\s+target",
        flat, re.IGNORECASE,
    ):
        rules["sti_max_pct_of_target"] = float(match.group(1))
        break

    # "60% of the STI is delivered as cash and 40% is deferred into equity"
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)\s*%\s*of\s+the\s+STI\s+is\s+delivered\s+as\s+cash", flat, re.IGNORECASE
    ):
        rules["sti_cash_pct"] = float(match.group(1))
        break

    # "The face value of the 2025 LTI performance rights opportunity is 100% of FR"
    for match in re.finditer(
        r"LTI[^.]{0,160}?opportunity\s+is\s+(\d+(?:\.\d+)?)\s*%\s*of\s+FR",
        flat, re.IGNORECASE,
    ):
        rules["lti_pct_of_fr"] = float(match.group(1))
        break

    return rules


def _find_mix_at_maximum(pages):
    """Extract the issuer's disclosed remuneration mix at maximum opportunity.

    Returns a dict of component -> percent, or None.
    """
    for page in pages:
        text = page.get("text") or ""
        low = text.lower()
        marker = re.search(r"remuneration mix at maximum opportunity", low)
        if not marker:
            continue
        region = text[marker.start(): marker.start() + 1200]
        mix = {}
        for label, pct in re.findall(
            r"([A-Za-z][A-Za-z0-9 &/\-]{2,40}?)\s+(\d+(?:\.\d+)?)\s*%", region
        ):
            key = label.strip().lower()
            if "fixed" in key:
                mix.setdefault("fixed", float(pct))
            elif "sti" in key and "cash" in key:
                mix.setdefault("sti_cash", float(pct))
            elif "sti" in key and re.search(r"defer|equity|share", key):
                mix.setdefault("sti_equity", float(pct))
            elif "lti" in key or "performance right" in key:
                mix.setdefault("lti", float(pct))
        if mix:
            return mix
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CEO_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:[-'’][A-Z][a-z]+)?(?:\s+[A-Z][a-z]+(?:[-'’][A-Z][a-z]+)?)?)"
    r"\s+(?:was\s+)?appointed\s+(?:as\s+)?(?:the\s+|AMP['’]s\s+)?"
    r"Chief Executive Officer\b[^.]{0,80}?\bof\s+([A-Z][A-Za-z&.\- ]{2,40}?)\s+Limited",
)


def detect_ceo_name(pages, ticker: Optional[str] = None):
    """The group CEO's name, from the AR's own appointment statement.

    Used only as a last resort, when the model did not report a CEO name for
    any year.  The pattern requires an appointment to Chief Executive Officer
    *of the company*, so executives who ran a business unit ("Chief Executive
    Officer, AMP New Zealand") are not picked up.
    """
    if not pages:
        return None
    for page in pages:
        text = page.get("text") or ""
        for match in _CEO_NAME_RE.finditer(text):
            name = match.group(1).strip()
            entity = match.group(2).strip()
            if ticker and entity.split()[0].upper() != ticker.upper():
                continue
            return name
    return None


def _find_statutory_pay(pages, name: str, target_fy=None):
    """Extract the CEO's statutory remuneration from the AR.

    The statutory table has one column per remuneration component and a
    total, so the same reconciliation as the realised-pay table applies.
    """
    def qualifies(text):
        low = text.lower()
        if "statutory remuneration" not in low:
            return False
        # The realised-pay page also refers to the statutory table, so the
        # statutory table is identified by its own definition of what
        # statutory remuneration is.
        if "actual remuneration" in low or "remuneration received" in low:
            return False
        return bool(re.search(r"accounting expense|cash salary|long-term benefits", low))

    page_number, values, row_name = _find_ceo_row(pages, name, qualifies, min_tokens=5)
    if not values:
        return None

    row_year = None
    if values and values[0][0] is not None:
        first = values[0][0]
        if float(first).is_integer() and _YEAR_MIN <= first <= _YEAR_MAX and len(values) >= 6:
            row_year = int(first)
            values = values[1:]
    if target_fy is not None and row_year is not None and row_year != int(target_fy):
        return None

    numeric = [v for v, _ in values if v is not None]
    if len(numeric) < 3:
        return None

    total = numeric[-1]
    components = numeric[:-1]
    if total <= 0:
        return None
    if abs(total - sum(components)) > max(1.0, 0.01 * abs(total)):
        return None

    return {"page": page_number, "total": total, "components": components, "name": row_name}


def extract_ceo_statutory_pay(pages, ceo_name: Optional[str] = None, target_fy=None, logger=None):
    """The CEO's statutory remuneration from the AR, in $millions."""
    if not pages or not ceo_name:
        return None
    found = _find_statutory_pay(pages, ceo_name, target_fy=target_fy)
    if not found:
        return None

    total = found["total"]
    value_m = total / 1_000_000.0 if total >= 100_000 else total / 1_000.0
    if logger:
        logger.info(
            f"CEO statutory pay from AR: {value_m:.3f}m (page {found['page']})"
        )
    return {
        "value_m": value_m,
        "source_method": "disclosed_statutory",
        "source_page": found["page"],
        "notes": f"CEO: {found.get('name') or ceo_name}",
    }


def extract_ceo_realised_pay(pages, ceo_name: Optional[str] = None, target_fy=None, logger=None):
    """The CEO's realised remuneration from the AR, in $millions.

    Uses the issuer's voluntary "actual remuneration received" table, which
    is the same table the framework figures come from.  The table is published
    in dollars by some issuers and in thousands by others, so the unit is
    detected from the size of the total.

    Returns a dict with ``value_m`` and ``source_page``, or None.
    """
    if not pages or not ceo_name:
        return None
    realised = _find_realised_pay(pages, ceo_name, target_fy=target_fy)
    if not realised:
        return None

    total = realised["total"]
    if total <= 0:
        return None

    # A total in the millions of units is stated in dollars; anything smaller
    # is in thousands.
    if total >= 100_000:
        value_m = total / 1_000_000.0
    else:
        value_m = total / 1_000.0

    if logger:
        logger.info(
            f"CEO realised pay from AR: {value_m:.3f}m "
            f"(page {realised['page']}, total {total:,.0f})"
        )

    return {
        "value_m": value_m,
        "source_page": realised["page"],
        "source_method": "disclosed",
        "notes": f"CEO: {realised.get('name') or ceo_name}",
    }


def extract_ceo_framework(pages, ceo_name: Optional[str] = None, logger=None):
    """Derive the CEO remuneration framework from Annual Report pages.

    ``pages`` is the list of cached AR page dicts.  ``ceo_name`` is the CEO's
    name as it appears in the AR (e.g. "Alexis George").

    Returns a dict with the target and maximum opportunity components in
    $millions, or None when the AR does not disclose enough to derive them or
    the extracted figures fail reconciliation.
    """
    if not pages:
        return None

    name = (ceo_name or "").strip()
    if not name:
        return None

    realised = _find_realised_pay(pages, name)
    sti = _find_sti_outcomes(pages, name)

    if realised is None and sti is None:
        return None

    rules = {"sti_target_pct_of_fr": None, "sti_max_pct_of_target": None,
             "lti_pct_of_fr": None, "sti_cash_pct": None}
    for page in pages:
        page_rules = _percent_rules(page.get("text") or "")
        for key, value in page_rules.items():
            if value is not None and rules[key] is None:
                rules[key] = value

    # --- Fixed remuneration -------------------------------------------------
    unit_scale = 1000.0  # AR tables are published in $'000 by convention.

    fixed = None
    if realised:
        fixed = realised["fixed"]
    if fixed is None:
        return None

    # --- STI target / maximum ----------------------------------------------
    sti_target_pct = rules["sti_target_pct_of_fr"]
    sti_target = None
    if sti_target_pct is not None:
        sti_target = fixed * sti_target_pct / 100.0
    elif sti:
        sti_target = sti["target"]

    if sti_target is None:
        return None

    # The STI outcomes table states the target opportunity in the same unit as
    # the realised pay table; use it to check the percentage-derived figure.
    if sti and realised:
        scale_ratio = sti["target"] / realised["fixed"] if realised["fixed"] else None
        if scale_ratio and abs(sti_target - sti["target"]) > max(2.0, 0.02 * abs(sti["target"])):
            if logger:
                logger.info(
                    f"CEO framework: STI target {sti_target:.0f} "
                    f"({rules['sti_target_pct_of_fr']}% of FR) disagrees with the AR's "
                    f"target opportunity {sti['target']:.0f} (p.{sti['page']}); "
                    f"using the AR table."
                )
            sti_target = sti["target"]

    sti_max_pct = rules["sti_max_pct_of_target"]
    sti_max = None
    if sti_max_pct is not None:
        sti_max = sti_target * sti_max_pct / 100.0
    elif sti and sti.get("max"):
        sti_max = sti["max"]
    if sti_max is None:
        return None

    # --- STI cash / deferred split -----------------------------------------
    cash_pct = rules["sti_cash_pct"]
    if cash_pct is None and sti:
        cash_pct = sti["cash_pct"]
    if cash_pct is None:
        return None

    # --- Long term incentive ------------------------------------------------
    lti_pct = rules["lti_pct_of_fr"]
    if lti_pct is None:
        return None
    lti = fixed * lti_pct / 100.0

    # --- Assemble (AR unit is $'000) ---------------------------------------
    def to_millions(value):
        return value / unit_scale

    target_fixed = to_millions(fixed)
    target_sti_cash = to_millions(sti_target * cash_pct / 100.0)
    target_sti_equity = to_millions(sti_target * (100.0 - cash_pct) / 100.0)
    target_lti = to_millions(lti)

    max_fixed = target_fixed
    max_sti_cash = to_millions(sti_max * cash_pct / 100.0)
    max_sti_equity = to_millions(sti_max * (100.0 - cash_pct) / 100.0)
    max_lti = target_lti

    total_target = target_fixed + target_sti_cash + target_sti_equity + target_lti
    total_max = max_fixed + max_sti_cash + max_sti_equity + max_lti

    # --- Validate against the disclosed remuneration mix --------------------
    validation = {}
    mix = _find_mix_at_maximum(pages)
    if mix:
        computed = {
            "fixed": 100.0 * max_fixed / total_max,
            "sti_cash": 100.0 * max_sti_cash / total_max,
            "sti_equity": 100.0 * max_sti_equity / total_max,
            "lti": 100.0 * max_lti / total_max,
        }
        deviations = {
            key: computed[key] - mix[key] for key in mix if key in computed
        }
        validation["disclosed_mix"] = mix
        validation["computed_mix"] = {k: round(v, 2) for k, v in computed.items()}
        validation["deviation_pp"] = {k: round(v, 2) for k, v in deviations.items()}
        if deviations and max(abs(v) for v in deviations.values()) > 1.5:
            if logger:
                logger.warning(
                    "CEO framework rejected: computed mix deviates from the AR's "
                    f"disclosed mix by more than 1.5pp ({validation['deviation_pp']})."
                )
            return None

    # The cash STI realised must agree with the STI outcomes table.
    if realised and sti and realised.get("cash_sti") and sti.get("cash"):
        if abs(realised["cash_sti"] - sti["cash"]) > max(1.0, 0.05 * max(1.0, sti["cash"])):
            if logger:
                logger.warning(
                    f"CEO framework rejected: realised cash STI {realised['cash_sti']:.0f} "
                    f"disagrees with the STI outcomes table {sti['cash']:.0f}."
                )
            return None
        validation["cash_sti_agreement"] = True

    if logger:
        logger.info(
            f"CEO framework from AR: fixed {target_fixed:.3f}, "
            f"STI cash {target_sti_cash:.3f}, STI equity {target_sti_equity:.3f}, "
            f"LTI {target_lti:.3f} (target total {total_target:.3f}, "
            f"max total {total_max:.3f})"
        )

    return {
        "fixed": target_fixed,
        "sti_cash": target_sti_cash,
        "sti_equity": target_sti_equity,
        "lti": target_lti,
        "max_fixed": max_fixed,
        "max_sti_cash": max_sti_cash,
        "max_sti_equity": max_sti_equity,
        "max_lti": max_lti,
        "total_target": total_target,
        "total_max": total_max,
        "source_pages": {
            "realised_pay": realised["page"] if realised else None,
            "sti_outcomes": sti["page"] if sti else None,
        },
        "rules": rules,
        "validation": validation,
        "method": "ar_statutory",
    }
