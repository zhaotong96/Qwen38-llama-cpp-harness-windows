"""SubAgent worktree isolation, tamper-proof artifact cards, and hazard gates.

Every SubAgent runs inside a dedicated Git worktree (``.agent/worktrees/<id>``,
branch ``subtask_<id>``) so the main workspace stays physically pristine even
when the SubAgent crashes mid-edit. When the SubAgent exits, the Python host —
never the SubAgent LLM — deterministically captures the objective telemetry
(modified files, unified diff, SHA-256 hashes, executed tests) into an
``artifact_card.json`` and pre-screens it for five static hazard patterns.
The parent agent reviews the card adversarially and renders a final
``merge`` (fast-forward) or ``revert`` (destroy + lesson) verdict through the
``subagent_review`` skill.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any

from core import persistence
from core.models import ROOT
from core.skill_sandbox import FROZEN_CORE_SKILLS
from core.tools import NON_INTERACTIVE_ENV

_HOST_EPHEMERAL_KEY: bytes = secrets.token_bytes(32)

WORKTREES_DIRNAME = "worktrees"
GIT_TIMEOUT = 60.0

TICKET_VERIFIED = "VERIFIED_READY_TO_MERGE"
TICKET_REJECTED = "REJECTED_VERIFICATION_FAILED"

HAZARD_TEST_ASSERTION_REMOVED = "HAZARD_TEST_ASSERTION_REMOVED"
HAZARD_TEST_SKIP_INJECTED = "HAZARD_TEST_SKIP_INJECTED"
HAZARD_UNVERIFIED_CODE_EDIT = "HAZARD_UNVERIFIED_CODE_EDIT"
HAZARD_REDLINE_FILE_MODIFIED = "HAZARD_REDLINE_FILE_MODIFIED"
HAZARD_EMPTY_OR_NOOP_DIFF = "HAZARD_EMPTY_OR_NOOP_DIFF"

_FLAG_ORDER = (
    HAZARD_TEST_ASSERTION_REMOVED,
    HAZARD_TEST_SKIP_INJECTED,
    HAZARD_UNVERIFIED_CODE_EDIT,
    HAZARD_REDLINE_FILE_MODIFIED,
    HAZARD_EMPTY_OR_NOOP_DIFF,
)

# Files whose modification is an architecture redline violation for a SubAgent.
REDLINE_FILES = frozenset({"requirements.txt", "docs/SPEC.md", "package.json"})

_WRITE_TOOLS = ("write_file", "patch", "modify_skill", "add_skill")

_REMOVED_ASSERTION_RE = re.compile(
    r"\bdef\s+test_[A-Za-z0-9_]*\s*\("  # a whole removed test case
    r"|\bself\.assert[A-Za-z_]*"      # a removed unittest assertion
    r"|(?<![\w.])(?:assert|assertEqual|assertTrue|assertFalse)\s"  # a removed assert statement
)
_ADDED_SKIP_RE = re.compile(r"@unittest\.skip|pytest\.mark\.skip|\.skipTest\(")
_ADDED_DEF_TEST_RE = re.compile(r"\bdef\s+test_[A-Za-z0-9_]*\s*\(")
_BARE_PASS_RE = re.compile(r"^\s*pass\s*(#.*)?$")


def subagent_worktrees_root(repo_root: Path = ROOT) -> Path:
    """Resolve .agent/worktrees under the given repository root."""
    return Path(repo_root) / ".agent" / WORKTREES_DIRNAME


def _run_git(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run one git command with non-interactive env; returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **NON_INTERACTIVE_ENV},
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 128, "", f"git invocation failed: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip('"')


def worktree_branch_name(subagent_id: str) -> str:
    return f"subtask_{subagent_id}"


def _drop_stale_worktree(subagent_id: str, wt_path: Path, repo_root: Path) -> None:
    """Best-effort cleanup of a leftover worktree from a crashed run."""
    _run_git(["worktree", "remove", "--force", str(wt_path)], repo_root)
    _run_git(["branch", "-D", worktree_branch_name(subagent_id)], repo_root)
    _run_git(["worktree", "prune"], repo_root)


def create_subagent_worktree(subagent_id: str, repo_root: Path = ROOT) -> Path:
    """Create an isolated Git worktree for the SubAgent and return its path.

    Runs ``git worktree add -B subtask_<id> .agent/worktrees/<id> HEAD`` so the
    child shares the object store (creation is milliseconds, near-zero disk)
    while every file write stays physically separated from the main checkout.
    """
    persistence._validate_identifier(subagent_id, "subagent id")
    repo_root = Path(repo_root).resolve()
    wt_root = subagent_worktrees_root(repo_root)
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / subagent_id
    branch = worktree_branch_name(subagent_id)

    if wt_path.exists():
        _drop_stale_worktree(subagent_id, wt_path, repo_root)

    rc, _, err = _run_git(["worktree", "add", "-B", branch, str(wt_path), "HEAD"], repo_root)
    if rc != 0:
        raise RuntimeError(
            f"git worktree add failed for '{subagent_id}': {(err or '').strip()[:400]}"
        )
    return wt_path


def detect_diff_hazards(
    files_modified: list[dict[str, Any]] | None,
    diff_text: str | None,
    tests_executed: list[dict[str, Any]] | None,
    last_write_step: int | None = None,
) -> list[str]:
    """Deterministically pre-screen a SubAgent diff for the five hazard classes.

    1. ``HAZARD_TEST_ASSERTION_REMOVED`` — deletions inside ``tests/`` that
       remove ``def test_`` cases or ``assert`` statements (green-by-deletion).
    2. ``HAZARD_TEST_SKIP_INJECTED`` — added ``@unittest.skip`` /
       ``pytest.mark.skip`` / ``skipTest`` calls or empty ``pass`` test stubs.
    3. ``HAZARD_UNVERIFIED_CODE_EDIT`` — business ``.py`` files changed with
       no passing (``exit=0``) test execution recorded after the last write.
    4. ``HAZARD_REDLINE_FILE_MODIFIED`` — requirements.txt / docs/SPEC.md /
       package.json / frozen core skill touched.
    5. ``HAZARD_EMPTY_OR_NOOP_DIFF`` — a completed delivery with no diff at all.
    """
    found: set[str] = set()
    files = [
        {
            "path": _normalize(entry.get("path", "")),
            "status": str(entry.get("status", "")).strip(),
        }
        for entry in (files_modified or [])
        if isinstance(entry, dict)
    ]
    diff = diff_text or ""

    current_file = ""
    last_added_is_def_test = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r'diff --git a/(.+?) b/(.+?)$', line)
            if match:
                current_file = _normalize(match.group(2))
            last_added_is_def_test = False
        elif line.startswith("+++ "):
            current_file = _normalize(line[4:].removeprefix("b/"))
        elif line.startswith("--- ") or line.startswith("@@ ") or line.startswith("index "):
            last_added_is_def_test = False
        elif line.startswith("+"):
            added = line[1:]
            if _ADDED_SKIP_RE.search(added):
                found.add(HAZARD_TEST_SKIP_INJECTED)
            if _ADDED_DEF_TEST_RE.search(added):
                last_added_is_def_test = True
            elif last_added_is_def_test and _BARE_PASS_RE.match(added):
                found.add(HAZARD_TEST_SKIP_INJECTED)
                last_added_is_def_test = False
            else:
                last_added_is_def_test = False
        elif line.startswith("-"):
            removed = line[1:]
            if current_file.startswith("tests/") and _REMOVED_ASSERTION_RE.search(removed):
                found.add(HAZARD_TEST_ASSERTION_REMOVED)

    # Hazard 3: modified business code without a subsequent passing test run.
    code_files = [
        entry["path"]
        for entry in files
        if entry["path"].endswith(".py") and not entry["path"].startswith("tests/")
    ]
    if code_files:
        passing_after_write = [
            test
            for test in (tests_executed or [])
            if isinstance(test, dict)
            and test.get("exit_code") == 0
            and (
                last_write_step is None
                or int(test.get("step") or 0) > int(last_write_step)
            )
        ]
        if not passing_after_write:
            found.add(HAZARD_UNVERIFIED_CODE_EDIT)

    # Hazard 4: redline / frozen core files.
    for entry in files:
        path = entry["path"]
        if path in REDLINE_FILES:
            found.add(HAZARD_REDLINE_FILE_MODIFIED)
        elif path.startswith("skills/") and path.endswith(".py"):
            stem = Path(path).stem
            if stem in FROZEN_CORE_SKILLS:
                found.add(HAZARD_REDLINE_FILE_MODIFIED)

    # Hazard 5: completed delivery with zero physical change.
    if not files and not diff.strip():
        found.add(HAZARD_EMPTY_OR_NOOP_DIFF)

    return [flag for flag in _FLAG_ORDER if flag in found]


def _sha256_of(file_path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _git_porcelain_files(wt_path: Path) -> list[dict[str, Any]]:
    """Parse ``git status --porcelain`` into the objective file manifest."""
    rc, status_out, _ = _run_git(["status", "--porcelain"], wt_path)
    if rc != 0:
        return []
    files: list[dict[str, Any]] = []
    for line in status_out.splitlines():
        if not line.strip() or len(line) < 4:
            continue
        status_code = line[:2].strip() or "?"
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        path = _normalize(raw_path)
        sha256 = _sha256_of(wt_path / path) if status_code != "D" else ""
        files.append({"path": path, "status": status_code, "sha256": sha256})
    return files


def artifact_card_path(subagent_id: str) -> Path:
    """Disk location of the immutable delivery card for one SubAgent."""
    return persistence.TASKS_ROOT.parent / "subagents" / subagent_id / "artifact_card.json"


def generate_artifact_card(
    subagent_id: str,
    wt_path: Path,
    tests_executed: list[dict[str, Any]] | None = None,
    summary: str = "",
    last_write_step: int | None = None,
    terminal_state: str = "completed",
    risk_assessment: str = "",
) -> dict[str, Any]:
    """Capture the objective SubAgent telemetry into a tamper-proof card.

    ``files_modified``, ``git_diff_patch`` and the SHA-256 hashes are computed
    by the Python host from Git directly; the SubAgent LLM can never overwrite
    them (Gate 2). The card is persisted under ``.agent/subagents/<id>/``.
    """
    persistence._validate_identifier(subagent_id, "subagent id")
    wt_path = Path(wt_path)

    # Intent-to-add untracked files so `git diff HEAD` covers brand-new files.
    _run_git(["add", "--intent-to-add", "--all", "."], wt_path)
    files_modified = _git_porcelain_files(wt_path)
    rc, diff_out, _ = _run_git(["diff", "HEAD"], wt_path)
    if rc != 0:
        diff_out = ""
    hazard_flags = detect_diff_hazards(
        files_modified, diff_out, tests_executed, last_write_step
    )

    card: dict[str, Any] = {
        "subagent_id": subagent_id,
        "branch": worktree_branch_name(subagent_id),
        "worktree_path": f".agent/{WORKTREES_DIRNAME}/{subagent_id}",
        "terminal_state": terminal_state,
        "hazard_flags": hazard_flags,
        "files_modified": files_modified,
        "tests_executed": list(tests_executed or []),
        "git_diff_patch": diff_out,
        "subagent_summary": str(summary or "")[:2000],
        "risk_assessment": str(risk_assessment or "").strip()
        or "host_determined_from_hazard_flags",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    card_file = artifact_card_path(subagent_id)
    persistence.atomic_write_text(
        card_file, json.dumps(card, ensure_ascii=False, indent=2) + "\n"
    )
    card["artifact_card_path"] = f".agent/subagents/{subagent_id}/artifact_card.json"
    return card


def format_adversarial_review_prompt(card: dict[str, Any]) -> str:
    """Render the adversarial falsification review card for the parent agent.

    Order is mandatory: objective telemetry first, hazard warnings loudly,
    the SubAgent's own summary last and explicitly marked UNTRUSTED.
    """
    hazards = card.get("hazard_flags") or []
    files = card.get("files_modified") or []
    tests = card.get("tests_executed") or []
    file_lines = "\n".join(
        f"  - [{entry.get('status', '?')}] {entry.get('path', '?')}"
        f" (sha256={str(entry.get('sha256', ''))[:12]})"
        for entry in files
    ) or "  (none)"
    test_lines = "\n".join(
        f"  - step {entry.get('step', '?')}: exit={entry.get('exit_code')} "
        f"cmd={str(entry.get('command', ''))[:120]}"
        for entry in tests
    ) or "  (no test executions recorded)"

    diff = str(card.get("git_diff_patch") or "")
    if len(diff) > 6000:
        diff = diff[:6000] + "\n...[diff truncated by host]"

    return "\n".join([
        "[SUBAGENT ARTIFACT REVIEW] Objective telemetry below is host-generated and tamper-proof.",
        f"subagent_id: {card.get('subagent_id')}",
        f"branch: {card.get('branch')}  worktree: {card.get('worktree_path')}",
        f"terminal_state: {card.get('terminal_state', 'unknown')}",
        f"HAZARD_FLAGS: {', '.join(hazards) if hazards else '(none detected by static screen)'}",
        f"files_modified ({len(files)}):",
        file_lines,
        "tests_executed:",
        test_lines,
        "git_diff_patch (verbatim):",
        diff or "(empty diff)",
        "[SUBAGENT SELF-REPORT — UNTRUSTED, verify every claim against the telemetry above]",
        str(card.get("subagent_summary", "")),
        "[ADVERSARIAL FALSIFICATION CHECKLIST]",
        "Q1 Authenticity: does the diff truly implement the objective, without stubs or unrelated edits?",
        "Q2 Verification: after the last file write, is there a real test command that returned exit=0?",
        "Q3 Compliance: any redline file touched or unreported dirty file?",
        "Decision: call subagent_review(subagent_id=..., decision='merge'|'revert', reason=...). "
        "Merge ONLY when Q1-Q3 all pass with evidence; otherwise revert and record the lesson.",
    ])


def _commit_worktree_changes(subagent_id: str, wt_path: Path, reason: str) -> tuple[bool, str]:
    """Commit the SubAgent's worktree delivery; returns (ok, error_message)."""
    _run_git(["add", "-A", "."], wt_path)
    message = f"subagent({subagent_id}): {reason[:120] or 'delivery'}"
    rc, _, err = _run_git(["commit", "-m", message], wt_path)
    if rc == 0:
        return True, ""
    combined = (err or "").lower()
    if "identity" in combined or "user.name" in combined or "user.email" in combined:
        rc, _, err = _run_git(
            [
                "-c", "user.name=SubAgent Harness",
                "-c", "user.email=subagent@harness.local",
                "commit", "-m", message,
            ],
            wt_path,
        )
        if rc == 0:
            return True, ""
    if "nothing to commit" in combined or "nothing added to commit" in combined:
        return True, ""
    return False, (err or "commit failed").strip()[:400]


