"""Skill: Parent-side adversarial review gate for SubAgent worktree deliveries.

After a SubAgent finishes inside its isolated Git worktree, the parent agent
must audit the host-generated artifact card (diff, hashes, test receipts,
hazard flags) against the three falsification questions and render a final
verdict: ``merge`` (atomic fast-forward into the main workspace) or ``revert``
(discard the sandbox and pin a ``[Branch Rewind Lesson]`` into the fact
ledger's section 4).
"""
from __future__ import annotations

import json

from core.subagent_gate import finalize_subagent_worktree

DESCRIPTION = (
    "Review an isolated SubAgent worktree artifact delivery card and execute "
    "either a fast-forward merge into the main workspace or a complete revert/cleanup."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "subagent_id": {
            "type": "string",
            "description": "Unique identifier of the completed SubAgent.",
        },
        "decision": {
            "type": "string",
            "enum": ["merge", "revert"],
            "description": "Review decision: 'merge' (fast-forward) or 'revert' (discard).",
        },
        "reason": {
            "type": "string",
            "description": "Architectural reasoning and verification evidence justifying the decision.",
        },
    },
    "required": ["subagent_id", "decision", "reason"],
}


def run(args: dict, runtime: dict | None = None) -> str:
    args = args or {}
    subagent_id = str(args.get("subagent_id") or "").strip()
    decision = str(args.get("decision") or "").strip().lower()
    reason = str(args.get("reason") or "").strip()

    if not subagent_id:
        return "error: subagent_id is required"
    if decision not in ("merge", "revert"):
        return "error: decision must be 'merge' or 'revert'"
    if not reason:
        return "error: a non-empty 'reason' with verification evidence is required"

    result = finalize_subagent_worktree(subagent_id, decision, reason)
    if not result.get("ok"):
        return "error: " + str(result.get("error", "review finalization failed"))

    receipt: dict[str, object] = dict(result)
    if decision == "revert":
        # The lesson line is pinned verbatim into ledger section 4 by the
        # agent's immutable-redline pipeline when the receipt enters context.
        receipt["receipt"] = result.get("lesson", "")
    return json.dumps(receipt, ensure_ascii=False)
