"""
ReAct Loop Tool Validator and Repetition Guard.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass
class ValidationResult:
    is_valid: bool
    error_message: str | None = None
    should_halt: bool = False


class ToolValidator:
    """Validates tool arguments and prevents repetitive dead loops in ReAct execution."""

    def __init__(self, max_repeats: int = 3) -> None:
        self.max_repeats = max_repeats
        self.history: list[str] = []
        self.batch_history: list[str] = []

    def reset(self) -> None:
        self.history.clear()
        self.batch_history.clear()

    def validate_schema(self, name: str, args: dict[str, Any]) -> ValidationResult:
        """Validate input types and required fields for built-in tools."""
        if not isinstance(args, dict):
            return ValidationResult(is_valid=False, error_message=f"Tool arguments for '{name}' must be a JSON object")

        if name == "shell":
            cmd = args.get("command")
            if not cmd or not isinstance(cmd, str) or not cmd.strip():
                return ValidationResult(is_valid=False, error_message="shell requires a non-empty string 'command'")

        elif name == "read_file":
            path = args.get("path")
            if not path or not isinstance(path, str):
                return ValidationResult(is_valid=False, error_message="read_file requires a valid string 'path'")

        elif name == "write_file":
            path = args.get("path")
            content = args.get("content")
            if not path or not isinstance(path, str):
                return ValidationResult(is_valid=False, error_message="write_file requires a valid string 'path'")
            if content is None or not isinstance(content, str):
                return ValidationResult(is_valid=False, error_message="write_file requires string 'content'")

        elif name == "mouse":
            action = args.get("action", "click")
            if not isinstance(action, str) or action.lower() not in ("move", "mousemove", "click", "dblclick", "double_click", "down", "up", "mouseup", "scroll", "wheel"):
                return ValidationResult(is_valid=False, error_message=f"mouse action '{action}' is invalid")
            for coord in ("x", "y"):
                val = args.get(coord, 0)
                if not isinstance(val, (int, float)) or val < 0:
                    return ValidationResult(is_valid=False, error_message=f"mouse coordinate '{coord}' must be a non-negative number")

        elif name == "keyboard":
            action = args.get("action", "type")
            if not isinstance(action, str) or action.lower() not in ("type", "text", "write", "key", "press", "hotkey", "combo"):
                return ValidationResult(is_valid=False, error_message=f"keyboard action '{action}' is invalid")
            if action.lower() in ("type", "text", "write") and not args.get("text"):
                return ValidationResult(is_valid=False, error_message="keyboard type action requires 'text'")
            if action.lower() in ("key", "press") and not (args.get("key") or args.get("keys")):
                return ValidationResult(is_valid=False, error_message="keyboard key action requires 'key'")
            if action.lower() in ("hotkey", "combo") and not (args.get("keys") or args.get("key")):
                return ValidationResult(is_valid=False, error_message="keyboard hotkey action requires 'keys'")

        elif name == "chrome_dom":
            action = args.get("action", "links")
            valid_actions = ("list", "tabs", "list_tabs", "navigate", "goto", "open", "html", "dom", "links", "hrefs", "query", "select", "evaluate", "js", "eval")
            if not isinstance(action, str) or action.lower() not in valid_actions:
                return ValidationResult(is_valid=False, error_message=f"chrome_dom action '{action}' is invalid")

        elif name == "read_skill":
            skill_name = args.get("skill_name")
            if not skill_name or not isinstance(skill_name, str):
                return ValidationResult(is_valid=False, error_message="read_skill requires string 'skill_name'")

        elif name == "subagent":
            objective = args.get("objective")
            if not objective or not isinstance(objective, str) or not objective.strip():
                return ValidationResult(is_valid=False, error_message="subagent requires a non-empty string 'objective'")
            max_rounds = args.get("max_rounds")
            if max_rounds is not None and (not isinstance(max_rounds, int) or max_rounds <= 0):
                return ValidationResult(is_valid=False, error_message="subagent 'max_rounds' must be a positive integer")
            allowed_skills = args.get("allowed_skills")
            if allowed_skills is not None and (
                not isinstance(allowed_skills, list)
                or any(not isinstance(item, str) for item in allowed_skills)
            ):
                return ValidationResult(is_valid=False, error_message="subagent 'allowed_skills' must be a list of strings")

        return ValidationResult(is_valid=True)

    def check_repetition(self, name: str, args: dict[str, Any]) -> ValidationResult:
        """Track execution signature to prevent endless repetitive tool calls."""
        sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        self.history.append(sig)

        recent = self.history[-self.max_repeats:]
        if len(recent) == self.max_repeats and len(set(recent)) == 1:
            return ValidationResult(
                is_valid=False,
                error_message=f"Dead loop detected: Tool '{name}' repeated {self.max_repeats} consecutive times with identical parameters.",
                should_halt=True,
            )

        return ValidationResult(is_valid=True)

    def check_batch_repetition(self, round_sigs: list[str]) -> ValidationResult:
        """Track batch round signatures to detect multi-tool repetitive loops."""
        if not round_sigs:
            return ValidationResult(is_valid=True)

        batch_sig = "|".join(round_sigs)
        self.batch_history.append(batch_sig)

        recent = self.batch_history[-self.max_repeats:]
        if len(recent) == self.max_repeats and len(set(recent)) == 1:
            return ValidationResult(
                is_valid=False,
                error_message=f"Batch loop detected: Repeated same round tools {self.max_repeats} consecutive times without progress.",
                should_halt=True,
            )

        return ValidationResult(is_valid=True)

    def validate(self, name: str, args: dict[str, Any]) -> ValidationResult:
        """Run complete validation suite (schema + repetition guard)."""
        schema_res = self.validate_schema(name, args)
        if not schema_res.is_valid:
            return schema_res

        return self.check_repetition(name, args)
