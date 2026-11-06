"""Macro Crystallization: distill a winning DAG trajectory into a deterministic skill.

The compiler runs a four-stage pipeline over the winning branch of a task's
``checkpoint.jsonl``:

1. **Causal spine deforestation** — drop failed retry turns and pure
   observation probes, then fold a successful ``grounding`` probe followed by
   a ``mouse`` click into a single spatially-invariant ``uia_click`` action.
2. **Taint-driven parameterization** — entity seeds harvested from the user's
   goal (ISO dates, file paths, quoted literals, numeric constants) become
   ``$slot:<name>`` parameter references; UI structure constants stay frozen.
3. **Guard & retry envelope** — every step replays through a three-attempt
   exponential-backoff executor with a window-activation probe.
4. **AST-pure emission** — the synthesized module carries only constants and
   pure function definitions at import time (verified by
   ``core.skill_sandbox.check_skill_ast``) and is promoted through the
   standard sandbox dry-run gate.

When a step misses after all retries (stale UI), the macro never crashes: it
returns a structured ``[MACRO_STALE_HANDOVER]`` receipt so the model resumes
from the exact failed step instead of restarting the whole workflow.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

from core import persistence
from core.context import ContextManager

# Pure-observation probes never belong in a replayable macro spine.
_OBSERVATION_PROBES = frozenset({"screenshot", "pic_zoom", "ui_tree"})

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s'\"”’,:;)]+|[\w\-]+\.(?:csv|xlsx|json|txt|py|sql|pdf)\b)"
)
_QUOTED_RE = re.compile("[\"'“”‘’]([^\"'“”‘’]{2,64})[\"'“”‘’]")
_NUMBER_RE = re.compile(r"\b\d{2,}\b")

_CONTROL_ID_RE = r"control_id=(\d+)"
_CENTER_RE = r"center=\((-?\d+),\s*(-?\d+)\)"
_EXIT_FAIL_RE = r"(?m)^exit=[1-9]"

_RETRY_BACKOFF_SECONDS = (0.25, 0.5, 1.0)


def extract_goal_taint_seeds(user_goal: str) -> dict[str, dict[str, str]]:
    """Harvest parameterizable entity seeds from the user's goal text.

    Returns ``{slot_name: {"value": ..., "kind": ...}}`` with deterministic
    slot names: ``target_date_N``, ``target_path_N``, ``target_text_N`` and
    ``target_value_N``. Values already captured by an earlier kind are not
    duplicated, and numbers inside dates or paths never become seeds.
    """
    goal = str(user_goal or "")
    seeds: dict[str, dict[str, str]] = {}
    seen_values: set[str] = set()

    def _take(kind: str, prefix: str, values: list[str]) -> None:
        for value in values:
            value = value.strip()
            if not value or value in seen_values:
                continue
            seen_values.add(value)
            slot = f"{prefix}_{sum(1 for k in seeds if k.startswith(prefix)) + 1}"
            seeds[slot] = {"value": value, "kind": kind}

    dates = _ISO_DATE_RE.findall(goal)
    _take("date", "target_date", dates)

    remainder = goal
    for date in dates:
        remainder = remainder.replace(date, " ")
    paths = [m.group(0) for m in _PATH_RE.finditer(remainder)]
    _take("path", "target_path", paths)
    for path in paths:
        remainder = remainder.replace(path, " ")

    quoted = [m.group(1) for m in _QUOTED_RE.finditer(remainder)]
    _take("quoted", "target_text", quoted)
    for text in quoted:
        remainder = remainder.replace(text, " ")

    numbers = _NUMBER_RE.findall(remainder)
    _take("number", "target_value", numbers)
    return seeds


def _parse_tool_calls(node: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for tc in node.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        function = tc.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except Exception:
            args = {}
        calls.append(
            {
                "id": str(tc.get("id") or ""),
                "name": str(function.get("name") or ""),
                "args": args if isinstance(args, dict) else {},
            }
        )
    return calls


def _is_mouse_click(args: dict[str, Any]) -> bool:
    return str(args.get("action") or "").strip().lower() in ("click", "dblclick")


def _receipt_blocks_replay(receipt: str) -> bool:
    """True when a receipt marks its turn as non-replayable.

    Beyond hard failures, a stale-macro handover receipt is a *failed* replay:
    the healed spine must contain only the takeover primitives, never the
    stale macro invocation itself.
    """
    if ContextManager._is_failed_tool_receipt(receipt):
        return True
    return "MACRO_STALE_AT_STEP" in receipt or "[MACRO_STALE_HANDOVER]" in receipt


def _extract_unrolled_stale_macro_prefix(
    macro_name: str, completed_count: int, failed_target: str = ""
) -> list[dict[str, Any]]:
    """Unroll completed prefix atomic steps from an existing macro skill module."""
    if completed_count <= 0 or not macro_name:
        return []
    skill_file = persistence.TASKS_ROOT.parent / "skills" / f"{macro_name}.py"
    if not skill_file.is_file():
        from core.models import ROOT
        skill_file = ROOT / "skills" / f"{macro_name}.py"
    if not skill_file.is_file():
        return []

    try:
        source = skill_file.read_text(encoding="utf-8")
        parsed = ast.parse(source)
    except Exception:
        return []

    default_args: dict[str, Any] = {}
    raw_steps: list[Any] = []
    for node in parsed.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_ARGS":
                    try:
                        val = ast.literal_eval(node.value)
                        if isinstance(val, dict):
                            default_args = val
                    except Exception:
                        pass
                elif isinstance(target, ast.Name) and target.id == "MACRO_STEPS":
                    try:
                        val = ast.literal_eval(node.value)
                        if isinstance(val, (list, tuple)):
                            raw_steps = list(val[:completed_count])
                    except Exception:
                        pass

    unrolled: list[dict[str, Any]] = []
    for step in raw_steps:
        if isinstance(step, dict) and "op" in step and step["op"] != macro_name:
            raw_args = step.get("args") or {}
            step_target = str(raw_args.get("target") or step.get("intent") or "")
            if failed_target and step_target == failed_target:
                break
            hydrated_args = {
                k: (default_args.get(v.split(":", 1)[1], v) if isinstance(v, str) and v.startswith("$slot:") else v)
                for k, v in raw_args.items()
            }
            unrolled.append({"op": step["op"], "args": hydrated_args})
    return unrolled


def extract_winning_spine(task_id: str, branch: str = "main") -> list[dict[str, Any]]:
    """Distill the winning branch into a spatially invariant action spine.

    Failed retry turns and pure observation probes are removed; a successful
    ``grounding`` probe followed by a ``mouse`` click folds into one
    ``uia_click`` semantic action. Raw absolute-pixel mouse steps are dropped
    (Gate 2: no hardcoded screen coordinates in crystallized macros); mouse
    steps carrying a ``control_id`` are kept.

    When a prior macro suffered a stale-handover, the completed prefix steps [0..k-1]
    are statically unrolled from the old macro and prepended to the takeover spine,
    solving the Prefix Amnesia Paradox without self-referential macro recursion.
    """
    chain = persistence.build_branch_nodes(task_id, branch)
    receipts: dict[str, str] = {
        str(node.get("tool_call_id")): str(node.get("content") or "")
        for node in chain
        if node.get("role") == "tool"
    }

    # Extract any unrolled prefix steps from stale handover receipts
    prefix_steps: list[dict[str, Any]] = []
    for node in chain:
        if node.get("role") != "assistant" or not node.get("tool_calls"):
            continue
        calls = _parse_tool_calls(node)
        for call in calls:
            receipt = receipts.get(call["id"], "")
            if "[MACRO_STALE_HANDOVER]" in receipt or "MACRO_STALE_AT_STEP" in receipt:
                try:
                    data = json.loads(receipt)
                    m_name = data.get("macro_name") or call.get("name")
                    c_count = int(data.get("completed_steps_count", 0))
                    failed_target = str(data.get("failed_target") or "")
                    if m_name and c_count > 0:
                        prefix_steps = _extract_unrolled_stale_macro_prefix(
                            m_name, c_count, failed_target=failed_target
                        )
                        break
                except Exception:
                    pass
        if prefix_steps:
            break

    actions: list[dict[str, Any]] = []
    for node in chain:
        if node.get("role") != "assistant" or not node.get("tool_calls"):
            continue
        calls = _parse_tool_calls(node)
        if not calls:
            continue
        # A turn survives only when every receipt exists and none failed.
        healthy = True
        for call in calls:
            receipt = receipts.get(call["id"])
            if receipt is None or _receipt_blocks_replay(receipt):
                healthy = False
                break
        if not healthy:
            continue
        if all(call["name"] in _OBSERVATION_PROBES for call in calls):
            continue
        for call in calls:
            actions.append(call)

    spine: list[dict[str, Any]] = list(prefix_steps)
    index = 0
    while index < len(actions):
        action = actions[index]
        name, args = action["name"], action["args"]
        if (
            name == "grounding"
            and index + 1 < len(actions)
            and actions[index + 1]["name"] == "mouse"
            and _is_mouse_click(actions[index + 1]["args"])
        ):
            spine.append(
                {
                    "op": "uia_click",
                    "args": {
                        "target": str(args.get("target") or ""),
                        "role": str(args.get("role") or ""),
                        "auto_id": str(args.get("auto_id") or ""),
                    },
                }
            )
            index += 2
            continue
        if name == "mouse":
            if args.get("control_id") is not None:
                spine.append(
                    {
                        "op": "mouse",
                        "args": {
                            "action": str(args.get("action") or "click"),
                            "control_id": int(args["control_id"]),
                        },
                    }
                )
            # else: raw (x, y) pixels are dropped — never spatially invariant.
            index += 1
            continue
        if name == "grounding":
            # A standalone locator probe carries no replayable side effect.
            index += 1
            continue
        spine.append({"op": name, "args": args})
        index += 1
    return spine


def _step_intent(step: dict[str, Any]) -> str:
    """Human-readable one-liner used in stale-handover recovery directives."""
    args = step.get("args") or {}
    if step.get("op") == "uia_click":
        return str(args.get("target") or "uia_click")
    if "command" in args:
        return str(args.get("command"))[:80]
    if "text" in args:
        return str(args.get("text"))[:80]
    if "path" in args:
        return str(args.get("path"))[:80]
    return str(step.get("op") or "action")


def _parameterize_steps(
    steps: list[dict[str, Any]],
    seeds: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, Any]]:
    """Replace seed values inside step args with ``$slot:<name>`` references."""
    value_to_slot = {entry["value"]: slot for slot, entry in seeds.items()}
    properties: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    parameterized: list[dict[str, Any]] = []

    def _swap(value: Any) -> Any:
        if isinstance(value, str) and value in value_to_slot:
            slot = value_to_slot[value]
            properties.setdefault(
                slot,
                {
                    "type": "string",
                    "description": f"Parameterized {seeds[slot]['kind']} seed from the original goal.",
                },
            )
            defaults.setdefault(slot, value)
            return f"$slot:{slot}"
        return value

    for step in steps:
        args = step.get("args") or {}
        new_args = {key: _swap(value) for key, value in args.items()}
        parameterized.append(
            {"op": step["op"], "args": new_args, "intent": _step_intent(step)}
        )
    schema = {
        "type": "object",
        "properties": properties,
        "required": [],
    }
    return parameterized, defaults, schema


_MACRO_TEMPLATE = '''"""
Crystallized desktop workflow macro: {description}