def finalize_subagent_worktree(
    subagent_id: str,
    decision: str,
    reason: str,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    """Execute the parent's review verdict on an isolated SubAgent worktree.

    ``merge``  : commit the worktree, fast-forward the main workspace
                 (``git merge --ff-only``), then destroy the sandbox.
    ``revert`` : destroy the sandbox unconditionally and return the causal
                 lesson line for the fact ledger's section 4.
    """
    persistence._validate_identifier(subagent_id, "subagent id")
    decision = str(decision or "").strip().lower()
    if decision not in ("merge", "revert"):
        return {"ok": False, "error": f"invalid decision {decision!r} (use 'merge' or 'revert')"}
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "error": "a non-empty review reason is required"}

    repo_root = Path(repo_root).resolve()
    wt_path = subagent_worktrees_root(repo_root) / subagent_id
    branch = worktree_branch_name(subagent_id)

    if decision == "revert":
        removed = False
        if wt_path.exists():
            rc, _, err = _run_git(["worktree", "remove", "--force", str(wt_path)], repo_root)
            removed = rc == 0
            if not removed:
                return {"ok": False, "error": f"worktree removal failed: {err.strip()[:300]}"}
        _run_git(["branch", "-D", branch], repo_root)
        _run_git(["worktree", "prune"], repo_root)
        lesson = (
            f"[Branch Rewind Lesson] (SubAgent '{subagent_id}' reverted): {reason}"
        )
        return {
            "ok": True,
            "decision": "revert",
            "worktree_removed": removed,
            "branch_deleted": True,
            "lesson": lesson,
        }

    # decision == "merge"
    if not wt_path.exists():
        return {"ok": False, "error": f"worktree not found for '{subagent_id}'"}
    committed, commit_err = _commit_worktree_changes(subagent_id, wt_path, reason)
    if not committed:
        return {"ok": False, "error": f"worktree commit failed: {commit_err}"}
    rc, _, err = _run_git(["merge", "--ff-only", branch], repo_root)
    if rc != 0:
        return {"ok": False, "error": f"fast-forward merge failed: {(err or '').strip()[:400]}"}
    removed_rc, _, _ = _run_git(["worktree", "remove", str(wt_path)], repo_root)
    if removed_rc != 0:
        _run_git(["worktree", "remove", "--force", str(wt_path)], repo_root)
    _run_git(["branch", "-D", branch], repo_root)
    return {
        "ok": True,
        "decision": "merge",
        "merged_branch": branch,
        "worktree_removed": True,
        "branch_deleted": True,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Phase 12C: tamper-proof verification tickets and relay finalization
# ---------------------------------------------------------------------------

def verification_ticket_path(subagent_id: str) -> Path:
    """Disk location of the deterministic verification ticket for one relay."""
    persistence._validate_identifier(subagent_id, "subagent id")
    return persistence.TASKS_ROOT.parent / "subagents" / subagent_id / "verification_ticket.json"


def _canonical_ticket_digest(ticket: dict[str, Any]) -> str:
    """SHA-256 over the ticket's canonical JSON (sorted keys, no digest/sig field)."""
    canonical = {k: v for k, v in ticket.items() if k not in ("payload_sha256", "signature_hmac")}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _canonical_ticket_hmac(ticket: dict[str, Any], key: bytes = _HOST_EPHEMERAL_KEY) -> str:
    """HMAC-SHA256 signature using the host's ephemeral memory-only key."""
    canonical = {k: v for k, v in ticket.items() if k not in ("payload_sha256", "signature_hmac")}
    payload_bytes = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(key, payload_bytes, hashlib.sha256).hexdigest()


def generate_verification_ticket(
    subagent_id: str,
    builder_diff: str,
    test_output: dict[str, Any],
    hazards: list[str],
    builder_worktree: str = "",
    scout_anchor_node: str = "",
    hmac_key: bytes = _HOST_EPHEMERAL_KEY,
) -> dict[str, Any]:
    """Deterministically issue the Verifier's tamper-proof relay ticket.

    ``VERIFIED_READY_TO_MERGE`` requires a 100% green test run AND zero
    hazard flags; anything else is force-rejected. Both canonical SHA-256
    and host-ephemeral HMAC-SHA256 signatures are sealed into the ticket.
    """
    persistence._validate_identifier(subagent_id, "subagent id")
    test_output = dict(test_output or {})
    try:
        exit_code = int(test_output.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = 1
    hazards = [str(h) for h in (hazards or [])]
    tests_run = test_output.get("tests_run")
    try:
        tests_run = int(tests_run) if tests_run is not None else None
    except (TypeError, ValueError):
        tests_run = None

    ticket: dict[str, Any] = {
        "subagent_id": subagent_id,
        "builder_worktree": builder_worktree or f".agent/{WORKTREES_DIRNAME}/{subagent_id}",
        "scout_anchor_node": str(scout_anchor_node or ""),
        "diff_sha256": hashlib.sha256((builder_diff or "").encode("utf-8")).hexdigest(),
        "hazards_detected": hazards,
        "test_execution": {
            "command": str(test_output.get("command") or ""),
            "exit_code": exit_code,
            "tests_run": tests_run,
            "passed": exit_code == 0,
        },
        "verdict": TICKET_VERIFIED if (exit_code == 0 and not hazards) else TICKET_REJECTED,
        "verified_at": datetime.now().isoformat(timespec="microseconds"),
    }
    ticket["payload_sha256"] = _canonical_ticket_digest(ticket)
    ticket["signature_hmac"] = _canonical_ticket_hmac(ticket, key=hmac_key)
    persistence.atomic_write_text(
        verification_ticket_path(subagent_id),
        json.dumps(ticket, ensure_ascii=False, indent=2) + "\n",
    )
    return ticket


def load_verification_ticket(subagent_id: str) -> dict[str, Any] | None:
    """Read a persisted ticket; None when absent or unreadable."""
    path = verification_ticket_path(subagent_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def verify_ticket_integrity(ticket: dict[str, Any] | None, hmac_key: bytes = _HOST_EPHEMERAL_KEY) -> bool:
    """True when the ticket's payload matches its recorded SHA-256 digest and HMAC signature."""
    if not isinstance(ticket, dict) or not ticket.get("payload_sha256"):
        return False
    if _canonical_ticket_digest(ticket) != ticket["payload_sha256"]:
        return False
    sig = ticket.get("signature_hmac")
    if not isinstance(sig, str) or not sig:
        return False
    expected = _canonical_ticket_hmac(ticket, key=hmac_key)
    if not hmac.compare_digest(sig, expected):
        return False
    return True


def finalize_subagent_relay(
    subagent_id: str,
    ticket: dict[str, Any],
    repo_root: Path = ROOT,
    reason: str = "",
) -> dict[str, Any]:
    """Execute the parent's one-click verdict from a valid verification ticket.

    ``VERIFIED_READY_TO_MERGE`` → commit + ``--ff-only`` atomic merge into the
    main workspace and sandbox destruction. Any rejected/invalid ticket →
    destroy the sandbox unconditionally and return the causal lesson.
    """
    persistence._validate_identifier(subagent_id, "subagent id")
    if not verify_ticket_integrity(ticket):
        return {"ok": False, "error": "ticket integrity check failed (tampering detected)"}
    verdict = str((ticket or {}).get("verdict") or "")
    if verdict == TICKET_VERIFIED:
        outcome = finalize_subagent_worktree(
            subagent_id, "merge", reason or "verified relay delivery", repo_root=repo_root
        )
    else:
        outcome = finalize_subagent_worktree(
            subagent_id,
            "revert",
            reason or f"relay rejected: {', '.join(ticket.get('hazards_detected') or []) or 'verification failed'}",
            repo_root=repo_root,
        )
        if outcome.get("ok"):
            outcome["lesson"] = (
                f"[Branch Rewind Lesson] (Relay '{subagent_id}' rejected: {verdict}) "
                "sandbox destroyed; delivery never reached the main workspace."
            )
    if outcome.get("ok"):
        outcome["verdict"] = verdict
    return outcome
