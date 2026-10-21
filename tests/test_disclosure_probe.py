# -*- coding: utf-8 -*-
"""Tests for the Stage 7 disclosure probe (_check_disclosure_not_missed).

The probe checks whether Stage 5's remuneration assessment missed a disclosed
value by claiming ``not_disclosed`` when a supplied page actually publishes a
CEO realised-remuneration row.

Canonical cases:
- NAB FY24 p.145: publishes Andrew Irvine (part-year) realised row -> a
  "no_realised_disclosure" claim is a false negative and must be flagged CRITICAL.
- RIO FY24: genuinely has no realised table -> a "not_disclosed" claim is fine.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stage7_audit import _check_disclosure_not_missed  # noqa: E402

C = ROOT / "data" / "_cache" / "annual_report_text"
failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


def page(tk, yr, pno):
    d = json.loads((C / f"{tk}_{yr}.json").read_text(encoding="utf-8"))
    return {x["page_number"]: (x.get("text") or "") for x in d["pages"]}[pno]


# NAB FY24 p.145 publishes a realised CEO row; p.146 is governance prose only.
p145 = page("NAB", 2024, 145)
p146 = page("NAB", 2024, 146)

# RIO FY24 key pages have no realised CEO disclosure.
rio_pages = {}
for pno in (131, 138, 140):
    rio_pages[pno] = page("RIO", 2024, pno)


def _write_tmp_workspace(ticker, year, output_text, ar_pages_map, included_pages):
    """Create a temp workspace with the files the probe needs."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="probe_test_"))
    ws = tmp / f"{ticker}_{year}"
    ws.mkdir(parents=True)

    # AR files directory
    ar_dir = ws / "annual_reports"
    ar_dir.mkdir()
    # Copy a real AR PDF if available; otherwise create a placeholder
    real_pdf = ROOT / "data" / "companies" / f"{ticker}_{year}" / "annual_reports" / f"{ticker}_{year}_AR.pdf"
    if real_pdf.exists():
        shutil.copy(real_pdf, ar_dir / f"{ticker}_{year}_AR.pdf")

    # Remediation assessment markdown
    reports_dir = ws / "reports"
    reports_dir.mkdir()
    (reports_dir / f"{ticker}_{year}_Remuneration_Assessment.md").write_text(output_text, encoding="utf-8")

    # Stage 5 retrieval manifest with included pages
    sup_dir = ws / "supporting_data" / "stage5_remuneration"
    sup_dir.mkdir(parents=True)
    manifest = {
        "stage": "stage5",
        "ticker": ticker,
        "year": year,
        "sections": [
            {"key": "test", "selected_pages": included_pages}
        ]
    }
    (sup_dir / "retrieval_manifest_stage5.json").write_text(json.dumps(manifest), encoding="utf-8")

    return ws, tmp


print("=" * 80)
print("A. No non-disclosure claim -> no issues")
print("=" * 80)
ws, tmp = _write_tmp_workspace("NAB", 2024,
    "CEO realised remuneration of $1.74m is disclosed [p.145].",
    {145: p145}, [145])
issues = _check_disclosure_not_missed(ws)
check("no issues when output discloses realised pay", not issues, str(issues))
import shutil
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 80)
print("B. Non-disclosure claim + disclosed row on supplied page -> CRITICAL")
print("=" * 80)
ws, tmp = _write_tmp_workspace("NAB", 2024,
    "Realised remuneration is not disclosed in the annual report.",
    {145: p145}, [145])
issues = _check_disclosure_not_missed(ws)
check("CRITICAL issue flagged", any(i["status"] == "CRITICAL" for i in issues), str(issues))
check("check name is 'Disclosure not missed'", any(i["check"] == "Disclosure not missed" for i in issues), str(issues))
check("detail mentions disclosed row", any("disclosed CEO realised row" in i["detail"] for i in issues), str(issues))
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 80)
print("C. Non-disclosure claim + no supplied pages carry disclosure -> no issue")
print("=" * 80)
ws, tmp = _write_tmp_workspace("NAB", 2024,
    "Realised remuneration is not disclosed in the annual report.",
    {146: p146}, [146])
issues = _check_disclosure_not_missed(ws)
check("no issues when only governance page supplied", not issues, str(issues))
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 80)
print("D. Non-disclosure claim + RIO FY24 (genuine non-disclosure) -> no issue")
print("=" * 80)
ws, tmp = _write_tmp_workspace("RIO", 2024,
    "Realised remuneration is not disclosed in the annual report.",
    rio_pages, [131, 138, 140])
issues = _check_disclosure_not_missed(ws)
check("no issues for RIO FY24 genuine non-disclosure", not issues, str(issues))
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 80)
print("E. Non-disclosure claim + no manifest -> no issue (no crash)")
print("=" * 80)
ws, tmp = _write_tmp_workspace("NAB", 2024,
    "Realised remuneration is not disclosed in the annual report.",
    {}, [])
# Remove manifest to test missing-manifest path
manifest_path = ws / "supporting_data" / "stage5_remuneration" / "retrieval_manifest_stage5.json"
manifest_path.unlink(missing_ok=True)
issues = _check_disclosure_not_missed(ws)
check("no crash when manifest missing", not issues, str(issues))
shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("disclosure_probe OK.")
