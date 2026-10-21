# -*- coding: utf-8 -*-
"""Tests for the rate limiter: paid vs free model detection, RATE_LIMIT_MODE,
and quota-exceeded handling.

These tests reset the bucket singleton between cases so each case starts fresh.
RATE_LIMIT_MODE is a module-level variable read at import time, so tests set
rate_limiter.RATE_LIMIT_MODE directly rather than os.environ.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import rate_limiter  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        if detail:
            print(f"        {detail}")
        failures.append(name)


def reset():
    """Clear all buckets and reset mode so each test starts fresh."""
    rate_limiter.reset_all_buckets()
    rate_limiter.RATE_LIMIT_MODE = "auto"


# ---------------------------------------------------------------------------
# A. Paid models are not throttled under auto mode
# ---------------------------------------------------------------------------
print("=" * 80)
print("A. Paid models (no :free suffix) are not throttled under auto mode")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "auto"
b = rate_limiter.get_bucket("paid_stage", models=("anthropic/claude-3.5-sonnet", "openai/gpt-4o"))
check("paid bucket is disabled", not b.enabled)
check("paid bucket rate is 0", b.rate == 0.0)
check("paid bucket tokens is 0", b.tokens == 0.0)
check("paid bucket stores models", b.models == ("anthropic/claude-3.5-sonnet", "openai/gpt-4o"))

start = time.time()
wait = b.take()
elapsed = time.time() - start
check("paid take() returns immediately (wait=0)", wait == 0.0)
check("paid take() does not block", elapsed < 0.1)

# ---------------------------------------------------------------------------
# B. Free models are throttled under auto mode
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("B. Free models (:free suffix) are throttled under auto mode")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "auto"
b = rate_limiter.get_bucket("free_stage", models=("nvidia/nemotron-3-ultra-550b-a55b:free",))
check("free bucket is enabled", b.enabled)
check("free bucket rate > 0", b.rate > 0.0)
check("free bucket tokens > 0", b.tokens > 0.0)
check("free bucket stores models", b.models == ("nvidia/nemotron-3-ultra-550b-a55b:free",))

# ---------------------------------------------------------------------------
# C. RATE_LIMIT_MODE=on forces throttling even for paid models
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("C. RATE_LIMIT_MODE=on forces throttling for paid models")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "on"
b = rate_limiter.get_bucket("forced_stage", models=("anthropic/claude-3.5-sonnet",))
check("forced bucket is enabled", b.enabled)
check("forced bucket rate > 0", b.rate > 0.0)

# ---------------------------------------------------------------------------
# D. RATE_LIMIT_MODE=off disables throttling for all models
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("D. RATE_LIMIT_MODE=off disables throttling for all models")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "off"
b = rate_limiter.get_bucket("off_stage", models=("nvidia/nemotron-3-ultra-550b:free",))
check("off bucket is disabled even for free model", not b.enabled)
check("off bucket rate is 0", b.rate == 0.0)

# ---------------------------------------------------------------------------
# E. Quota exceeded is not retried when bucket is disabled
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("E. 'quota exceeded' is not retried when bucket disabled")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "auto"
b = rate_limiter.get_bucket("quota_stage", models=("anthropic/claude-3.5-sonnet",))
check("quota bucket is disabled", not b.enabled)

err_str = "quota exceeded"
is_429 = (
    "429" in err_str or
    "rate limit" in err_str or
    "too many requests" in err_str or
    ("quota exceeded" in err_str and b.enabled)
)
check("quota exceeded not flagged as 429 when disabled", not is_429)

reset()
rate_limiter.RATE_LIMIT_MODE = "auto"
b = rate_limiter.get_bucket("quota_free", models=("model:free",))
is_429 = (
    "429" in err_str or
    "rate limit" in err_str or
    "too many requests" in err_str or
    ("quota exceeded" in err_str and b.enabled)
)
check("quota exceeded flagged as 429 when enabled", is_429)

# ---------------------------------------------------------------------------
# F. 429 errors are always flagged regardless of enabled state
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("F. 429 errors are flagged regardless of bucket state")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "auto"
b = rate_limiter.get_bucket("http429", models=("anthropic/claude-3.5-sonnet",))
check("http429 bucket is disabled", not b.enabled)

err_str = "429 Too Many Requests"
is_429 = (
    "429" in err_str or
    "rate limit" in err_str or
    "too many requests" in err_str or
    ("quota exceeded" in err_str and b.enabled)
)
check("429 error flagged even when disabled", is_429)

# ---------------------------------------------------------------------------
# G. Singleton: same name returns same bucket
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("G. Singleton: same bucket name returns same instance")
print("=" * 80)

reset()
rate_limiter.RATE_LIMIT_MODE = "auto"
b1 = rate_limiter.get_bucket("singleton_test", models=("model:free",))
b2 = rate_limiter.get_bucket("singleton_test", models=("other:free",))
check("singleton returns same instance", b1 is b2)

# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("rate_limiter OK.")
