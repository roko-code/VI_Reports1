# -*- coding: utf-8 -*-
"""Run every test suite and report pass/fail per file."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
TESTS = sorted((ROOT / "tests").glob("test_*.py"))

overall_fail = 0
for t in TESTS:
    print("=" * 88)
    print(f"SUITE: {t.name}")
    p = subprocess.run(
        [sys.executable, str(t)],
        cwd=str(ROOT / "src"),
        capture_output=True, text=True,
    )
    out = (p.stdout or "") + (p.stderr or "")
    # Print only the summary lines to keep output tight.
    keep = [ln for ln in out.splitlines()
            if ln.strip().startswith(("PASS", "FAIL", "OK.", "FAILED", "SUITE", "==="))]
    for ln in keep:
        print(" ", ln)
    if p.returncode != 0:
        overall_fail += 1
        print(f"  >>> {t.name} exited {p.returncode}")

print("=" * 88)
if overall_fail:
    print(f"{overall_fail} SUITE(S) FAILED")
    sys.exit(1)
print("ALL SUITES PASSED")