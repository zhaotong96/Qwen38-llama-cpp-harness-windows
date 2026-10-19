"""
Self-evolving skill pre-flight sandbox and quarantine governance.

Three static/dynamic gates protect the skill registry:

1. AST import whitelist plus the top-level purity theorem (no calls,
   comprehensions, loops, or context managers can execute at import time).
2. A pure-stdlib recursive validator for OpenAI Function Calling JSON Schema.
3. An isolated subprocess dry-run guarded by ``sys.addaudithook``; the module
   contract is reflected before a skill is atomically promoted into ``skills/``.

Runtime failures are tracked in ``.agent/skill_health.json``. A dynamic skill
that fails three consecutive times is moved to ``.agent/skills_disabled/``.
"""
from __future__ import annotations

import ast
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
from typing import Any

from core import persistence
from core.models import AGENT_DIR, ROOT
from core.tools import _run_process


CODE_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
STAGING_DIR = AGENT_DIR / "skills_staging"
DISABLED_DIR = AGENT_DIR / "skills_disabled"
HEALTH_FILE = AGENT_DIR / "skill_health.json"

MAX_SCHEMA_DEPTH = 8
DRY_RUN_TIMEOUT = 5.0

FROZEN_CORE_SKILLS = frozenset({
    "add_skill", "branch_rewind", "chat_with_web_llm", "chrome_dom", "git",
    "keyboard", "list_dir", "modify_skill", "mouse", "patch", "pic_zoom",
    "read_checkpoint_node", "read_file", "read_skill", "screenshot", "search",
    "shell", "subagent", "task_blocked", "task_done", "ui_tree", "write_file",
})

ALLOWED_EXTERNAL_PACKAGES = frozenset({"openai", "mss", "PIL", "websocket"})
ALLOWED_INTERNAL_PACKAGES = frozenset({"core", "skills"})
ALLOWED_ROOT_IMPORTS = (
    frozenset(getattr(sys, "stdlib_module_names", ()))
    | ALLOWED_EXTERNAL_PACKAGES
    | ALLOWED_INTERNAL_PACKAGES
)
FORBIDDEN_DUNDERS = frozenset({
    "__import__", "__loader__", "__spec__", "__builtins__",
    "__subclasses__", "__globals__", "__code__", "__bases__", "__mro__",
    "__class__", "__dict__", "__getattribute__", "__reduce__", "__reduce_ex__",
})
FORBIDDEN_DYNAMIC_NAMES = frozenset({"eval", "exec", "compile", "__import__"})
ALLOWED_JSON_SCHEMA_TYPES = frozenset({
    "object", "string", "number", "integer", "boolean", "array", "null",
})

_HEALTH_LOCK = threading.Lock()


def _skills_dir() -> Path:
    return Path(SKILLS_DIR)


def _staging_dir() -> Path:
    return Path(STAGING_DIR)


def _disabled_dir() -> Path:
    return Path(DISABLED_DIR)


def _health_file() -> Path:
    return Path(HEALTH_FILE)


def validate_openai_parameters_schema(
    schema: Any, path: str = "PARAMETERS", depth: int = 0
) -> list[str]:
    """Validate one PARAMETERS value against the OpenAI function-calling subset."""
    errors: list[str] = []
    if depth > MAX_SCHEMA_DEPTH:
        return [f"{path}: schema nesting depth exceeds maximum ({MAX_SCHEMA_DEPTH})"]
    if not isinstance(schema, dict):
        return [f"{path} must be a JSON object (dict), got {type(schema).__name__}"]

    if depth == 0 and schema.get("type") != "object":
        errors.append(f"{path}.type at root must be 'object', got {schema.get('type')!r}")

    stype = schema.get("type")
    if stype not in ALLOWED_JSON_SCHEMA_TYPES:
        errors.append(
            f"{path}.type invalid: {stype!r} "
            f"(must be one of {sorted(ALLOWED_JSON_SCHEMA_TYPES)})"
        )
        return errors

    if "description" in schema and not isinstance(schema["description"], str):
        errors.append(f"{path}.description must be a string")

    if "enum" in schema:
        enum_value = schema["enum"]
        if not isinstance(enum_value, list) or not enum_value:
            errors.append(f"{path}.enum must be a non-empty list")

    if stype == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{path}.properties must be a dict for object schema")
            properties = {}
        for key, sub_schema in properties.items():
            if not isinstance(key, str) or not key.strip():
                errors.append(f"{path}.properties key {key!r} must be a non-empty string")
            errors.extend(
                validate_openai_parameters_schema(sub_schema, f"{path}.properties.{key}", depth + 1)
            )

        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            errors.append(f"{path}.required must be a list of strings")
        else:
            undefined = [item for item in required if item not in properties]
            if undefined:
                errors.append(f"{path}.required references undefined properties: {undefined}")
            if len(required) != len(set(required)):
                errors.append(f"{path}.required contains duplicate field names")

    elif stype == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            errors.append(f"{path}.items must be a schema dict for array type")
        else:
            errors.extend(validate_openai_parameters_schema(items, f"{path}.items", depth + 1))

    if depth == 0:
        try:
            json.dumps(schema)
        except Exception as exc:
            errors.append(f"{path} is not JSON-serializable: {exc}")
    return errors


