"""
Agent Configuration Dataclasses, Presets, and Multi-API Role-Tiered Provider Router.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from core import models
from core import persistence
from core.models import (
    BASE_URL,
    CHAT_EXTRA,
    CHROME_CDP_PORT,
    FALLBACK_N_CTX,
    MAX_ROUNDS,
    MODEL,
    ROOT,
    SAFETY_LOG,
    SCREEN_PATH,
    SHELL_TIMEOUT,
    SKILL_ROOTS,
    probe_context_window,
)

# Context management policy ratios (strategy choices, not model facts).
COMPACT_TRIGGER_RATIO = 0.5   # compact when payload reaches 50% of the window
COMPACT_TARGET_RATIO = 0.25   # fold history down to ~25% of the window

VALID_ROLES = ("main", "subagent", "compactor")

# Process-level probe cache: key -> resolved n_ctx. Prevents front-end refresh
# storms from re-probing the model server; cleared on providers hot reload.
_PROBED_WINDOW_CACHE: dict[str, int] = {}


def reset_probe_cache() -> None:
    """Drop every cached context window so the next load probes live."""
    _PROBED_WINDOW_CACHE.clear()

_ROLE_ENV_PREFIX = {
    "main": "MAIN",
    "subagent": "SUBAGENT",
    "compactor": "COMPACT",
}

DEFAULT_PROVIDERS_TEMPLATE: dict[str, Any] = {
    "active_profiles": {
        "main": "flagship_tier",
        "subagent": "economy_worker_tier",
        "compactor": "local_fast_tier",
    },
    "profiles": {
        "flagship_tier": {
            "base_url": "https://api.example-flagship.com/v1",
            "model": "claude-3-7-sonnet-or-gemma4-pro",
            "api_key_env": "FLAGSHIP_API_KEY",
            "fallback_n_ctx": 128000,
            "price_per_1m_input": 3.00,
            "price_per_1m_output": 15.00,
            "chat_extra": {},
        },
        "economy_worker_tier": {
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "gemma-4-9b-instruct",
            "api_key_env": "LOCAL_API_KEY",
            "fallback_n_ctx": 32768,
            "price_per_1m_input": 0.10,
            "price_per_1m_output": 0.30,
            "chat_extra": {"reasoning_format": "auto"},
        },
        "local_fast_tier": {
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "local-model",
            "api_key_env": "LOCAL_API_KEY",
            "fallback_n_ctx": 32768,
            "price_per_1m_input": 0.00,
            "price_per_1m_output": 0.00,
            "chat_extra": {},
        },
    },
}


def _providers_file_path(custom_path: Path | None = None) -> Path:
    if custom_path is not None:
        return Path(custom_path)
    return persistence.TASKS_ROOT.parent / "providers.json"


def _usage_file_path(custom_path: Path | None = None) -> Path:
    if custom_path is not None:
        return Path(custom_path)
    return persistence.TASKS_ROOT.parent / "usage.jsonl"


def derive_context_fields(n_ctx: int) -> dict[str, int]:
    """Derive generation/compaction budgets from a probed context window."""
    return {
        "n_ctx": n_ctx,
        "max_gen_tokens": n_ctx // 2,
        "compact_trigger": int(n_ctx * COMPACT_TRIGGER_RATIO),
        "compact_target": int(n_ctx * COMPACT_TARGET_RATIO),
    }


def load_providers_file(providers_path: Path | None = None) -> dict[str, Any] | None:
    """Load .agent/providers.json if it exists and is valid JSON."""
    path = _providers_file_path(providers_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def save_providers_file(data: dict[str, Any], providers_path: Path | None = None) -> Path:
    """Atomically write .agent/providers.json after validating structure.

    Saving is the hot-reload trigger: the probe cache is dropped so the next
    configuration load re-probes every endpoint against its new provider.
    """
    if not isinstance(data, dict):
        raise ValueError("providers configuration must be a JSON object")
    path = _providers_file_path(providers_path)
    written = persistence.atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )
    reset_probe_cache()
    return written


def _slot_for_role(role: str) -> int:
    """Dual-slot affinity: main owns slot 0; subagent and compactor share slot 1."""
    return 0 if role == "main" else 1


def _with_slot_affinity(chat_extra: dict[str, Any], role: str) -> dict[str, Any]:
    merged = dict(chat_extra)
    merged.setdefault("id_slot", _slot_for_role(role))
    return merged


def _resolve_role_endpoint(
    role: str = "main",
    providers_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve role endpoint parameters using 3-tier priority:
    1. .agent/providers.json active profile for role
    2. Role-specific environment variables (MAIN_*, SUBAGENT_*, COMPACT_*)
    3. Global environment variables GEMMA_BASE_URL / GEMMA_MODEL (models.BASE_URL / models.MODEL)
    """
    role = role if role in VALID_ROLES else "main"
    default_base_url = os.environ.get("GEMMA_BASE_URL") or models.BASE_URL
    default_model = os.environ.get("GEMMA_MODEL") or models.MODEL
    default_api_key = os.environ.get("GEMMA_API_KEY") or "local"

    # Tier 1: providers.json
    if isinstance(providers_data, dict):
        active_profiles = providers_data.get("active_profiles") or {}
        profiles = providers_data.get("profiles") or {}
        profile_name = active_profiles.get(role)
        profile = profiles.get(profile_name) if profile_name else None
        if isinstance(profile, dict):
            api_key_env = str(profile.get("api_key_env") or "")
            api_key = (os.environ.get(api_key_env) if api_key_env else None) or default_api_key
            chat_extra = profile.get("chat_extra")
            resolved_extra = dict(chat_extra) if isinstance(chat_extra, dict) else dict(CHAT_EXTRA)
            return {
                "role": role,
                "profile_name": profile_name,
                "base_url": str(profile.get("base_url") or default_base_url),
                "model": str(profile.get("model") or default_model),
                "api_key": api_key,
                "api_key_env": api_key_env,
                "fallback_n_ctx": int(profile.get("fallback_n_ctx") or FALLBACK_N_CTX),
                "price_per_1m_input": float(profile.get("price_per_1m_input") or 0.0),
                "price_per_1m_output": float(profile.get("price_per_1m_output") or 0.0),
                "chat_extra": _with_slot_affinity(resolved_extra, role),
            }

    # Tier 2: Role-specific environment variables
    prefix = _ROLE_ENV_PREFIX[role]
    env_url = (os.environ.get(f"{prefix}_BASE_URL") or "").strip()
    env_model = (os.environ.get(f"{prefix}_MODEL") or "").strip()
    env_key = (os.environ.get(f"{prefix}_API_KEY") or "").strip()
    if env_url or env_model or env_key:
        return {
            "role": role,
            "profile_name": f"env_{role}",
            "base_url": env_url or default_base_url,
            "model": env_model or default_model,
            "api_key": env_key or default_api_key,
            "api_key_env": f"{prefix}_API_KEY",
            "fallback_n_ctx": FALLBACK_N_CTX,
            "price_per_1m_input": 0.0,
            "price_per_1m_output": 0.0,
            "chat_extra": _with_slot_affinity(dict(CHAT_EXTRA), role),
        }

    # Tier 3: Global GEMMA_* baseline
    return {
        "role": role,
        "profile_name": "default",
        "base_url": default_base_url,
        "model": default_model,
        "api_key": default_api_key,
        "api_key_env": "GEMMA_API_KEY",
        "fallback_n_ctx": FALLBACK_N_CTX,
        "price_per_1m_input": 0.0,
        "price_per_1m_output": 0.0,
        "chat_extra": _with_slot_affinity(dict(CHAT_EXTRA), role),
    }


