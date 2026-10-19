import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from core import persistence

DESCRIPTION = (
    "Read the raw original content of an archived DAG node from checkpoint.jsonl by node_id; "
    "use it to recall details folded out of the context fact ledger (see the archived-node index table)."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "node_id": {
            "type": "string",
            "description": "DAG node id from the archived-node index table, e.g. n_000004_ab12cd",
        },
        "task_id": {
            "type": "string",
            "description": "Optional task id; defaults to the parent/current task on disk",
        },
    },
    "required": ["node_id"],
}


@contextmanager
def _tasks_root(root: Path):
    with mock.patch.object(persistence, "TASKS_ROOT", Path(root)):
        yield


def _candidate_tasks(explicit_task: str, runtime: dict) -> list[str]:
    raw = [
        explicit_task,
        runtime.get("parent_task_id"),
        runtime.get("task_id"),
        persistence.get_current_session(),
    ]
    tasks: list[str] = []
    for value in raw:
        task_id = str(value or "").strip()
        if task_id and task_id not in tasks:
            tasks.append(task_id)
    return tasks


def _candidate_roots(runtime: dict) -> list[Path]:
    raw = [
        runtime.get("tasks_root"),
        runtime.get("parent_tasks_root"),
        persistence.TASKS_ROOT,
        persistence.TASKS_ROOT.parent / "subagents",
    ]
    roots: list[Path] = []
    for value in raw:
        if value is None:
            continue
        root = Path(value)
        if root not in roots:
            roots.append(root)
    return roots


def run(args: dict, runtime: dict) -> str:
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return "error: node_id is required"
    runtime = runtime or {}
    explicit_task = str(args.get("task_id") or "").strip()
    tasks = _candidate_tasks(explicit_task, runtime)
    if not tasks:
        return "error: no task_id provided and no active session on disk"

    searched: list[str] = []
    for root in _candidate_roots(runtime):
        with _tasks_root(root):
            for task_id in tasks:
                searched.append(f"{root.name}/{task_id}")
                try:
                    loaded = persistence.load_dag_nodes(task_id)
                except Exception:
                    continue
                if loaded is None:
                    continue
                nodes, _ = loaded
                for node in nodes:
                    if node.get("id") == node_id:
                        clean = persistence.strip_dag_envelope(node)
                        return json.dumps(clean, ensure_ascii=False, indent=2)

    return (
        f"error: node not found: {node_id} "
        f"(searched tasks: {', '.join(tasks)}; locations: {', '.join(searched)})"
    )
