"""
Shared rate limiter for OpenRouter free tier.

Free tier limits (as of Oct 2026):
- 20 requests per minute (RPM)
- 50 requests per day (1000/day after $10 credit)
- 429 Too Many Requests when exceeded

This module provides a TokenBucket with:
- Configurable RPM and daily limits via env vars
- 429 error handling with exponential backoff + jitter
- Daily request counter with persistence
- Thread-safe operations
"""

from __future__ import annotations

import os
import time
import json
import threading
from pathlib import Path
from datetime import datetime, date
from typing import Optional
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# Configuration from environment (with free-tier defaults)
FREE_TIER_RPM = int(os.getenv("FREE_TIER_RPM", "20"))
FREE_TIER_DAILY_LIMIT = int(os.getenv("FREE_TIER_DAILY_LIMIT", "50"))
RATE_LIMIT_STATE_DIR = Path(os.getenv("RATE_LIMIT_STATE_DIR", str(Path(__file__).resolve().parent.parent / "data" / "_rate_limit")))

RATE_LIMIT_STATE_DIR.mkdir(parents=True, exist_ok=True)
DAILY_COUNT_FILE = RATE_LIMIT_STATE_DIR / "daily_count.json"

# Global lock for daily counter
_daily_lock = threading.Lock()


class RateLimitExceeded(Exception):
    """Raised when daily request limit is exceeded."""
    def __init__(self, message: str, reset_time: Optional[float] = None):
        super().__init__(message)
        self.reset_time = reset_time


class TokenBucket:
    """
    Token bucket rate limiter with 429 handling and daily limit tracking.
    
    Usage:
        bucket = TokenBucket(rpm=20, daily_limit=50)
        
        # Before each API call:
        bucket.take()
        
        # After a 429 error:
        bucket.handle_429(retry_after_seconds=None)
    """
    
    def __init__(
        self,
        rpm: int = FREE_TIER_RPM,
        daily_limit: int = FREE_TIER_DAILY_LIMIT,
        name: str = "default",
        logger=None
    ):
        self.rpm = rpm
        self.daily_limit = daily_limit
        self.name = name
        self.logger = logger
        
        # Token bucket state
        self.rate = rpm / 60.0  # tokens per second
        self.tokens = float(rpm)
        self.last = time.time()
        self._lock = threading.Lock()
        
        # 429 backoff state
        self.backoff_until = 0.0
        self.consecutive_429s = 0
        
        # Load daily count
        self._load_daily_count()
    
    def _load_daily_count(self) -> None:
        """Load today's request count from disk."""
        with _daily_lock:
            today = date.today().isoformat()
            if DAILY_COUNT_FILE.exists():
                try:
                    data = json.loads(DAILY_COUNT_FILE.read_text(encoding="utf-8"))
                    if data.get("date") == today:
                        self.daily_count = data.get("count", 0)
                    else:
                        self.daily_count = 0
                except (json.JSONDecodeError, OSError):
                    self.daily_count = 0
            else:
                self.daily_count = 0
    
    def _save_daily_count(self) -> None:
        """Save today's request count to disk."""
        with _daily_lock:
            today = date.today().isoformat()
            data = {"date": today, "count": self.daily_count}
            tmp = DAILY_COUNT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(DAILY_COUNT_FILE)
    
    def increment_daily(self) -> None:
        """Increment daily counter and save."""
        self.daily_count += 1
        self._save_daily_count()
    
    def check_daily_limit(self) -> None:
        """Check if daily limit exceeded, raise if so."""
        if self.daily_count >= self.daily_limit:
            # Calculate seconds until midnight
            now = datetime.now()
            midnight = datetime.combine(now.date() + __import__('datetime').timedelta(days=1), __import__('datetime').time.min)
            reset_seconds = (midnight - now).total_seconds()
            raise RateLimitExceeded(
                f"Daily request limit ({self.daily_limit}) exceeded for {self.name}. "
                f"Resets at midnight ({reset_seconds:.0f}s).",
                reset_time=time.time() + reset_seconds
            )
    
    def take(self) -> float:
        """
        Take a token, blocking until available.
        Returns wait time in seconds (0 if immediate).
        """
        # Check daily limit first
        self.check_daily_limit()
        
        # Check 429 backoff
        if self.backoff_until > time.time():
            wait = self.backoff_until - time.time()
            if self.logger:
                self.logger.warning(f"[{self.name}] Backing off for {wait:.1f}s due to 429")
            time.sleep(wait)
        
        with self._lock:
            now = time.time()
            elapsed = now - self.last
            
            # Refill tokens based on elapsed time
            if elapsed > 0:
                self.tokens = min(self.rate * elapsed + self.tokens, self.rpm)
            
            if self.tokens >= 1:
                self.tokens -= 1
                self.last = now
                self.increment_daily()
                return 0.0
            
            # Need to wait for token
            wait = (1 - self.tokens) / self.rate
            self.tokens = 0
            self.last = now + wait
            self.increment_daily()
            
            if self.logger:
                self.logger.debug(f"[{self.name}] Rate limit wait: {wait:.2f}s")
            time.sleep(wait)
            return wait
    
    def handle_429(self, retry_after: Optional[float] = None) -> float:
        """
        Handle a 429 response with exponential backoff.
        
        Args:
            retry_after: Seconds from Retry-After header, if present
            
        Returns:
            Total wait time in seconds
        """
        self.consecutive_429s += 1
        
        # Base backoff: 2^N seconds with jitter
        base_backoff = min(2 ** self.consecutive_429s, 60)  # Cap at 60s
        
        # Use Retry-After header if provided and longer
        if retry_after and retry_after > base_backoff:
            backoff = retry_after
        else:
            # Add jitter (±25%)
            import random
            jitter = base_backoff * 0.25 * (2 * random.random() - 1)
            backoff = base_backoff + jitter
        
        self.backoff_until = time.time() + backoff
        
        if self.logger:
            self.logger.warning(
                f"[{self.name}] 429 received (count: {self.consecutive_429s}), "
                f"backing off for {backoff:.1f}s"
            )
        
        time.sleep(backoff)
        return backoff
    
    def reset_429_state(self) -> None:
        """Reset 429 backoff state after successful request."""
        if self.consecutive_429s > 0:
            if self.logger:
                self.logger.info(f"[{self.name}] 429 state reset after {self.consecutive_429s} consecutive 429s")
            self.consecutive_429s = 0
            self.backoff_until = 0.0
    
    def get_status(self) -> dict:
        """Get current rate limiter status."""
        return {
            "name": self.name,
            "rpm": self.rpm,
            "daily_limit": self.daily_limit,
            "daily_count": self.daily_count,
            "daily_remaining": max(0, self.daily_limit - self.daily_count),
            "tokens_available": self.tokens,
            "consecutive_429s": self.consecutive_429s,
            "backoff_active": self.backoff_until > time.time(),
        }