def _probe_window(base_url: str, model: str = "", fallback: int = FALLBACK_N_CTX) -> int:
    """Resolve the context window through the process-level probe cache.

    Cache hit -> no network traffic. Cache miss -> call probe_context_window
    while staying compatible with 1-arg test mocks.
    """
    cache_key = f"{base_url.rstrip('/')}|{model or 'default'}"
    cached = _PROBED_WINDOW_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if fallback == FALLBACK_N_CTX:
        n_ctx = probe_context_window(base_url)
    else:
        try:
            n_ctx = probe_context_window(base_url, fallback=fallback)
        except TypeError:
            n_ctx = probe_context_window(base_url)
    _PROBED_WINDOW_CACHE[cache_key] = n_ctx
    return n_ctx


def load_config(role: str = "main", providers_path: Path | None = None) -> AgentConfig:
    """Build the runtime configuration shared by the CLI and the HTTP API.

    Probes the live model context window once at startup and derives the
    generation/compaction budgets from it. Supports role-tiered routing via
    .agent/providers.json and role-specific environment variables while falling
    back seamlessly to GEMMA_BASE_URL and GEMMA_MODEL.
    """
    providers_data = load_providers_file(providers_path)
    resolved = _resolve_role_endpoint(role=role, providers_data=providers_data)
    n_ctx = _probe_window(
        resolved["base_url"], model=resolved["model"], fallback=resolved["fallback_n_ctx"]
    )
    fields = derive_context_fields(n_ctx)
    return AgentConfig(
        base_url=resolved["base_url"],
        model=resolved["model"],
        api_key=resolved["api_key"],
        role=resolved["role"],
        profile_name=resolved["profile_name"],
        price_per_1m_input=resolved["price_per_1m_input"],
        price_per_1m_output=resolved["price_per_1m_output"],
        chat_extra=resolved["chat_extra"],
        **fields,
    )


