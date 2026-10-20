"""
Deterministic EPS extraction from Annual Report pages.

Stage 4 asks the model for earnings per share out of the individual annual
reports.  That works, but it costs a model call per year and the free-tier
rate limit regularly drops some of them.  The figures are also easy to
attribute to the wrong year: an AR prints the current year and the
comparative side by side, and a model that reads the columns loosely reports
the comparative under the current year.  AMP FY2024 was reported as 9.3c,
which is FY2023's number - FY2024's reported EPS is 5.7c.

This module reads the EPS rows directly, so the column header decides which
number belongs to which year:

  * a table row such as ``EPS - underlying (cps) 1 11.3 6.1 5.2 9.0`` under a
    column header such as ``FY 25 2H 25 1H 25 FY 24``;
  * prose such as "Basic (loss)/ earnings per share for the year ended
    31 December 2021 on a statutory basis was (7.6) cents per share".

Values are in cents per share, matching the rest of the performance table.
"""

import re

# A column header reference: "FY23", "FY 23", "2H 23", "2021".
_COLUMN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(FY|[12]H)\s*)?(\d{2,4})(?![A-Za-z0-9])", re.IGNORECASE)

# The label and unit between "EPS" and the numbers.
_EPS_TAIL_RE = re.compile(
    r"^\s*[–—\-]?\s*(?P<label>[A-Za-z][A-Za-z /\-]{0,30}?)\s*"
    r"(?:\((?P<unit>[^)]*)\))?\s*(?P<numbers>[\(\)\d.,\s]*\d[\(\)\d.,\s]*)")

_NUMBER_TOKEN_RE = re.compile(r"^\(?-?[\d,]*\.?\d+\)?$")


def _two_or_four_digit_year(digits: str, prefix):
    """Map a header token to a calendar year, or None if it is not one.

    PDF text extraction glues footnote markers onto the neighbouring token,
    so a column header can arrive as "1H 241" (the 1H 24 column plus
    footnote 1) or "FY 231" (the FY 23 column plus footnote 1).  A prefixed
    token with three digits is therefore read as a two-digit year with the
    trailing digit being the footnote marker.
    """
    if len(digits) == 4:
        year = int(digits)
        return year if 1980 <= year <= 2100 else None
    if len(digits) == 3 and prefix:
        return 2000 + int(digits[:2])
    if len(digits) == 2:
        if not prefix:
            return None
        return 2000 + int(digits)
    return None


def header_columns(line: str):
    """Year columns declared by a header line, in order.

    Each entry is ``(year, is_half_year)``.  Half-year columns ("2H 25") are
    returned because they occupy a column slot, but they are never a match
    for a full financial year.
    """
    columns = []
    for match in _COLUMN_RE.finditer(line):
        prefix = (match.group(1) or "").upper()
        year = _two_or_four_digit_year(match.group(2), prefix)
        if year is None:
            continue
        columns.append((year, prefix in ("1H", "2H")))
    return columns


def _parse_numbers(text: str):
    """Numbers at the start of ``text``; a percentage ends the row."""
    values = []
    for token in text.split():
        if "%" in token:
            break
        cleaned = token.strip(".,;:")
        if not _NUMBER_TOKEN_RE.match(cleaned):
            if values:
                break
            continue
        negative = cleaned.startswith("(") and cleaned.endswith(")")
        try:
            value = float(cleaned.strip("()").replace(",", ""))
        except ValueError:
            break
        values.append(-value if negative else value)
    return values


def _basis_from_label(label: str) -> str:
    if re.search(r"underlying|adjusted|normalis|normaliz|core", label, re.IGNORECASE):
        return "underlying"
    return "statutory"


def eps_rows_on_page(page_text: str):
    """Every EPS row on a page: (basis, values, line_index)."""
    rows = []
    for index, line in enumerate(page_text.split("\n")):
        for match in re.finditer(r"\bEPS\b", line):
            tail = line[match.end():]
            parsed = _EPS_TAIL_RE.match(tail)
            if not parsed:
                continue
            values = _parse_numbers(parsed.group("numbers"))
            if not values:
                continue
            unit = parsed.group("unit") or ""
            rows.append((_basis_from_label(parsed.group("label")), values, index, unit))
            break  # one row per line
    return rows


def _align(rows, header_line_index, columns, target_index):
    """Pick the value for ``target_index`` from a row, if the row fits."""
    if not columns or target_index is None or target_index >= len(columns):
        return None
    count = len(columns)
    values = rows
    if len(values) > count:
        # Footnote markers sit between the label and the first value.
        values = values[len(values) - count:]
    elif len(values) < count:
        return None
    return values[target_index]


def _prose_value(match):
    """Value from a prose match, honouring a parenthesised negative."""
    value = float(match.group(2).replace(",", ""))
    if match.group(1):
        return -value
    return value


def extract_eps(pages, target_fy: int, header_window: int = 60):
    """Reported and underlying EPS for ``target_fy`` from AR pages.

    ``pages`` is a list of cached AR page dicts.  Returns a dict with
    ``eps``, ``underlying_eps`` and their source pages, containing only the
    keys that were found.
    """
    if not pages:
        return {}

    result = {}

    for page in pages:
        text = page.get("text") or ""
        if not text:
            continue
        page_no = page.get("page_number")
        lines = text.split("\n")

        # Header candidates on this page, in document order.
        headers = []
        for index, line in enumerate(lines):
            columns = header_columns(line)
            if len(columns) >= 2:
                headers.append((index, columns))

        for basis, values, row_index, unit in eps_rows_on_page(text):
            if "%" in unit:
                continue
            best = None
            for header_index, columns in headers:
                if header_index >= row_index:
                    continue
                if row_index - header_index > header_window:
                    continue
                target_index = None
                for position, (year, is_half) in enumerate(columns):
                    if year == target_fy and not is_half:
                        target_index = position
                        break
                if target_index is None:
                    continue
                value = _align(values, header_index, columns, target_index)
                if value is None:
                    continue
                if best is None or header_index > best[0]:
                    best = (header_index, value)
            if best is None:
                continue
            key = "underlying_eps" if basis == "underlying" else "eps"
            page_key = f"{key}_source_page"
            if result.get(key) is None:
                result[key] = best[1]
                result[page_key] = page_no
                result[f"{key}_basis"] = basis

    # Prose form: "earnings per share for the year ended 31 December 2021 on a
    # statutory basis was (7.6) cents per share".
    for page in pages:
        text = page.get("text") or ""
        if not text:
            continue
        flat = re.sub(r"\s+", " ", text)
        if not re.search(r"(?:year|period) ended[^.]{0,80}?\b" + str(target_fy) + r"\b", flat,
                         re.IGNORECASE):
            continue
        page_no = page.get("page_number")

        match = re.search(
            r"statutory basis was\s*(\()?\s*([\d,]+(?:\.\d+)?)\s*(\))?\s*cents?\s+per share",
            flat, re.IGNORECASE)
        if match and result.get("eps") is None:
            result["eps"] = _prose_value(match)
            result["eps_source_page"] = page_no
            result["eps_basis"] = "statutory"

        match = re.search(
            r"underlying basis,?\s+the earnings per share was\s*"
            r"(\()?\s*([\d,]+(?:\.\d+)?)\s*(\))?\s*cents?\s+per share",
            flat, re.IGNORECASE)
        if match and result.get("underlying_eps") is None:
            result["underlying_eps"] = _prose_value(match)
            result["underlying_eps_source_page"] = page_no
            result["underlying_eps_basis"] = "underlying"

    return result