Auto-compiled by core/macro_compiler.py from a verified winning DAG branch.
Replays deterministically with zero model tokens; on a stale UI it returns a
structured [MACRO_STALE_HANDOVER] receipt so the agent resumes from the exact
failed step instead of restarting the workflow.
"""
from __future__ import annotations

import json
import re
import time

DESCRIPTION = {description_r}
MACRO_NAME = {macro_name_r}

PARAMETERS = {schema_r}

DEFAULT_ARGS = {defaults_r}

RETRY_BACKOFF_SECONDS = {backoff_r}

MACRO_STEPS = {steps_r}

_CONTROL_ID_RE = r"control_id=(\\d+)"
_CENTER_RE = r"center=\\((-?\\d+),\\s*(-?\\d+)\\)"
_EXIT_FAIL_RE = r"(?m)^exit=[1-9]"


def _bind_param(value, args):
    """Resolve ``$slot:<name>`` references against the call args, then defaults."""
    if isinstance(value, str) and value.startswith("$slot:"):
        slot = value.split(":", 1)[1]
        return str(args.get(slot, DEFAULT_ARGS.get(slot, "")))
    if isinstance(value, list):
        return [_bind_param(item, args) for item in value]
    if isinstance(value, dict):
        return {{key: _bind_param(item, args) for key, item in value.items()}}
    return value


def _receipt_failed(text):
    stripped = str(text).lstrip().lower()
    if stripped.startswith("error"):
        return True
    if "[execution_rejected]" in stripped or "[execution_interrupted" in stripped:
        return True
    return re.search(_EXIT_FAIL_RE, str(text)) is not None


def _activate_target_window(runtime):
    title = str(DEFAULT_ARGS.get("window_title") or "")
    if not title:
        return
    from skills import window_manager

    window_manager.run({{"action": "activate_window", "title": title}}, runtime)


def _uia_click(step_args, runtime):
    """Re-ground the semantic selector, then click the resolved control."""
    from skills import grounding
    from skills import mouse

    locator = {{
        "target": str(step_args.get("target") or ""),
        "role": str(step_args.get("role") or ""),
        "auto_id": str(step_args.get("auto_id") or ""),
    }}
    if not locator["target"]:
        raise RuntimeError("uia_click requires a semantic target")
    receipt = grounding.run(locator, runtime)
    text = str(receipt)
    if "[GROUNDING_HIT]" not in text:
        raise RuntimeError(
            "grounding miss for target %r: %s" % (locator["target"], text[:160])
        )
    click_args = {{"action": "click"}}
    control_match = re.search(_CONTROL_ID_RE, text)
    center_match = re.search(_CENTER_RE, text)
    if control_match:
        click_args["control_id"] = int(control_match.group(1))
    elif center_match:
        click_args["x"] = int(center_match.group(1))
        click_args["y"] = int(center_match.group(2))
    else:
        raise RuntimeError("grounding hit without a clickable center")
    return mouse.HANDLER(**click_args)


def _execute_op(op, step_args, runtime):
    """Dispatch one crystallized action through its skill module."""
    if op == "uia_click":
        return _uia_click(step_args, runtime)
    import importlib

    module = importlib.import_module("skills." + op)
    handler = getattr(module, "HANDLER", None)
    if callable(handler):
        return handler(**step_args)
    run_fn = getattr(module, "run", None)
    if callable(run_fn):
        return run_fn(dict(step_args), runtime)
    raise RuntimeError("macro op has no callable entry point: " + str(op))


def _run_step_with_retry(step, args, runtime):
    """Three attempts with 0.25s/0.5s/1.0s exponential backoff per step."""
    step_args = _bind_param(step.get("args") or {{}}, args)
    op = str(step.get("op") or "")
    last_error = ""
    for attempt in range(len(RETRY_BACKOFF_SECONDS)):
        try:
            if op in ("uia_click", "mouse", "clipboard", "keyboard"):
                _activate_target_window(runtime)
            receipt = _execute_op(op, step_args, runtime)
            if _receipt_failed(receipt):
                raise RuntimeError(str(receipt)[:240])
            return {{"ok": True, "receipt": str(receipt)}}
        except Exception as exc:
            last_error = str(exc)
            if attempt < len(RETRY_BACKOFF_SECONDS) - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
    return {{"ok": False, "error": last_error}}


def run(args, runtime=None):
    """Replay the crystallized workflow; 0 model tokens, millisecond latency."""
    args = dict(args or {{}})
    runtime = runtime or {{}}
    completed = []
    for index, step in enumerate(MACRO_STEPS, start=1):
        outcome = _run_step_with_retry(step, args, runtime)
        if outcome.get("ok"):
            completed.append(index)
            continue
        bound_args = _bind_param(step.get("args") or {{}}, args)
        if completed:
            progress_note = "Steps 0..%d already executed on screen." % (len(completed) - 1)
        else:
            progress_note = "No steps executed yet on screen."
        stale_directive = (
            "[MACRO_STALE_HANDOVER] %s Step %d ('%s') failed due to UI change. "
            "Please take over from Step %d."
        ) % (progress_note, index, str(step.get("intent", "")), index)
        failed_target = ""
        if isinstance(bound_args, dict):
            failed_target = str(bound_args.get("target") or step.get("intent") or "")
        return json.dumps(
            {{
                "ok": False,
                "status": "MACRO_STALE_AT_STEP",
                "macro_name": MACRO_NAME,
                "completed_steps_count": len(completed),
                "failed_step_index": index,
                "failed_step_op": str(step.get("op") or ""),
                "failed_target": failed_target,
                "window_title": str(DEFAULT_ARGS.get("window_title") or ""),
                "error_snapshot": str(outcome.get("error") or "")[:300],
                "failed_step_spec": {{"op": step.get("op"), "args": bound_args}},
                "directive": stale_directive,
                "recovery_directive": (
                    "[MACRO_STALE_HANDOVER] Steps 1..{{completed}} succeeded on screen. "
                    "Do NOT restart from Step 1. Resume interactively from Step {{index}} "
                    "(intent: {{intent}}), then update this macro via modify_skill."
                ).format(completed=len(completed), index=index, intent=str(step.get("intent", ""))),
            }},
            ensure_ascii=False,
        )
    return (
        "MACRO_COMPLETED_ZERO_TOKEN: %d step(s) replayed deterministically "
        "(0 model tokens consumed)." % len(MACRO_STEPS)
    )
'''


def synthesize_macro_skill_code(
    macro_name: str,
    description: str,
    parameters_schema: dict[str, Any],
    default_values: dict[str, Any],
    compiled_steps: list[dict[str, Any]],
) -> str:
    """Emit an AST-pure Python module for the crystallized macro."""
    body = _MACRO_TEMPLATE.format(
        description=" ".join(str(description or "crystallized workflow").split()),
        description_r=repr(str(description or "crystallized workflow")),
        macro_name_r=repr(str(macro_name)),
        schema_r=repr(parameters_schema or {"type": "object", "properties": {}}),
        defaults_r=repr(default_values or {}),
        backoff_r=repr(_RETRY_BACKOFF_SECONDS),
        steps_r=repr(tuple(compiled_steps or ())),
    )
    return body


def compile_and_deploy_macro(
    task_id: str,
    macro_name: str,
    description: str,
    branch: str = "main",
    overwrite: bool = False,
) -> str:
    """End-to-end: trajectory distillation → code synthesis → sandbox promotion."""
    from core import skill_sandbox

    if not macro_name.startswith("macro_"):
        return "error: macro_name must start with 'macro_'"
    try:
        persistence._validate_identifier(task_id, "task id")
    except ValueError as exc:
        return f"error: {exc}"

    chain = persistence.build_branch_nodes(task_id, branch)
    if not chain:
        return f"error: task '{task_id}' has no checkpoint trajectory on branch '{branch}'"
    goal = next(
        (
            str(node.get("content"))
            for node in chain
            if node.get("role") == "user" and isinstance(node.get("content"), str)
        ),
        "",
    )
    if not goal.strip():
        return "error: no user goal message found in the trajectory"

    seeds = extract_goal_taint_seeds(goal)
    spine = extract_winning_spine(task_id, branch)
    if not spine:
        return (
            "error: no replayable winning actions found on branch "
            f"'{branch}' (all turns failed or were observation probes)"
        )

    # V-01: Capture activated window title from trajectory to empower pre-step probe guard
    target_window = ""
    for node in chain:
        for tc in _parse_tool_calls(node):
            if tc.get("name") == "window_manager" and isinstance(tc.get("args"), dict):
                w_title = tc["args"].get("title")
                if w_title:
                    target_window = str(w_title)
                    break
        if target_window:
            break

    steps, defaults, schema = _parameterize_steps(spine, seeds)
    if target_window:
        defaults["window_title"] = target_window
    code = synthesize_macro_skill_code(macro_name, description, schema, defaults, steps)
    return skill_sandbox.verify_and_deploy_skill(macro_name, code, overwrite=overwrite)