def _assert_pure_expression(expr: ast.AST | None, ctx_label: str, errors: list[str]) -> None:
    """Reject any import-time executable construct below a top-level expression."""
    if expr is None:
        return
    if isinstance(expr, ast.Lambda):
        for default in expr.args.defaults + [d for d in expr.args.kw_defaults if d is not None]:
            _assert_pure_expression(default, f"{ctx_label} lambda default", errors)
        return
    if isinstance(expr, (
        ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr,
        ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    )):
        errors.append(
            f"line {getattr(expr, 'lineno', '?')}: forbidden top-level "
            f"{type(expr).__name__} in {ctx_label} "
            "(no function calls or comprehensions are allowed at import time)"
        )
        return
    for child in ast.iter_child_nodes(expr):
        _assert_pure_expression(child, ctx_label, errors)


def _check_annotations(arguments: ast.arguments, fn_name: str, errors: list[str]) -> None:
    all_args = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
    ]
    if arguments.vararg is not None:
        all_args.append(arguments.vararg)
    if arguments.kwarg is not None:
        all_args.append(arguments.kwarg)
    for arg in all_args:
        if arg is not None and arg.annotation is not None:
            _assert_pure_expression(arg.annotation, f"annotation on '{fn_name}'", errors)


def check_skill_ast(source_code: str) -> list[str]:
    """Gate 1: syntax, import whitelist, dynamic-execution ban, and top-level purity."""
    errors: list[str] = []
    try:
        tree = ast.parse(source_code)
    except SyntaxError as exc:
        return [f"SyntaxError at line {exc.lineno}, col {exc.offset}: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg not in ALLOWED_ROOT_IMPORTS:
                    errors.append(
                        f"line {node.lineno}: forbidden package '{alias.name}' "
                        f"(allowed external: {sorted(ALLOWED_EXTERNAL_PACKAGES)})"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                errors.append(f"line {node.lineno}: relative imports are forbidden in skills")
            elif node.module:
                root_pkg = node.module.split(".")[0]
                if root_pkg not in ALLOWED_ROOT_IMPORTS:
                    errors.append(f"line {node.lineno}: forbidden package '{node.module}'")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_DUNDERS:
            errors.append(f"line {node.lineno}: forbidden dunder identifier '{node.id}'")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_DUNDERS:
            errors.append(f"line {node.lineno}: forbidden dunder attribute '.{node.attr}'")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_DYNAMIC_NAMES:
            errors.append(
                f"line {node.lineno}: dynamic code execution primitive '{node.id}' is forbidden"
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_DYNAMIC_NAMES:
                errors.append(
                    f"line {node.lineno}: dynamic code execution '{node.func.id}()' is forbidden"
                )

    assigned: set[str] = set()
    callables: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            callables.add(stmt.name)
            for decorator in stmt.decorator_list:
                _assert_pure_expression(decorator, f"decorator on '{stmt.name}'", errors)
            for default in stmt.args.defaults + [d for d in stmt.args.kw_defaults if d is not None]:
                _assert_pure_expression(default, f"default argument in '{stmt.name}'", errors)
            _check_annotations(stmt.args, stmt.name, errors)
            _assert_pure_expression(stmt.returns, f"return annotation on '{stmt.name}'", errors)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
                else:
                    errors.append(
                        f"line {stmt.lineno}: top-level assignment target must be a simple variable name"
                    )
            _assert_pure_expression(stmt.value, "top-level assignment", errors)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                assigned.add(stmt.target.id)
                _assert_pure_expression(
                    stmt.annotation, "top-level annotated assignment", errors
                )
            else:
                errors.append(
                    f"line {stmt.lineno}: top-level assignment target must be a simple variable name"
                )
            _assert_pure_expression(stmt.value, "top-level annotated assignment", errors)
        else:
            errors.append(
                f"line {stmt.lineno}: forbidden top-level statement '{type(stmt).__name__}' "
                "(only imports, constant assignments, and function definitions are permitted)"
            )

    if "DESCRIPTION" not in assigned:
        errors.append("missing top-level 'DESCRIPTION' assignment (SPEC §4)")
    if "PARAMETERS" not in assigned:
        errors.append("missing top-level 'PARAMETERS' assignment (SPEC §4)")
    if "HANDLER" not in assigned and "run" not in callables and "HANDLER" not in callables:
        errors.append("must define either 'HANDLER' or 'def run(args, runtime)' (SPEC §4)")

    return errors


_DRY_RUN_DRIVER = r'''
import json
import inspect
import sys
from pathlib import Path

staged_path = Path(sys.argv[1]).resolve()
code_root = Path(sys.argv[2]).resolve()
if str(code_root) not in sys.path:
    sys.path.insert(0, str(code_root))

from core.skill_sandbox import validate_openai_parameters_schema

BLOCKED_EVENTS = (
    "subprocess.", "os.system", "os.spawn", "os.exec", "os.fork", "os.startfile",
    "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace", "shutil.",
    "socket.", "urllib.", "http.", "ftplib.", "smtplib.", "winreg.",
    "ctypes.dlopen", "ctypes.dlsym", "pty.spawn", "webbrowser.open",
)


def _audit_guard(event, args):
    if event == "open" and len(args) >= 2:
        mode = str(args[1] or "r")
        if any(marker in mode for marker in ("w", "a", "x", "+")):
            raise PermissionError(
                "import-time file write blocked: %r (mode=%r)" % (args[0], mode)
            )
    for prefix in BLOCKED_EVENTS:
        if event == prefix or event.startswith(prefix):
            raise PermissionError("import-time side effect blocked by audit hook: %s" % event)


sys.addaudithook(_audit_guard)

try:
    source = staged_path.read_text(encoding="utf-8")
    namespace = {"__name__": "_staged_skill", "__file__": str(staged_path)}
    exec(compile(source, str(staged_path), "exec"), namespace)

    description = namespace.get("DESCRIPTION")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("DESCRIPTION must be a non-empty string")

    parameters = namespace.get("PARAMETERS")
    schema_errors = validate_openai_parameters_schema(parameters)
    if schema_errors:
        raise ValueError("Invalid PARAMETERS schema: " + "; ".join(schema_errors))

    handler = namespace.get("HANDLER")
    run_fn = namespace.get("run")
    if handler is not None and not callable(handler):
        raise ValueError("HANDLER must be callable")
    if run_fn is not None and not callable(run_fn) and handler is None:
        raise ValueError("run must be callable")
    if not callable(handler) and not callable(run_fn):
        raise ValueError("Skill must export a callable HANDLER or run(args, runtime)")

    if callable(run_fn) and not callable(handler):
        signature = inspect.signature(run_fn)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
            for parameter in signature.parameters.values()
        )
        if len(positional) < 2 and not has_varargs:
            raise ValueError("run(args, runtime) must accept at least 2 positional arguments")

    print(json.dumps({"ok": True, "description": description.strip()}))
except Exception as exc:
    print(
        json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}),
        file=sys.stderr,
    )
    sys.exit(1)
'''


def verify_and_deploy_skill(skill_name: str, code: str, overwrite: bool = False) -> str:
    """Run all three gates and atomically promote a validated dynamic skill."""
    name = Path((skill_name or "").strip()).name.removesuffix(".py").strip()
    if not name or not name.isidentifier() or name.startswith("_"):
        return f"error: invalid skill_name {skill_name!r} (must be a valid public Python identifier)"
    if name in FROZEN_CORE_SKILLS:
        return f"error: '{name}' is a protected core skill and cannot be added or overwritten"

    skills_dir = _skills_dir().resolve()
    target_path = (skills_dir / f"{name}.py").resolve()
    if target_path.parent != skills_dir:
        return f"error: skill path escapes the skills directory: {target_path}"
    if not overwrite and target_path.exists():
        return f"error: skill {name}.py already exists"
    if overwrite and not target_path.exists():
        return f"error: skill {name}.py does not exist"

    ast_errors = check_skill_ast(code)
    if ast_errors:
        return "error: [SKILL_SANDBOX_AST_REJECTED] " + " | ".join(ast_errors)

    staging_dir = _staging_dir()
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staging_dir / f"{name}.py"
    persistence.atomic_write_text(staged_path, code)

    try:
        status, stdout, stderr, returncode = _run_process(
            [sys.executable, "-I", "-B", "-c", _DRY_RUN_DRIVER, str(staged_path), str(CODE_ROOT)],
            timeout=DRY_RUN_TIMEOUT,
        )
    finally:
        staged_path.unlink(missing_ok=True)

    if status != "ok" or returncode != 0:
        detail = (stderr or stdout or status).strip()
        return f"error: [SKILL_SANDBOX_DRYRUN_REJECTED] ({status}, exit={returncode}): {detail[:600]}"

    persistence.atomic_write_text(target_path, code)
    reset_skill_failures(name)
    action_word = "updated" if overwrite else "created"
    return f"{action_word} skill {target_path.name} (passed AST, Schema, and Dry-Run sandbox gates)"


def _load_health() -> dict[str, Any]:
    health_file = _health_file()
    if not health_file.is_file():
        return {}
    try:
        data = json.loads(health_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_health(data: dict[str, Any]) -> None:
    persistence.atomic_write_text(
        _health_file(),
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def record_skill_outcome(
    skill_name: str, succeeded: bool, error_detail: str = ""
) -> str | None:
    """Track consecutive runtime failures and quarantine on the third one."""
    if skill_name in FROZEN_CORE_SKILLS:
        return None

    with _HEALTH_LOCK:
        data = _load_health()
        entry = data.get(skill_name)
        if not isinstance(entry, dict):
            entry = {"consecutive_failures": 0}

        if succeeded:
            data[skill_name] = {
                "consecutive_failures": 0,
                "status": "healthy",
                "last_success": datetime.now().isoformat(timespec="seconds"),
            }
            _save_health(data)
            return None

        count = int(entry.get("consecutive_failures", 0)) + 1
        entry = {
            "consecutive_failures": count,
            "last_error": error_detail[:300],
            "status": "degraded",
        }
        if count < 3:
            data[skill_name] = entry
            _save_health(data)
            return None

        skill_path = _skills_dir() / f"{skill_name}.py"
        disabled_dir = _disabled_dir()
        disabled_dir.mkdir(parents=True, exist_ok=True)
        moved = False
        if skill_path.is_file():
            destination = disabled_dir / f"{skill_name}.py"
            try:
                skill_path.replace(destination)
                moved = True
            except OSError:
                try:
                    destination.write_text(
                        skill_path.read_text(encoding="utf-8", errors="replace"),
                        encoding="utf-8",
                    )
                    skill_path.unlink(missing_ok=True)
                    moved = True
                except OSError:
                    moved = False

        entry["status"] = "quarantined"
        entry["quarantined_path"] = f".agent/skills_disabled/{skill_name}.py"
        data[skill_name] = entry
        _save_health(data)
        verb = "was moved to" if moved else "was marked for isolation at"
        return (
            f"[SKILL_QUARANTINED] Dynamic skill '{skill_name}' failed 3 consecutive times "
            f"and {verb} .agent/skills_disabled/{skill_name}.py."
        )


def reset_skill_failures(skill_name: str) -> None:
    """Clear a skill's failure counter after a successful re-deployment."""
    with _HEALTH_LOCK:
        health_file = _health_file()
        if not health_file.is_file():
            return
        data = _load_health()
        if skill_name in data:
            data.pop(skill_name, None)
            _save_health(data)
