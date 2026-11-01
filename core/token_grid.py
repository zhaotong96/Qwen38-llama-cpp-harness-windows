"""DeltaNet 1024-token boundary grid alignment and hidden-state telemetry.

The remote inference host runs ``llama-server`` with
``--checkpoint-min-step 1024 --ctx-checkpoints 64 --cache-ram 16384
--no-cache-idle-slots``: every 1024 tokens the hybrid-recurrent model's
DeltaNet loop state is frozen into a RAM checkpoint. A static skeleton prefix
(System Prompt + fact ledger) whose length sits in the 64-token shadow just
before a grid line lets the server materialize one extra durable snapshot at
compaction/branch time, so rewind and epoch boundaries snap to physical
checkpoints instead of landing mid-block.

This module is pure standard library (Gate 3) and degrades smoothly to the
weighted local estimator when the ``/tokenize`` endpoint is unreachable
(Gate 4). Padding uses zero-semantic Markdown horizontal rules (``\\n---``)
only — HTML/XML comment padding such as ``<!-- pad -->`` is forbidden
(Gate 1) — and never appends more than ``max_pad_tokens`` (Gate 2).
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

GRID_STEP = 1024
MAX_PAD_TOKENS = 64
TOKENIZE_TIMEOUT = 0.3

# Structural divider shim: in the Qwen vocabulary each "\n---" line encodes as
# ~2 format-only tokens and carries no semantic bias. Never use comment pads.
_PAD_LINE = "---"

_TOKEN_CACHE: dict[str, int] = {}


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF      # CJK Unified Ideographs Extension A
        or 0x4E00 <= code <= 0x9FFF   # CJK Unified Ideographs
        or 0xF900 <= code <= 0xFAFF   # CJK Compatibility Ideographs
        or 0x3000 <= code <= 0x303F   # CJK punctuation
        or 0xFF00 <= code <= 0xFFEF   # Full-width forms
    )


def estimate_text_tokens(text: str) -> int:
    """Weighted character estimate: CJK 0.75 token/char, other text 0.25 token/char.

    Canonical offline fallback shared by the context manager, the DAG
    persistence layer, and the grid alignment probe.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    return int(cjk * 0.75 + (len(text) - cjk) * 0.25)


def clear_tokenize_cache() -> None:
    """Drop the SHA-1 memoization cache (tests and endpoint switches)."""
    _TOKEN_CACHE.clear()


