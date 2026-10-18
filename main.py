"""
Local Computer Use Agent - Thin CLI Entry Point & Daemon Client.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

# Ensure repo root is on sys.path regardless of execution environment
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.agent import Agent
from core.config import load_config
from core.interrupt import set_abort_flag
from core.logger import setup_logger
from core.models import ROOT
from core.persistence import (
    checkout_branch,
    create_branch,
    get_current_session,
    list_branches,
    list_tasks,
)
from core import queue_manager


def _probe_daemon(base_url: str = "http://127.0.0.1:8765") -> dict | None:
    """Check if the local Unified Agent Web Daemon is online on port 8765."""
    try:
        req = urllib.request.Request(f"{base_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def _daemon_json(
    method: str,
    url: str,
    payload: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict]:
    """One JSON round-trip against the harness daemon; HTTP errors carry their JSON body."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            return err.code, json.loads(raw)
        except Exception:
            return err.code, {"error": raw.strip()}


def _daemon_follow_task(base_url: str, task_id: str) -> int:
    """Poll one daemon task until it reaches a terminal status, then print the outcome."""
    detail: dict = {}
    while True:
        try:
            _, detail = _daemon_json("GET", f"{base_url}/tasks/{task_id}")
        except Exception as e:
            print(f"daemon poll error: {e}")
            return 1
        status = str(detail.get("status", "unknown"))
        if status != "running":
            break
        time.sleep(0.5)
    print(f"[daemon] {task_id} -> {status}")
    last = str(detail.get("last_assistant") or "").strip()
    if last:
        print(f"assistant: {last}")
    return 0


def _daemon_list_tasks(base_url: str) -> int:
    try:
        _, listing = _daemon_json("GET", f"{base_url}/tasks")
    except Exception as e:
        print(f"daemon error: {e}")
        return 1
    tasks = listing.get("tasks") or []
    if not tasks:
        print("no resumable tasks")
        return 0
    print("resumable tasks:")
    for task in tasks:
        print(
            f"{task['id']}  created={task.get('created', '')} status={task.get('status', '')} "
            f"branch={task.get('current_branch', 'main')} messages={task.get('message_count', '')}"
        )
    return 0


