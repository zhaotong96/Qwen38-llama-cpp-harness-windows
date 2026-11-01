"""Skill: Crystallize a successful workflow trajectory into a deterministic macro.

Distills the winning DAG branch of a task (default: the current active task)
into an AST-pure, zero-token deterministic Python skill via
``core.macro_compiler.compile_and_deploy_macro``. The new macro is promoted
through the standard skill sandbox (AST purity, schema validation, audit-hook
dry run) and becomes immediately callable by name.
"""
from __future__ import annotations

from core.macro_compiler import compile_and_deploy_macro

DESCRIPTION = (
    "Compile and crystallize a successful multi-step desktop workflow from the current "
    "or specified task DAG trajectory into an AST-pure, zero-token deterministic Python skill."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "macro_name": {
            "type": "string",
            "description": "Identifier for the new macro skill, e.g. 'macro_export_report'. Must start with 'macro_'.",
        },
        "description": {
            "type": "string",
            "description": "Concise natural language description of what this workflow achieves.",
        },
        "task_id": {
            "type": "string",
            "description": "Source task_id containing the successful trajectory (defaults to current active task).",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether to overwrite an existing macro skill of the same name (default false).",
        },
    },
    "required": ["macro_name", "description"],
}


def run(args: dict, runtime: dict | None = None) -> str:
    args = args or {}
    macro_name = str(args.get("macro_name") or "").strip()
    description = str(args.get("description") or "").strip()
    task_id = str(args.get("task_id") or (runtime or {}).get("task_id") or "").strip()
    overwrite = bool(args.get("overwrite"))

    if not macro_name.startswith("macro_"):
        return "error: macro_name must start with 'macro_'"
    if not description:
        return "error: a non-empty description of the workflow is required"
    if not task_id:
        from core import persistence

        task_id = str(persistence.get_current_session() or "").strip()
    if not task_id:
        return "error: no source task_id available (pass task_id explicitly)"

    return compile_and_deploy_macro(
        task_id,
        macro_name,
        description,
        overwrite=overwrite,
    )