def load_all_role_configs(providers_path: Path | None = None) -> dict[str, AgentConfig]:
    """Load AgentConfig instances for all three roles (main, subagent, compactor).

    Repeated calls (e.g. front-end refreshes hitting GET /config/providers)
    reuse the process-level probe cache and send no extra network traffic.
    """
    providers_data = load_providers_file(providers_path)
    configs: dict[str, AgentConfig] = {}
    for role in VALID_ROLES:
        resolved = _resolve_role_endpoint(role=role, providers_data=providers_data)
        n_ctx = _probe_window(
            resolved["base_url"], model=resolved["model"], fallback=resolved["fallback_n_ctx"]
        )
        fields = derive_context_fields(n_ctx)
        configs[role] = AgentConfig(
            base_url=resolved["base_url"],
            model=resolved["model"],
            api_key=resolved["api_key"],
            role=resolved["role"],
            profile_name=resolved["profile_name"],
            price_per_1m_input=resolved["price_per_1m_input"],
            price_per_1m_output=resolved["price_per_1m_output"],
            chat_extra=resolved["chat_extra"],
            **fields,
        )
    return configs


def record_usage(
    role: str,
    model: str,
    base_url: str,
    input_tokens: int,
    output_tokens: int,
    price_per_1m_input: float = 0.0,
    price_per_1m_output: float = 0.0,
    task_id: str | None = None,
    usage_path: Path | None = None,
) -> dict[str, Any]:
    """Append a token usage and cost audit entry to .agent/usage.jsonl."""
    path = _usage_file_path(usage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cost_usd = (
        (max(0, input_tokens) / 1_000_000.0) * float(price_per_1m_input)
        + (max(0, output_tokens) / 1_000_000.0) * float(price_per_1m_output)
    )
    entry = {
        "timestamp": datetime.now().isoformat(timespec="microseconds"),
        "task_id": task_id,
        "role": role,
        "model": model,
        "base_url": base_url,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": round(cost_usd, 8),
    }
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry


def read_usage_summary(usage_path: Path | None = None) -> dict[str, Any]:
    """Read .agent/usage.jsonl and aggregate token and cost totals by role."""
    path = _usage_file_path(usage_path)
    entries: list[dict[str, Any]] = []
    by_role: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                        entries.append(item)
                        role = str(item.get("role", "main"))
                        bucket = by_role.setdefault(
                            role,
                            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                        )
                        bucket["calls"] += 1
                        bucket["input_tokens"] += int(item.get("input_tokens", 0))
                        bucket["output_tokens"] += int(item.get("output_tokens", 0))
                        bucket["cost_usd"] = round(
                            bucket["cost_usd"] + float(item.get("cost_usd", 0.0)), 8
                        )
                        total_cost = round(total_cost + float(item.get("cost_usd", 0.0)), 8)
                    except Exception:
                        continue
        except OSError:
            pass
    return {
        "total_calls": len(entries),
        "total_cost_usd": total_cost,
        "by_role": by_role,
        "recent": entries[-50:],
    }


@dataclass
class AgentConfig:
    """Runtime Configuration for the Agent."""

    base_url: str = BASE_URL
    model: str = MODEL
    api_key: str = "local"
    role: str = "main"
    profile_name: str = "default"
    price_per_1m_input: float = 0.0
    price_per_1m_output: float = 0.0
    max_rounds: int = MAX_ROUNDS
    shell_timeout: int = SHELL_TIMEOUT
    # Context fields default to the conservative fallback window; main.py passes
    # probed values via derive_context_fields() at startup.
    n_ctx: int = FALLBACK_N_CTX
    max_gen_tokens: int = FALLBACK_N_CTX // 2
    compact_trigger: int = int(FALLBACK_N_CTX * COMPACT_TRIGGER_RATIO)
    compact_target: int = int(FALLBACK_N_CTX * COMPACT_TARGET_RATIO)
    chat_extra: dict[str, Any] = field(default_factory=lambda: dict(CHAT_EXTRA))
    chrome_cdp_port: int = CHROME_CDP_PORT
    screen_path: Path = SCREEN_PATH
    safety_log: Path = SAFETY_LOG
    log_level: str = "INFO"


class Presets:
    """Hardcoded safety rules. The system prompt is not preset: it is read from .agent/AGENTS.md."""

    DANGEROUS_SHELL_REGEX: str = (
        r"(?i)\b(format-volume|clear-disk|initialize-disk|diskpart|shutdown|"
        r"stop-computer|restart-computer|remove-item\b[^\r\n]*\s-(?:recurse|force)|"
        r"git\s+(?:reset\s+--hard|clean\s+-)|reg\s+delete|invoke-expression|iex)\b|"
        r"\brm\s+-[a-z]*r[a-z]*f|\|\s*(?:sh|bash|pwsh|powershell)\b"
    )
