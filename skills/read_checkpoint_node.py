import json

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


def run(args: dict, runtime: dict) -> str:
    node_id = str(args.get("node_id") or "").strip()
    if not node_id:
        return "error: node_id is required"
    task_id = str(
        args.get("task_id")
        or (runtime or {}).get("parent_task_id")
        or persistence.get_current_session()
        or ""
    ).strip()
    if not task_id:
        return "error: no task_id provided and no active session on disk"
    try:
        loaded = persistence.load_dag_nodes(task_id)
    except Exception as e:
        return f"error: cannot load checkpoint for task {task_id}: {e}"
    if loaded is None:
        return f"error: no checkpoint found for task: {task_id}"
    nodes, _ = loaded
    for node in nodes:
        if node.get("id") == node_id:
            clean = persistence.strip_dag_envelope(node)
            return json.dumps(clean, ensure_ascii=False, indent=2)
    return f"error: node not found in task {task_id}: {node_id}"
