# -*- coding: utf-8 -*-
"""Verify the checkpoint wrapper resumes completed (extractor, fiscal_year)
calls without re-invoking the LLM, and that failed calls are recorded.

This exercises the wrapper in isolation against a fake _call_llm, so it does
not depend on a live API or on the qwen rate limit.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\jurok\VI_Reports1")
sys.path.insert(0, str(ROOT / "src"))

import performance_table as pt  # noqa: E402
from pipeline_db import init_run_db  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="ckpt_"))
db, run_id = init_run_db(tmp)

calls = []


def fake_call_llm(client, model, prompt_text, user_content, logger=None,
                  fallback_model=None, max_retries=3, call_name="llm_call",
                  supporting_data_dir=None, json_validation_retries=2):
    calls.append(call_name)
    return {"echo": call_name, "model": model}


# Bind the wrapper to the fake so no network is touched.
pt._call_llm = fake_call_llm
wrapped = pt._make_checkpointed_call(db, run_id, None)

print("A. First call invokes the LLM and checkpoints")
r = wrapped(None, "m", "p", "u", call_name="prompt_3_fy2026")
check("returns parsed result", r == {"echo": "prompt_3_fy2026", "model": "m"})
check("LLM invoked once", calls == ["prompt_3_fy2026"])
cp = db.get_checkpoint_status(run_id, "prompt_3", 2026)
check("checkpoint marked completed", cp and cp["status"] == "completed")
check("result_json stored", cp and json.loads(cp["result_json"]) == r)

print("B. Resume skips the LLM entirely")
r2 = wrapped(None, "m", "p", "u", call_name="prompt_3_fy2026")
check("returns stored result", r2 == r)
check("LLM NOT invoked again", calls == ["prompt_3_fy2026"])

print("C. A different year is a separate checkpoint")
r3 = wrapped(None, "m", "p", "u", call_name="prompt_3_fy2025")
check("second year invokes LLM", calls == ["prompt_3_fy2026", "prompt_3_fy2025"])
cp2 = db.get_checkpoint_status(run_id, "prompt_3", 2025)
check("second year checkpointed", cp2 and cp2["status"] == "completed")

print("D. A non-yeared call keys to year 0")
r4 = wrapped(None, "m", "p", "u", call_name="prompt_1a")
check("prompt_1a invokes LLM", calls[-1] == "prompt_1a")
cp3 = db.get_checkpoint_status(run_id, "prompt_1a", 0)
check("prompt_1a checkpointed at year 0", cp3 and cp3["status"] == "completed")

print("E. A failed call is recorded as failed, not completed")
def failing(client, model, prompt_text, user_content, logger=None,
            fallback_model=None, max_retries=3, call_name="llm_call",
            supporting_data_dir=None, json_validation_retries=2):
    raise RuntimeError("boom 429 rate limit")

pt._call_llm = failing
try:
    wrapped(None, "m", "p", "u", call_name="prompt_3_fy2024")
    check("failed call raises", False, "no exception raised")
except RuntimeError as e:
    check("failed call raises", "boom" in str(e))
cp4 = db.get_checkpoint_status(run_id, "prompt_3", 2024)
check("failed call checkpointed as failed", cp4 and cp4["status"] == "failed")
check("failed call error stored", cp4 and "boom" in (cp4.get("error") or ""))

print("F. call_log rows exist for every attempt")
stats = db.get_call_stats(run_id)
check("call_log has rows", stats.get("total_calls", 0) >= 4, str(stats))
check("successes recorded", stats.get("successes", 0) >= 3, str(stats))
check("errors recorded", stats.get("errors", 0) >= 1, str(stats))

print()
print("=" * 88)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("Checkpoint resume integration OK.")