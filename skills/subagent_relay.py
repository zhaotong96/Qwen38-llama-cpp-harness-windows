"""Skill: Three-role Scout → Builder → Verifier relay pipeline (Phase 12C).

Runs the分权制衡 relay: a strictly read-only Scout fixes a 1024-grid-aligned
context anchor, a Builder synthesizes the change inside an isolated Git
worktree, and an independent Verifier runs the targeted tests and static
hazard scans. The host deterministically issues a tamper-proof
``verification_ticket.json``; the parent agent merges or reverts with one
final ``finalize`` call based only on that ticket.
"""
from __future__ import annotations

import json

from core import subagent_gate
from core.subagent_runner import run_subagent

DESCRIPTION = (
    "Run the three-role Scout-Builder-Verifier relay pipeline: read-only code "
    "investigation with a 1024-grid-aligned anchor, isolated worktree "
    "construction, and independent test/hazard verification with a "
    "tamper-proof verification ticket. Supports action='finalize' to merge or "
    "revert the delivery based on the ticket verdict."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "The complete engineering objective for the relay pipeline.",
        },
        "target_modules": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Modules or paths the scout must investigate first.",
        },
        "test_target": {
            "type": "string",
            "description": "Targeted unittest/pytest command the verifier must run (e.g. 'python -m unittest discover -s tests -p test_feature.py').",
        },
        "action": {
            "type": "string",
            "enum": ["run", "finalize"],
            "description": "'run' executes the full three-stage relay (default); 'finalize' applies the parent's merge/revert verdict from a verification ticket.",
        },
        "subagent_id": {
            "type": "string",
            "description": "For action='finalize': the builder/owner subagent id whose verification ticket is applied.",
        },
        "reason": {
            "type": "string",
            "description": "For action='finalize': the review reason recorded with the verdict.",
        },
    },
    "required": ["goal"],
}


def _run_pipeline(args: dict, runtime: dict | None) -> str:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return "error: relay requires a non-empty 'goal'"
    target_modules = [
        str(m).strip() for m in (args.get("target_modules") or []) if str(m).strip()
    ]
    test_target = str(args.get("test_target") or "").strip()
    parent_task_id = str((runtime or {}).get("task_id") or "").strip()

    scout_prompt = (
        f"Relay objective: {goal}\n"
        f"Investigate the affected modules first: {', '.join(target_modules) if target_modules else 'as implied by the objective'}.\n"
        "Deliver: (1) a concise topology/impact report, (2) the exact affected file "
        "list, (3) any constraints the builder must respect."
    )
    scout = run_subagent(parent_task_id, scout_prompt, role_mode="scout", runtime=runtime)
    if not scout.get("ok") or scout.get("status") != "completed":
        return json.dumps(
            {
                "stage": "scout",
                "ok": False,
                "summary": "Scout stage failed; relay halted before any code change.",
                "scout": scout,
            },
            ensure_ascii=False,
        )

    builder_prompt = (
        f"Relay objective: {goal}\n"
        "Implement the change in your isolated worktree, keep the diff minimal, "
        "and self-test before finishing."
    )
    builder = run_subagent(
        parent_task_id,
        builder_prompt,
        role_mode="builder",
        inherited_context={
            "anchor_node_id": scout.get("anchor_node_id", ""),
            "anchor_ckpt_aligned": scout.get("anchor_ckpt_aligned", False),
            "report": scout.get("report", ""),
        },
        runtime=runtime,
    )
    if not builder.get("ok") or builder.get("status") != "completed" or not builder.get("worktree_path"):
        return json.dumps(
            {
                "stage": "builder",
                "ok": False,
                "summary": "Builder stage failed; the sandbox worktree is preserved for inspection.",
                "scout": {"subagent_id": scout.get("subagent_id"), "anchor_node_id": scout.get("anchor_node_id")},
                "builder": builder,
            },
            ensure_ascii=False,
        )

    verifier_prompt = (
        f"Verify the builder delivery at {builder.get('worktree_path')} against the relay "
        f"objective: {goal}\n"
        f"Run exactly this targeted suite and report its verbatim outcome: {test_target or 'python -m unittest discover -s tests -t .'}"
    )
    verifier = run_subagent(
        parent_task_id,
        verifier_prompt,
        role_mode="verifier",
        builder_worktree=str(builder.get("worktree_path", "")),
        inherited_context={"anchor_node_id": scout.get("anchor_node_id", "")},
        runtime=runtime,
    )
    ticket = verifier.get("verification_ticket") or {}

    summary = {
        "ok": bool(verifier.get("ok")) and ticket.get("verdict") == subagent_gate.TICKET_VERIFIED,
        "stage": "verifier",
        "scout": {
            "subagent_id": scout.get("subagent_id"),
            "anchor_node_id": scout.get("anchor_node_id"),
            "anchor_ckpt_aligned": scout.get("anchor_ckpt_aligned"),
        },
        "builder": {
            "subagent_id": builder.get("subagent_id"),
            "worktree_path": builder.get("worktree_path"),
            "status": builder.get("status"),
        },
        "verifier": {
            "subagent_id": verifier.get("subagent_id"),
            "status": verifier.get("status"),
            "test_execution": verifier.get("test_execution"),
            "hazard_flags": verifier.get("hazard_flags"),
        },
        "verification_ticket": ticket,
        "next_action": (
            f"Call subagent_relay(action='finalize', subagent_id='{ticket.get('subagent_id')}', "
            "reason='...') to merge the delivery with --ff-only, or to destroy the sandbox "
            "and record the lesson when the verdict is rejected."
        ),
    }
    return json.dumps(summary, ensure_ascii=False)


def _finalize(args: dict) -> str:
    subagent_id = str(args.get("subagent_id") or "").strip()
    if not subagent_id:
        return "error: finalize requires 'subagent_id' of the builder delivery"
    ticket = subagent_gate.load_verification_ticket(subagent_id)
    if ticket is None:
        return f"error: no verification ticket found for '{subagent_id}'"
    outcome = subagent_gate.finalize_subagent_relay(
        subagent_id, ticket, reason=str(args.get("reason") or "").strip()
    )
    return json.dumps(outcome, ensure_ascii=False)


def run(args: dict, runtime: dict | None = None) -> str:
    args = dict(args or {})
    action = str(args.get("action") or "run").strip().lower()
    if action == "finalize":
        return _finalize(args)
    return _run_pipeline(args, runtime)
