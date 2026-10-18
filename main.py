"""
Local Computer Use Agent - Thin CLI Entry Point & Daemon Client.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
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
