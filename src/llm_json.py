"""Shared hardened JSON parser for LLM responses.

Before this module, five sites each parsed an LLM response with their own
copy of the same "fence-strip + fix single quotes + trailing-comma + sliding
raw_decode" logic, and three of them used an O(n^2) char-by-char ``idx += 1``
scan that re-tried parsing from every offset. This module is the single,
O(L)-amortised parser. Stages call ``parse_llm_json`` (or keep their
existing ``_parse_json_from_response`` wrapper and delegate here).
"""

from __future__ import annotations

import json
import re


def _extract_json_object(text: str) -> str:
    """Return the substring from the first '{' to the last '}' (inclusive)."""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and 0 < end:
        return text[start : end + 1]
    return text


def _fix_common_malformations(text: str) -> str:
    # Single-quoted keys/values -> double quotes.
    text = text.replace("'", '"')
    # Trailing commas before } or ].
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def parse_llm_json(content: str) -> dict:
    """Parse JSON from an LLM response, recovering common malformations.

    Handles prose-wrapped JSON, ```json fences, single-quoted keys, and
    trailing commas. Uses ``JSONDecoder.raw_decode`` jumped to each '{' so the
    recovery scan is O(L) amortised rather than O(L^2).

    Raises ValueError if no JSON object can be recovered.
    """
    if content is None:
        raise ValueError("Empty LLM response")

    text = content.strip()

    # Strip markdown code fences.
    if text.startswith("```json"):
        text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
    elif text.startswith("```"):
        text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()

    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM returned {type(parsed).__name__}, expected JSON object")
        return parsed
    except json.JSONDecodeError:
        pass

    text = _extract_json_object(text)
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM returned {type(parsed).__name__}, expected JSON object")
        return parsed
    except json.JSONDecodeError:
        pass

    text = _fix_common_malformations(text)
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM returned {type(parsed).__name__}, expected JSON object")
        return parsed
    except json.JSONDecodeError:
        pass

    # Last resort: scan for the first '{' that yields a valid dict, jumping
    # to each '{' rather than advancing one char at a time.
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx >= 0:
        try:
            obj, _end = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        next_idx = text.find("{", idx + 1)
        if next_idx == idx:  # safety against infinite loop
            break
        idx = next_idx

    raise ValueError(f"Could not parse JSON from LLM response (last 400 chars): ...{content[-400:]}")
