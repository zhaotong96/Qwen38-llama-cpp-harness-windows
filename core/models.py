"""
Data models and configuration constants for the agent workspace.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

# Base Paths
ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOTS = (ROOT / "skills",)
AGENT_DIR = ROOT / ".agent"
SCREEN_PATH = AGENT_DIR / "screen.jpg"
INBOX = AGENT_DIR / "inbox"
INBOX_PROCESSED = AGENT_DIR / "inbox_processed"
QUEUE_DIR = AGENT_DIR / "queue"
QUEUE_PROCESSED_DIR = AGENT_DIR / "queue_processed"
SUBAGENTS_ROOT = AGENT_DIR / "subagents"
INTERRUPT_PROCESSED_DIR = AGENT_DIR / "interrupt_processed"
CURRENT_SESSION_PATH = AGENT_DIR / "current_session"
PROVIDERS_PATH = AGENT_DIR / "providers.json"
USAGE_PATH = AGENT_DIR / "usage.jsonl"
AGENTS_PATH = AGENT_DIR / "AGENTS.md"
GOAL_PATH = AGENT_DIR / "GOAL.md"
SAFETY_LOG = ROOT / "logs" / "safety_guard.log"
WEB_DIR = ROOT / "web"

# Model & Server Settings (env overrides: GEMMA_BASE_URL / GEMMA_MODEL).
# Neither front end hardcodes a private endpoint; point these at your own
# OpenAI-compatible server (llama.cpp, etc.). Empty values fall back to the
# neutral defaults.
BASE_URL = os.environ.get("GEMMA_BASE_URL") or "http://127.0.0.1:8080/v1"
MODEL = os.environ.get("GEMMA_MODEL") or "local-model"
MAX_ROUNDS = 200
SHELL_TIMEOUT = 120
CHROME_CDP_PORT = 9222

# LLM Chat Settings
CHAT_EXTRA = {
    "top_k": 20,
    "min_p": 0.05,
    "repeat_penalty": 1.05,
    "cache_prompt": True,
    "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True},
}
# Context window probing: model facts come from the server, not constants.
FALLBACK_N_CTX = 32768
GEN_OVERHEAD_MARGIN = 2048


def probe_context_window(base_url: str, timeout: int = 3, fallback: int = FALLBACK_N_CTX) -> int:
    """Probe the model context window from the OpenAI-compatible /v1/models endpoint.

    Resolution order:
      1. data[i].meta.n_ctx          (llama.cpp server)
      2. data[i].context_length      (OpenRouter-style field)
      3. fallback (default FALLBACK_N_CTX) with a warning when the server is
         unreachable or the payload is unusable. Never raises.
    """
    url = base_url.rstrip("/") + "/models"
    log = logging.getLogger("agent")
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        models = payload.get("data") or payload.get("models") or []
        for m in models:
            meta = m.get("meta") or {}
            n_ctx = meta.get("n_ctx") or m.get("context_length")
            if isinstance(n_ctx, int) and n_ctx > 0:
                log.info(f"[ctx] probed context window: n_ctx={n_ctx}")
                return n_ctx
        log.warning("[ctx] /v1/models reachable but no context field found; using fallback")
    except Exception as e:
        log.warning(f"[ctx] context probe failed ({type(e).__name__}: {e}); using fallback")
    return fallback