# Global bucket instances per stage (singleton pattern)
_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()


def get_bucket(name: str, rpm: int = FREE_TIER_RPM, daily_limit: int = FREE_TIER_DAILY_LIMIT, logger=None) -> TokenBucket:
    """Get or create a named token bucket (singleton per name)."""
    with _buckets_lock:
        if name not in _buckets:
            _buckets[name] = TokenBucket(rpm=rpm, daily_limit=daily_limit, name=name, logger=logger)
        return _buckets[name]


def reset_all_buckets() -> None:
    """Reset all bucket states (for testing)."""
    global _buckets
    with _buckets_lock:
        _buckets.clear()
    if DAILY_COUNT_FILE.exists():
        DAILY_COUNT_FILE.unlink()


# Convenience function for simple rate limiting
def rate_limited_call(
    bucket_name: str,
    call_fn,
    *args,
    max_retries: int = 5,
    rpm: int = FREE_TIER_RPM,
    daily_limit: int = FREE_TIER_DAILY_LIMIT,
    logger=None,
    **kwargs
):
    """
    Execute a function with rate limiting and 429 retry logic.
    
    Args:
        bucket_name: Name for the token bucket
        call_fn: Function to call (should make the API request)
        max_retries: Max retries on 429 errors
        rpm: Requests per minute limit
        daily_limit: Daily request limit
        logger: Optional logger
        
    Returns:
        Result of call_fn
        
    Raises:
        RateLimitExceeded: If daily limit exceeded
        Exception: Last exception if all retries exhausted
    """
    bucket = get_bucket(bucket_name, rpm=rpm, daily_limit=daily_limit, logger=logger)
    last_err = None
    
    for attempt in range(max_retries + 1):
        try:
            bucket.take()
            result = call_fn(*args, **kwargs)
            bucket.reset_429_state()
            return result
            
        except RateLimitExceeded:
            # Daily limit - don't retry
            raise
            
        except Exception as err:
            last_err = err
            err_str = str(err).lower()
            
            # Check for 429 / rate limit errors
            is_429 = (
                "429" in err_str or
                "rate limit" in err_str or
                "too many requests" in err_str or
                "quota exceeded" in err_str
            )
            
            if is_429 and attempt < max_retries:
                # Extract retry-after from error if possible
                retry_after = None
                if "retry-after" in err_str:
                    import re
                    match = re.search(r"retry.after[:\s=]+(\d+)", err_str)
                    if match:
                        retry_after = float(match.group(1))
                
                bucket.handle_429(retry_after)
                continue
            
            # Non-429 error or max retries reached
            if logger:
                logger.error(f"[{bucket_name}] Call failed after {attempt + 1} attempts: {err}")
            raise
    
    raise last_err