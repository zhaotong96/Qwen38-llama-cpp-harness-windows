"""Skill: Spawn an isolated SubAgent sandbox to prevent main context rot."""
from __future__ import annotations

from core.subagent_runner import run_subagent

DESCRIPTION = (
    "Spawn an isolated SubAgent with a clean context window to execute a "
    "self-contained exploratory, search, or multi-step operational sub-task. "
    "Prevents main context pollution. Only one SubAgent may be active at a time. "
    "Returns only the distilled final report and artifact paths."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "description": "Clear, self-contained task description and acceptance criteria for the SubAgent.",
        },
        "context_hints": {
            "type": "string",
            "description": "Essential background facts or file paths needed by the SubAgent.",
        },
        "max_rounds": {
            "type": "integer",
            "description": "Maximum ReAct turns allowed for this SubAgent (default 15, max 40).",
        },
        "allowed_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional whitelist of skill names. Note: 'subagent' is always excluded to prevent recursive spawning.",
        },
    },
    "required": ["objective"],
}


def run(args: dict, runtime: dict = None) -> str:
    return run_subagent(args, runtime)