def _tokenize_via_endpoint(text: str, base_url: str, timeout: float) -> int | None:
    """POST the text to llama-server's /tokenize endpoint; None on any failure."""
    url = base_url.rstrip("/") + "/tokenize"
    payload = json.dumps({"content": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None
    tokens = body.get("tokens")
    if isinstance(tokens, list):
        return len(tokens)
    return None


def exact_or_estimated_tokens(
    text: str,
    base_url: str | None = None,
    timeout: float = TOKENIZE_TIMEOUT,
) -> int:
    """Measure token count via POST /tokenize with SHA-1 memoization.

    Falls back silently to the weighted estimator when the endpoint is
    unreachable, times out, or returns an unusable payload (Gate 4: the agent
    main loop must never observe an exception from this function).
    """
    if not text:
        return 0
    key = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
    cached = _TOKEN_CACHE.get(key)
    if cached is not None:
        return cached

    count: int | None = None
    if base_url:
        count = _tokenize_via_endpoint(text, base_url, timeout)
    if count is None:
        count = estimate_text_tokens(text)
    _TOKEN_CACHE[key] = count
    return count


def _pad_suffix(pad_lines: int) -> str:
    """Build the zero-semantic structural divider shim for ``pad_lines`` rows."""
    if pad_lines <= 0:
        return ""
    return "\n" + "\n".join(_PAD_LINE for _ in range(pad_lines)) + "\n"


def align_skeleton_block_to_grid(
    block_text: str,
    prior_tokens: int = 0,
    grid_step: int = GRID_STEP,
    max_pad_tokens: int = MAX_PAD_TOKENS,
    base_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Align a long-lived skeleton block onto the next checkpoint grid line.

    Only structural Markdown dividers are appended, and only when the block
    ends within ``max_pad_tokens`` of the next ``grid_step`` boundary. The
    result is a deterministic pure function of the inputs, so the same
    skeleton always produces the same prefix (idempotent: a second call finds
    the remainder just past the line and appends nothing).

    Returns ``(padded_text, info)`` where ``info`` reports the measured token
    positions, the applied pad, and whether a grid line was crossed.
    """
    before = prior_tokens + exact_or_estimated_tokens(block_text, base_url)
    remainder = before % grid_step
    gap_to_next = (grid_step - remainder) % grid_step
    info: dict[str, Any] = {
        "before_tokens": before,
        "gap_to_next": gap_to_next,
        "pad_lines": 0,
        "pad_tokens": 0,
        "after_tokens": before,
        "aligned": remainder == 0,
    }
    if remainder == 0:
        # Exactly on the boundary: the snapshot at grid_step*K already covers
        # the whole prefix; nothing to do.
        return block_text, info
    if gap_to_next > max_pad_tokens:
        info["reason"] = "gap_exceeds_ceiling"
        return block_text, info

    formula_lines = max(1, (gap_to_next + 1) // 2)
    lines_cap = max(1, max_pad_tokens)
    chosen_lines = 0
    chosen_text = block_text
    chosen_after = before

    if base_url:
        # Exact path: the server tokenizer usually lands the formula directly;
        # probe a small bounded neighbourhood before giving up.
        candidates = [formula_lines]
        candidates += [d for d in (formula_lines - 1, formula_lines + 1, formula_lines + 2) if d >= 1]
        for lines in candidates:
            if lines > lines_cap:
                continue
            candidate = block_text + _pad_suffix(lines)
            after = prior_tokens + exact_or_estimated_tokens(candidate, base_url)
            if after - before > max_pad_tokens:
                continue
            if after // grid_step > before // grid_step:
                chosen_lines, chosen_text, chosen_after = lines, candidate, after
                break
    else:
        # Offline path: scan all line counts and pick the smallest that
        # crosses a grid line within the pad ceiling.
        for lines in range(1, lines_cap + 1):
            candidate = block_text + _pad_suffix(lines)
            after = prior_tokens + estimate_text_tokens(candidate)
            if after - before > max_pad_tokens:
                break
            if after // grid_step > before // grid_step:
                chosen_lines, chosen_text, chosen_after = lines, candidate, after
                break

    if chosen_lines <= 0:
        # Nothing crossed within the ceiling: keep the formula pad as a
        # best-effort shim and report the block as not aligned.
        chosen_lines = formula_lines
        chosen_text = block_text + _pad_suffix(formula_lines)
        chosen_after = prior_tokens + estimate_text_tokens(chosen_text)

    info.update({
        "pad_lines": chosen_lines,
        "pad_tokens": chosen_after - before,
        "after_tokens": chosen_after,
        "aligned": chosen_after // grid_step > before // grid_step,
    })
    return chosen_text, info


def verify_deltanet_checkpoint_telemetry(
    timings: dict[str, Any] | None,
    expected_prefix_tokens: int,
    grid_step: int = GRID_STEP,
) -> dict[str, Any]:
    """Audit llama-server cache telemetry against the immutable prefix length.

    States:
    - ``RESONANT``: ``cache_n`` covers the aligned prefix (normal case).
    - ``COLD_RESET_DETECTED``: prefix >= one grid line yet ``cache_n == 0``
      (slot evicted / cache-ram pressure forced a cold prefill).
    - ``CHECKPOINT_GAP_DRIFT``: ``cache_n`` is below the legally reusable
      floor ``((L // grid_step) - 1) * grid_step`` (checkpoint restore skipped).
    - ``UNKNOWN``: no telemetry available (non-llama.cpp endpoint or the
      caller could not extract ``timings``).
    """
    prefix = max(0, int(expected_prefix_tokens or 0))
    report: dict[str, Any] = {
        "status": "UNKNOWN",
        "cache_n": None,
        "prompt_n": None,
        "expected_prefix_tokens": prefix,
        "min_expected_cache": 0,
        "grid_step": grid_step,
    }
    if not isinstance(timings, dict) or "cache_n" not in timings:
        return report

    try:
        cache_n = int(timings.get("cache_n") or 0)
        prompt_n = int(timings.get("prompt_n") or 0)
    except (TypeError, ValueError):
        return report

    min_cache = ((prefix // grid_step) - 1) * grid_step if prefix // grid_step >= 1 else 0
    min_cache = max(0, min_cache)

    if prefix >= grid_step and cache_n == 0:
        status = "COLD_RESET_DETECTED"
    elif cache_n < min_cache:
        status = "CHECKPOINT_GAP_DRIFT"
    else:
        status = "RESONANT"

    report.update({
        "status": status,
        "cache_n": cache_n,
        "prompt_n": prompt_n,
        "min_expected_cache": min_cache,
    })
    return report


def fourgram_repeat_rate(text: str) -> float:
    """Sliding 4-gram repetition rate over whitespace-delimited tokens."""
    tokens = (text or "").split()
    if len(tokens) < 4:
        return 0.0
    grams = [tuple(tokens[i:i + 4]) for i in range(len(tokens) - 3)]
    return 1.0 - len(set(grams)) / len(grams)


def should_reseed_generation(text: str, threshold: float = 0.35) -> bool:
    """True when generated text shows severe repetition loop contamination."""
    return fourgram_repeat_rate(text) > threshold