def _run_daemon_client(
    argv: list[str],
    resume_requested: bool,
    resume_task_id: str | None,
    base_url: str,
    goal_mode: bool = False,
) -> int:
    """Forward this CLI session to the online daemon so disk state has a single writer."""
    if goal_mode:
        print("[daemon] note: goal mode is set on the daemon process; forwarding as-is")

    if resume_requested:
        if not resume_task_id:
            return _daemon_list_tasks(base_url)
        code, out = _daemon_json("POST", f"{base_url}/tasks/{resume_task_id}/resume", {})
        if code != 202:
            print(f"daemon error: {out.get('error', out)}")
            return 1
        return _daemon_follow_task(base_url, resume_task_id)

    if argv:
        task = " ".join(argv).strip()
        if not task or task.lower() in ("exit", "quit", "q"):
            return 0
        code, out = _daemon_json("POST", f"{base_url}/tasks", {"task": task})
        if code != 202:
            print(f"daemon error: {out.get('error', out)}")
            return 1
        print(f"[daemon] task: {task}")
        return _daemon_follow_task(base_url, out["task_id"])

    print(f"\n[Daemon client mode: forwarding to harness at {base_url}. Enter 'exit' to quit.]")
    while True:
        try:
            user = input("\nOperator> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            return 0
        if not user:
            continue
        if user.lower() in ("exit", "quit", "q"):
            print("bye")
            return 0
        command, _, argument = user.partition(" ")
        cmd_lower = command.lower()
        if cmd_lower == "resume":
            if argument.strip():
                code, out = _daemon_json("POST", f"{base_url}/tasks/{argument.strip()}/resume", {})
                if code != 202:
                    print(f"daemon error: {out.get('error', out)}")
                else:
                    _daemon_follow_task(base_url, argument.strip())
            else:
                _daemon_list_tasks(base_url)
            continue
        if cmd_lower == "queue":
            if argument.strip():
                code, out = _daemon_json(
                    "POST", f"{base_url}/queue", {"content": argument.strip(), "source": "cli"}
                )
                if code == 201:
                    print(f"queued {(out.get('item') or {}).get('id', '?')}")
                else:
                    print(f"daemon error: {out.get('error', out)}")
            else:
                _code, out = _daemon_json("GET", f"{base_url}/queue")
                items = out.get("queue") or []
                if not items:
                    print("queue empty")
                for item in items:
                    print(
                        f"[{item['id']}] prio={item.get('priority')} "
                        f"status={item.get('status')}: {item.get('content')}"
                    )
            continue
        if cmd_lower == "interrupt":
            code, out = _daemon_json(
                "POST",
                f"{base_url}/interrupt",
                {"reason": argument.strip() or "CLI operator interrupt", "source": "cli"},
            )
            if code == 200:
                print(f"abort.flag set for {out.get('task_id', '?')}")
            else:
                print(f"daemon error: {out.get('error', out)}")
            continue
        if cmd_lower == "reload":
            code, out = _daemon_json("POST", f"{base_url}/reload", {})
            if code == 200:
                print(f"skills reloaded ({out.get('skills', '?')} tools)")
            else:
                print(f"daemon error: {out.get('error', out)}")
            continue
        code, out = _daemon_json("POST", f"{base_url}/tasks", {"task": user})
        if code != 202:
            print(f"daemon busy: {out.get('error', out)}")
            continue
        _daemon_follow_task(base_url, out["task_id"])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Windows console encoding configuration
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, "reconfigure") and getattr(_stream, "encoding", "").lower() != "utf-8":
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    goal_mode = False
    if "/goal" in argv or "--goal" in argv:
        goal_mode = True
        if "/goal" in argv:
            argv.remove("/goal")
        if "--goal" in argv:
            argv.remove("--goal")

    resume_requested = False
    resume_task_id: str | None = None
    if "--resume" in argv:
        resume_requested = True
        resume_index = argv.index("--resume")
        argv.pop(resume_index)
        if resume_index < len(argv) and not argv[resume_index].startswith("-"):
            resume_task_id = argv.pop(resume_index)

    daemon_url = "http://127.0.0.1:8765"
    if _probe_daemon(daemon_url) is not None:
        return _run_daemon_client(argv, resume_requested, resume_task_id, daemon_url, goal_mode=goal_mode)

    config = load_config()
    logger = setup_logger(log_level=config.log_level)
    agent = Agent(config=config, goal_mode=goal_mode)

    logger.info("=" * 56)
    logger.info(f"agent  model={config.model}")
    logger.info(f"api    {config.base_url}")
    logger.info(f"cwd    {ROOT}")
    logger.info("skills " + " ".join(t["function"]["name"] for t in agent.tools))
    logger.info("usage: py -3 main.py [--goal] [--resume <task_id>] [\"your initial task\"]")
    logger.info("=" * 56)

    if resume_requested:
        if resume_task_id:
            agent.resume_task(resume_task_id)
        else:
            tasks = list_tasks()
            if not tasks:
                print("no resumable tasks")
            else:
                print("resumable tasks:")
                for task in tasks:
                    print(
                        f"{task['id']}  created={task['created']} "
                        f"status={task['status']} branch={task.get('current_branch', 'main')} "
                        f"messages={task['message_count']}"
                    )
        return 0

    if argv:
        task = " ".join(argv).strip()
        if task:
            if task.lower() in ("exit", "quit", "q"):
                return 0
            logger.info(f"\ntask: {task}")
            agent.run_task(task)
            return 0

    print("\n[Interactive Operator Shell active. Enter 'exit' to quit.]")
    while True:
        try:
            user = input("\nOperator> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            return 0
        if not user:
            continue
        if user.lower() in ("exit", "quit", "q"):
            print("bye")
            return 0
        command, _, argument = user.partition(" ")
        cmd_lower = command.lower()
        if cmd_lower == "reload" and not argument:
            agent.reload_skills()
            print("skills reloaded")
            continue
        if cmd_lower == "resume":
            if argument:
                agent.resume_task(argument.strip())
            else:
                tasks = list_tasks()
                if not tasks:
                    print("no resumable tasks")
                else:
                    print("resumable tasks:")
                    for task in tasks:
                        print(
                            f"{task['id']}  created={task['created']} "
                            f"status={task['status']} messages={task['message_count']}"
                        )
            continue
        if cmd_lower == "queue":
            if argument:
                item = queue_manager.enqueue(argument.strip(), source="cli")
                print(f"queued {item['id']} (priority={item['priority']})")
            else:
                items = queue_manager.list_queue()
                if not items:
                    print("queue empty")
                else:
                    for item in items:
                        print(f"[{item['id']}] prio={item['priority']} status={item['status']}: {item['content']}")
            continue
        if cmd_lower == "branches":
            tid = agent.current_task_id or get_current_session()
            if not tid:
                print("no active session")
            else:
                for bname, head in list_branches(tid).items():
                    print(f"  {bname} -> {head}")
            continue
        if cmd_lower == "branch" and argument:
            tid = agent.current_task_id or get_current_session()
            if tid:
                bname, _, from_node = argument.strip().partition(" ")
                res = create_branch(tid, bname.strip(), from_node_id=from_node.strip() or None)
                print(f"created and checked out branch {res['branch']} @ {res['head_node_id']}")
            continue
        if cmd_lower == "checkout" and argument:
            tid = agent.current_task_id or get_current_session()
            if tid:
                res = checkout_branch(tid, argument.strip())
                agent.context.reload_from_branch(argument.strip())
                print(f"checked out branch {res['branch']} @ {res['head_node_id']}")
            continue
        if cmd_lower == "interrupt":
            tid = agent.current_task_id or get_current_session()
            if tid:
                set_abort_flag(tid, source="cli", reason=argument.strip() or "CLI operator interrupt")
                print(f"abort.flag set for {tid}")
            continue
        agent.run_task(user)

    return 0


if __name__ == "__main__":
    sys.exit(main())
