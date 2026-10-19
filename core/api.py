"""
Unified HTTP & SSE Gateway for the Agent Harness.

Exposes the shared core (Singleton Agent, Git-style DAG context tree, unified
file-backed message queue, 3-gate decoding/execution interrupts, isolated
SubAgent sandboxes, Multi-API subscription router, and Zero-Build Web UI) over
standard library HTTP/1.1 + Server-Sent Events (SSE).

Run from the project root:

    py -3 -m core.api [--host 127.0.0.1] [--port 8765] [--goal]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import queue
import sys
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse
from unittest import mock

from core import config as config_mod
from core.agent import Agent
from core.config import load_config
from core import interrupt
from core.logger import setup_logger
from core.models import INBOX, ROOT, WEB_DIR
from core import persistence
from core.persistence import (
    CHECKPOINT_FILE,
    STATUS_FILE,
    TASKS_ROOT,
    checkout_branch,
    create_branch,
    create_tag,
    ensure_current_session,
    generate_task_id,
    get_current_branch,
    get_current_session,
    get_task_tree,
    list_branches,
    list_tags,
    list_tasks,
    load_checkpoint,
    load_status,
    read_stream_events,
    save_status,
    set_current_session,
)
from core import queue_manager
from core import subagent_runner

logger = setup_logger()

INDEX = {
    "name": "gemma4-agent-windows unified web & api harness",
    "description": (
        "Unified HTTP/1.1 REST & SSE gateway sharing one singleton Agent session, "
        "Git-style DAG context tree, explicit file-backed queue, 3-gate interrupt "
        "controller, isolated SubAgent sandbox, and zero-build Web console."
    ),
    "endpoints": [
        "GET    /health",
        "GET    /session",
        "POST   /session                     body: {\"task_id\": \"...\"}",
        "GET    /skills",
        "POST   /skills/<name>               body: skill arguments JSON object",
        "GET    /tasks",
        "GET    /tasks/<id>",
        "POST   /tasks                       body: {\"task\": \"...\"}",
        "POST   /tasks/<id>/resume           body: {\"branch\": \"...\"} (optional)",
        "GET    /tasks/<id>/tree",
        "GET    /tasks/<id>/branches",
        "POST   /tasks/<id>/branches         body: {\"name\": \"...\", \"from_node_id\": \"...\", \"checkout\": true}",
        "POST   /tasks/<id>/checkout         body: {\"branch\": \"...\"}",
        "POST   /tasks/<id>/tags             body: {\"name\": \"...\", \"node_id\": \"...\"}",
        "GET    /tasks/<id>/step_state",
        "POST   /tasks/<id>/interrupt        body: {\"reason\": \"...\", \"mode\": \"reject_execution\"}",
        "POST   /interrupt                   body: {\"task_id\": \"...\", \"reason\": \"...\", \"mode\": \"reject_execution\"}",
        "GET    /queue",
        "POST   /queue                       body: {\"content\": \"...\", \"priority\": 50, \"status\": \"queued\"}",
        "PATCH  /queue/<id>                  body: {\"content\": \"...\", \"priority\": 10, \"status\": \"held|queued\"}",
        "DELETE /queue/<id>",
        "GET    /subagents",
        "GET    /subagents/<sub_id>",
        "POST   /subagents                   body: {\"objective\": \"...\"}",
        "POST   /subagents/<sub_id>/interrupt",
        "GET    /config/providers",
        "PUT    /config/providers            body: providers.json object",
        "GET    /usage",
        "GET    /events                      SSE real-time stream (?task_id=<id>&since_seq=0)",
        "GET    /web/index.html              Zero-build native ES Modules Web UI console",
        "POST   /inbox                       body: {\"message\": \"...\"} (legacy compatible)",
        "POST   /reload                      reload skills/ from disk",
    ],
}


class Harness:
    """Single-flight wrapper around one shared Agent with startup crash self-healing."""

    def __init__(self, goal_mode: bool = False) -> None:
        self.agent = Agent(config=load_config(), goal_mode=goal_mode)
        self.lock = threading.Lock()
        self.active_task: str | None = None
        self.healed_subagents: list[str] = []
        self.recovered_queue_items: int = 0
        self._startup_self_heal()

    def _startup_self_heal(self) -> None:
        """Scan .agent/ on startup to heal orphan SubAgent locks, stuck queue items, and session pointer."""
        with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
            try:
                self.healed_subagents = subagent_runner.heal_orphan_subagent_locks()
            except Exception as e:
                logger.warning(f"[self-heal] subagent lock recovery warning: {e}")

            try:
                self.recovered_queue_items = queue_manager.recover_interrupted_items()
            except Exception as e:
                logger.warning(f"[self-heal] queue recovery warning: {e}")

            try:
                session_id = get_current_session()
                if session_id and load_checkpoint(session_id) is not None:
                    self.agent.current_task_id = session_id
                    self.agent.context.bind_task(
                        session_id,
                        branch=get_current_branch(session_id),
                        sync_initial=True,
                    )
            except Exception as e:
                logger.warning(f"[self-heal] session pointer load warning: {e}")

    @property
    def current_session(self) -> str | None:
        with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
            return self.active_task or self.agent.current_task_id or get_current_session()

    def busy(self) -> bool:
        return self.lock.locked()

    def broadcast_event(self, event_type: str, delta: Any, task_id: str | None = None) -> None:
        target_id = task_id or self.current_session
        self.agent.observer.emit(
            target_id,
            event_type,
            delta,
            tasks_root=TASKS_ROOT,
        )

    def submit(self, task: str, task_id: str | None = None) -> str | None:
        """Run a task in a background thread; ``None`` means the harness is busy."""
        if not self.lock.acquire(blocking=False):
            return None
        with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
            resolved_id = task_id or generate_task_id()
            self.active_task = resolved_id
            try:
                set_current_session(resolved_id)
                save_status(resolved_id, "running")
                threading.Thread(
                    target=self._worker,
                    args=(self.agent.run_task, task, resolved_id),
                    daemon=True,
                ).start()
                return resolved_id
            except Exception:
                self.active_task = None
                self.lock.release()
                raise

    def resume(self, task_id: str, branch: str | None = None) -> bool:
        """Continue a checkpointed task in the background; ``False`` means busy."""
        if not self.lock.acquire(blocking=False):
            return False
        self.active_task = task_id
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                set_current_session(task_id)
            threading.Thread(
                target=self._worker,
                args=(self.agent.resume_task, task_id, branch),
                daemon=True,
            ).start()
            return True
        except Exception:
            self.active_task = None
            self.lock.release()
            raise

    def invoke(self, name: str, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Run one skill through the same validator and dispatcher as the ReAct loop."""
        if not self.lock.acquire(blocking=False):
            return 409, {"error": "agent busy", "active_task": self.active_task}
        try:
            check = self.agent.validator.validate_schema(name, args)
            if not check.is_valid:
                return 400, {"error": check.error_message}
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                result = self.agent.dispatchers[name](args)
            text, image = result if isinstance(result, tuple) and len(result) == 2 else (result, None)
            return 200, {"result": str(text), "image_base64": image}
        except Exception as e:
            return 500, {"error": f"error executing {name}: {e}"}
        finally:
            self.lock.release()

    def reload(self) -> bool:
        """Reload skills from disk; ``False`` means the harness is busy."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self.agent.reload_skills()
            return True
        finally:
            self.lock.release()

    def _worker(self, job: Callable[..., Any], *job_args: Any) -> None:
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                job(*job_args)
        except Exception as e:
            logger.error(f"[api] background job failed: {e}")
            if self.active_task:
                try:
                    with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                        save_status(self.active_task, "blocked")
                except Exception:
                    pass
        finally:
            self.active_task = None
            self.lock.release()


class Handler(BaseHTTPRequestHandler):
    """Route HTTP & SSE requests onto the shared harness."""

    protocol_version = "HTTP/1.1"
    harness: Harness

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("http " + (fmt % args))

    def _parts(self) -> list[str]:
        path = urlparse(self.path).path
        return [unquote(part) for part in path.strip("/").split("/") if part]

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json(400, {"error": f"invalid JSON body: {e}"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return data

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _busy(self) -> None:
        self._json(409, {"error": "agent busy", "active_task": self.harness.active_task})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- GET --------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                self._route_get()
        except Exception as e:
            logger.error(f"[api] GET {self.path} failed: {e}")
            self._json(500, {"error": str(e)})

    def _route_get(self) -> None:
        parts = self._parts()
        if not parts:
            return self._json(200, INDEX)
        if parts == ["health"]:
            return self._json(200, self._health())
        if parts == ["session"]:
            return self._json(200, self._session_info())
        if parts == ["skills"]:
            return self._json(200, {"skills": self._skill_catalog()})
        if parts == ["tasks"]:
            return self._json(200, {"tasks": list_tasks()})
        if len(parts) == 2 and parts[0] == "tasks":
            return self._task_detail(parts[1])
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "tree":
            return self._task_tree(parts[1])
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "branches":
            return self._task_branches(parts[1])
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "step_state":
            return self._task_step_state(parts[1])
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "step_mode":
            return self._task_step_mode(parts[1])
        if parts == ["queue"]:
            return self._json(200, {"queue": queue_manager.list_queue()})
        if len(parts) == 2 and parts[0] == "queue":
            item = queue_manager.get_item(parts[1])
            if item is None:
                return self._json(404, {"error": f"queue item not found: {parts[1]}"})
            return self._json(200, {"item": item})
        if parts == ["subagents"]:
            return self._json(
                200,
                {
                    "active_subagent": subagent_runner.get_active_subagent(),
                    "subagents": subagent_runner.list_subagents(),
                },
            )
        if len(parts) == 2 and parts[0] == "subagents":
            try:
                detail = subagent_runner.get_subagent_detail(parts[1])
            except ValueError:
                detail = None
            if detail is None:
                return self._json(404, {"error": f"unknown subagent: {parts[1]}"})
            return self._json(200, detail)
        if parts == ["config", "providers"]:
            return self._get_providers_config()
        if parts == ["usage"]:
            return self._json(200, config_mod.read_usage_summary())
        if parts == ["events"]:
            return self._handle_sse()
        if parts[0] == "web":
            return self._serve_static_web(parts[1:])
        self._json(404, {"error": "not found"})

    def _health(self) -> dict[str, Any]:
        config = self.harness.agent.config
        session_id = self.harness.current_session
        current_branch = DEFAULT_BRANCH = "main"
        if session_id:
            try:
                current_branch = get_current_branch(session_id)
            except Exception:
                current_branch = "main"
        return {
            "ok": True,
            "model": config.model,
            "base_url": config.base_url,
            "n_ctx": config.n_ctx,
            "goal_mode": self.harness.agent.goal_mode,
            "busy": self.harness.busy(),
            "active_task": self.harness.active_task,
            "current_session": session_id,
            "current_branch": current_branch,
            "active_subagent": subagent_runner.get_active_subagent(),
            "queue_count": len(queue_manager.list_queue()),
        }

    def _session_info(self) -> dict[str, Any]:
        session_id = self.harness.current_session
        branch = "main"
        branches: dict[str, str] = {}
        if session_id:
            try:
                branch = get_current_branch(session_id)
                branches = list_branches(session_id)
            except Exception:
                pass
        return {
            "current_session": session_id,
            "current_branch": branch,
            "branches": branches,
            "busy": self.harness.busy(),
            "active_task": self.harness.active_task,
        }

    def _skill_catalog(self) -> list[dict[str, Any]]:
        """The same tool schemas the internal LLM sees, straight from the registry."""
        return [
            {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "parameters": tool["function"]["parameters"],
            }
            for tool in self.harness.agent.tools
        ]

    def _task_detail(self, task_id: str) -> None:
        try:
            status = load_status(task_id)
            loaded = load_checkpoint(task_id)
        except ValueError:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        if status is None and loaded is None:
            return self._json(404, {"error": f"unknown task: {task_id}"})

        messages, _ = loaded if loaded else ([], {})
        last_assistant = ""
        for message in reversed(messages):
            if message.get("role") == "assistant" and message.get("content"):
                last_assistant = str(message["content"])
                break

        marker = TASKS_ROOT / task_id / CHECKPOINT_FILE
        if not marker.is_file():
            marker = TASKS_ROOT / task_id / STATUS_FILE
        created = (
            datetime.fromtimestamp(marker.stat().st_mtime).isoformat(timespec="seconds")
            if marker.is_file()
            else ""
        )

        self._json(
            200,
            {
                "id": task_id,
                "status": status or "unknown",
                "created": created,
                "message_count": len(messages),
                "last_assistant": last_assistant,
                "current_branch": get_current_branch(task_id),
                "branches": list_branches(task_id),
            },
        )

    def _task_tree(self, task_id: str) -> None:
        try:
            tree = get_task_tree(task_id)
        except ValueError:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        if tree is None:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        self._json(200, tree)

    def _task_branches(self, task_id: str) -> None:
        try:
            status = load_status(task_id)
            loaded = load_checkpoint(task_id)
        except ValueError:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        if status is None and loaded is None:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        self._json(
            200,
            {
                "task_id": task_id,
                "current_branch": get_current_branch(task_id),
                "branches": list_branches(task_id),
                "tags": list_tags(task_id),
            },
        )

    def _task_step_state(self, task_id: str) -> None:
        try:
            persistence._validate_identifier(task_id, "task id")
        except ValueError:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        self._json(
            200,
            {
                "task_id": task_id,
                "step_state": interrupt.read_step_state(task_id),
                "abort_flag": interrupt.check_abort_flag(task_id),
                "step_mode": interrupt.get_step_mode(task_id),
            },
        )

    def _task_step_mode(self, task_id: str) -> None:
        try:
            persistence._validate_identifier(task_id, "task id")
        except ValueError:
            return self._json(404, {"error": f"unknown task: {task_id}"})
        self._json(
            200,
            {
                "task_id": task_id,
                "step_mode": interrupt.get_step_mode(task_id),
                "step_state": interrupt.read_step_state(task_id),
            },
        )

    def _get_providers_config(self) -> None:
        raw = config_mod.load_providers_file() or config_mod.DEFAULT_PROVIDERS_TEMPLATE
        role_configs = config_mod.load_all_role_configs()
        resolved = {
            role: {
                "profile_name": cfg.profile_name,
                "base_url": cfg.base_url,
                "model": cfg.model,
                "n_ctx": cfg.n_ctx,
                "max_gen_tokens": cfg.max_gen_tokens,
                "compact_trigger": cfg.compact_trigger,
                "compact_target": cfg.compact_target,
                "price_per_1m_input": cfg.price_per_1m_input,
                "price_per_1m_output": cfg.price_per_1m_output,
            }
            for role, cfg in role_configs.items()
        }
        self._json(200, {"providers": raw, "resolved_roles": resolved})

    def _serve_static_web(self, rel_parts: list[str]) -> None:
        if not rel_parts:
            rel_parts = ["index.html"]
        rel_path = Path(*rel_parts)
        if ".." in rel_path.parts:
            return self._json(400, {"error": "invalid path"})
        target = (WEB_DIR / rel_path).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._json(404, {"error": f"static file not found: {'/'.join(rel_parts)}"})

        content_type, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        else:
            content_type = content_type or "application/octet-stream"

        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_sse(self) -> None:
        q = self._query()
        task_id = (q.get("task_id") or [None])[0] or self.harness.current_session
        try:
            since_seq = int((q.get("since_seq") or ["0"])[0])
        except ValueError:
            since_seq = 0
        once = (q.get("once") or ["0"])[0] in ("1", "true", "yes")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close" if once else "keep-alive")
        self.close_connection = True
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def send_sse_event(evt_name: str, data_obj: Any, evt_id: int | str | None = None) -> bool:
            try:
                lines = []
                if evt_id is not None:
                    lines.append(f"id: {evt_id}")
                lines.append(f"event: {evt_name}")
                lines.append(f"data: {json.dumps(data_obj, ensure_ascii=False, default=str)}")
                lines.append("")
                payload = ("\n".join(lines) + "\n").encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()
                return True
            except Exception:
                return False

        # Replay persisted events from live_stream.jsonl
        if task_id:
            try:
                backlog = read_stream_events(task_id, since_seq=since_seq)
                for evt in backlog:
                    if not send_sse_event(evt.get("event_type", "message"), evt, evt_id=evt.get("seq")):
                        return
            except Exception:
                pass

        # Send initial state snapshot
        send_sse_event("snapshot", self._health())
        if once:
            return

        live_queue: queue.Queue = queue.Queue()

        def on_event(evt: dict[str, Any]) -> None:
            live_queue.put(evt)

        self.harness.agent.observer.add_listener(on_event)
        try:
            while True:
                try:
                    evt = live_queue.get(timeout=5.0)
                    if not send_sse_event(evt.get("event_type", "message"), evt, evt_id=evt.get("seq")):
                        break
                except queue.Empty:
                    if not send_sse_event("ping", {"ts": datetime.now().isoformat(timespec="seconds")}):
                        break
        finally:
            self.harness.agent.observer.remove_listener(on_event)

    # --- POST / PUT / PATCH / DELETE --------------------------------------

    def do_POST(self) -> None:
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                self._route_post()
        except Exception as e:
            logger.error(f"[api] POST {self.path} failed: {e}")
            self._json(500, {"error": str(e)})

    def do_PUT(self) -> None:
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                self._route_put_or_patch()
        except Exception as e:
            logger.error(f"[api] PUT {self.path} failed: {e}")
            self._json(500, {"error": str(e)})

    def do_PATCH(self) -> None:
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                self._route_put_or_patch()
        except Exception as e:
            logger.error(f"[api] PATCH {self.path} failed: {e}")
            self._json(500, {"error": str(e)})

    def do_DELETE(self) -> None:
        try:
            with mock.patch.object(persistence, "TASKS_ROOT", TASKS_ROOT):
                self._route_delete()
        except Exception as e:
            logger.error(f"[api] DELETE {self.path} failed: {e}")
            self._json(500, {"error": str(e)})

    def _route_post(self) -> None:
        parts = self._parts()
        body = self._body()
        if body is None:
            return

        if parts == ["session"]:
            task_id = body.get("task_id")
            if task_id:
                try:
                    set_current_session(str(task_id))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            else:
                task_id = ensure_current_session()
            if not self.harness.busy() and load_checkpoint(str(task_id)) is not None:
                self.harness.agent.current_task_id = str(task_id)
                self.harness.agent.context.bind_task(str(task_id), sync_initial=True)
            self.harness.broadcast_event("session_changed", {"current_session": str(task_id)}, task_id=str(task_id))
            return self._json(200, self._session_info())

        if parts == ["tasks"]:
            task = body.get("task")
            if not isinstance(task, str) or not task.strip():
                return self._json(400, {"error": "body must contain a non-empty string 'task'"})
            pinned_id = body.get("task_id")
            task_id = self.harness.submit(task.strip(), task_id=str(pinned_id) if pinned_id else None)
            if task_id is None:
                if body.get("enqueue_if_busy"):
                    item = queue_manager.enqueue(
                        content=task.strip(),
                        priority=int(body.get("priority", 50)),
                        source=str(body.get("source", "api")),
                        author=str(body.get("author", "operator")),
                    )
                    self.harness.broadcast_event("queue_updated", {"action": "enqueue", "item": item})
                    return self._json(202, {"queued": True, "item": item, "active_task": self.harness.active_task})
                return self._busy()
            return self._json(202, {"task_id": task_id, "status": "running"})

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "resume":
            task_id = parts[1]
            try:
                known = load_checkpoint(task_id) is not None
            except ValueError:
                known = False
            if not known:
                return self._json(404, {"error": f"no checkpoint for task: {task_id}"})
            branch = body.get("branch")
            if not self.harness.resume(task_id, branch=str(branch) if branch else None):
                return self._busy()
            return self._json(202, {"task_id": task_id, "status": "running"})

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "branches":
            task_id = parts[1]
            name = body.get("name") or body.get("branch")
            if not isinstance(name, str) or not name.strip():
                return self._json(400, {"error": "body must contain branch 'name'"})
            from_node_id = body.get("from_node_id")
            checkout = bool(body.get("checkout", True))
            force_at_assistant = bool(body.get("force_at_assistant", False))
            try:
                res = create_branch(
                    task_id,
                    branch_name=name.strip(),
                    from_node_id=str(from_node_id) if from_node_id else None,
                    checkout=checkout,
                    force_at_assistant=force_at_assistant,
                )
            except ValueError as e:
                msg = str(e)
                return self._json(404 if "empty or missing" in msg or "unknown" in msg else 400, {"error": msg})

            if checkout and self.harness.agent.current_task_id == task_id:
                self.harness.agent.context.reload_from_branch(name.strip())
            self.harness.broadcast_event("branch_created", res, task_id=task_id)
            return self._json(201, res)

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "checkout":
            task_id = parts[1]
            branch = body.get("branch") or body.get("name")
            if not isinstance(branch, str) or not branch.strip():
                return self._json(400, {"error": "body must contain 'branch'"})
            try:
                res = checkout_branch(task_id, branch.strip())
            except ValueError as e:
                return self._json(404, {"error": str(e)})
            if self.harness.agent.current_task_id == task_id:
                self.harness.agent.context.reload_from_branch(branch.strip())
            self.harness.broadcast_event(
                "branch_checked_out",
                {"task_id": task_id, "branch": branch.strip(), "head_node_id": res["head_node_id"]},
                task_id=task_id,
            )
            return self._json(200, res)

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "tags":
            task_id = parts[1]
            name = body.get("name") or body.get("tag")
            if not isinstance(name, str) or not name.strip():
                return self._json(400, {"error": "body must contain tag 'name'"})
            node_id = body.get("node_id")
            try:
                res = create_tag(task_id, name.strip(), node_id=str(node_id) if node_id else None)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self.harness.broadcast_event("tag_created", res, task_id=task_id)
            return self._json(201, res)

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "step_mode":
            task_id = parts[1]
            mode = body.get("mode") or body.get("step_mode")
            if not isinstance(mode, str) or not mode.strip():
                return self._json(400, {"error": "body must contain 'mode'"})
            try:
                resolved = interrupt.set_step_mode(task_id, mode.strip())
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self.harness.broadcast_event(
                "step_mode_changed",
                {"task_id": task_id, "step_mode": resolved},
                task_id=task_id,
            )
            return self._json(200, {"ok": True, "task_id": task_id, "step_mode": resolved})

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "step_continue":
            task_id = parts[1]
            try:
                persistence._validate_identifier(task_id, "task id")
                path = interrupt.write_step_continue(task_id)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self.harness.broadcast_event(
                "step_continue_set",
                {"task_id": task_id, "flag": path.name},
                task_id=task_id,
            )
            return self._json(200, {"ok": True, "task_id": task_id, "flag": path.name})

        if parts == ["interrupt"] or (len(parts) == 3 and parts[0] == "tasks" and parts[2] == "interrupt"):
            target_id = (
                parts[1]
                if len(parts) == 3
                else (body.get("task_id") or self.harness.active_task or self.harness.current_session)
            )
            if not target_id:
                return self._json(400, {"error": "no active or specified task_id to interrupt"})
            try:
                flag_data = interrupt.set_abort_flag(
                    str(target_id),
                    source=str(body.get("source", "api")),
                    reason=str(body.get("reason", "Operator interrupted")),
                    mode=str(body.get("mode", "reject_execution")),
                    halt_loop=bool(body.get("halt_loop", False)),
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})

            # Cascade abort to any active SubAgent
            active_sub = subagent_runner.get_active_subagent()
            if active_sub:
                subagent_runner.abort_subagent(
                    active_sub,
                    source=str(body.get("source", "api")),
                    reason=f"Cascaded from main task {target_id}: {body.get('reason', 'Operator interrupted')}",
                )

            self.harness.broadcast_event("interrupt_set", flag_data, task_id=str(target_id))
            return self._json(200, {"ok": True, "task_id": str(target_id), "abort": flag_data})

        if parts == ["queue"]:
            content = body.get("content") or body.get("message")
            if not isinstance(content, str) or not content.strip():
                return self._json(400, {"error": "body must contain a non-empty string 'content'"})
            try:
                item = queue_manager.enqueue(
                    content=content.strip(),
                    priority=int(body.get("priority", 50)),
                    source=str(body.get("source", "api")),
                    author=str(body.get("author", "operator")),
                    mode=str(body.get("mode", "steer_or_next_turn")),
                    target_branch=str(body.get("target_branch", "main")),
                    status=str(body.get("status", "queued")),
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self.harness.broadcast_event("queue_updated", {"action": "enqueue", "item": item})
            return self._json(201, {"ok": True, "item": item})

        if len(parts) == 2 and parts[0] == "queue":
            return self._modify_queue_item(parts[1], body)

        if parts == ["subagents"]:
            objective = body.get("objective")
            if not isinstance(objective, str) or not objective.strip():
                return self._json(400, {"error": "body must contain a non-empty string 'objective'"})
            raw_res = subagent_runner.run_subagent(
                body,
                runtime={"parent_task_id": self.harness.active_task or self.harness.current_session},
            )
            if raw_res.startswith("error: SUBAGENT_BUSY:"):
                return self._json(409, {"error": raw_res, "active_subagent": subagent_runner.get_active_subagent()})
            try:
                parsed = json.loads(raw_res)
                return self._json(200, parsed)
            except Exception:
                return self._json(200, {"result": raw_res})

        if len(parts) == 3 and parts[0] == "subagents" and parts[2] == "interrupt":
            sub_id = parts[1]
            info = subagent_runner.abort_subagent(
                sub_id,
                source=str(body.get("source", "api")),
                reason=str(body.get("reason", "SubAgent interrupted")),
            )
            if info is None:
                return self._json(404, {"error": f"unknown subagent: {sub_id}"})
            return self._json(200, {"ok": True, "subagent_id": sub_id, "abort": info})

        if parts == ["config", "providers"]:
            return self._update_providers_config(body)

        if len(parts) == 2 and parts[0] == "skills":
            name = parts[1]
            if name not in self.harness.agent.dispatchers:
                return self._json(404, {"error": f"unknown skill: {name}"})
            code, payload = self.harness.invoke(name, body)
            return self._json(code, payload)

        if parts == ["inbox"]:
            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                return self._json(400, {"error": "body must contain a non-empty string 'message'"})
            INBOX.mkdir(parents=True, exist_ok=True)
            note = INBOX / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
            note.write_text(json.dumps({"message": message}, ensure_ascii=False), encoding="utf-8")
            return self._json(202, {"ok": True, "file": note.name})

        if parts == ["reload"]:
            if not self.harness.reload():
                return self._busy()
            return self._json(200, {"ok": True, "skills": len(self.harness.agent.tools)})

        self._json(404, {"error": "not found"})

    def _route_put_or_patch(self) -> None:
        parts = self._parts()
        body = self._body()
        if body is None:
            return
        if len(parts) == 2 and parts[0] == "queue":
            return self._modify_queue_item(parts[1], body)
        if parts == ["config", "providers"]:
            return self._update_providers_config(body)
        self._json(404, {"error": "not found"})

    def _route_delete(self) -> None:
        parts = self._parts()
        if len(parts) == 2 and parts[0] == "queue":
            cancelled = queue_manager.cancel_item(parts[1], by="api")
            if cancelled is None:
                return self._json(404, {"error": f"queue item not found: {parts[1]}"})
            self.harness.broadcast_event("queue_updated", {"action": "cancel", "item": cancelled})
            return self._json(200, {"ok": True, "item": cancelled})
        self._json(404, {"error": "not found"})

    def _modify_queue_item(self, item_id: str, body: dict[str, Any]) -> None:
        try:
            updated = queue_manager.modify_item(
                item_id,
                content=body.get("content"),
                priority=int(body["priority"]) if "priority" in body and body["priority"] is not None else None,
                status=body.get("status"),
                mode=body.get("mode"),
                target_branch=body.get("target_branch"),
                by=str(body.get("by", "api")),
            )
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if updated is None:
            return self._json(404, {"error": f"queue item not found: {item_id}"})
        self.harness.broadcast_event("queue_updated", {"action": "modify", "item": updated})
        self._json(200, {"ok": True, "item": updated})

    def _update_providers_config(self, body: dict[str, Any]) -> None:
        payload = body.get("providers") if isinstance(body.get("providers"), dict) else body
        try:
            config_mod.save_providers_file(payload)
            configs = self.harness.agent.reload_providers()
        except Exception as e:
            return self._json(400, {"error": f"failed to update providers: {e}"})
        resolved = {
            role: {"profile_name": cfg.profile_name, "base_url": cfg.base_url, "model": cfg.model, "n_ctx": cfg.n_ctx}
            for role, cfg in configs.items()
        }
        self.harness.broadcast_event("providers_updated", resolved)
        self._json(200, {"ok": True, "providers": payload, "resolved_roles": resolved})


def build_server(host: str, port: int, harness: Harness) -> ThreadingHTTPServer:
    """Create the HTTP server bound to one harness instance."""
    handler_class = type("HarnessHandler", (Handler,), {"harness": harness})
    server = ThreadingHTTPServer((host, port), handler_class)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, "reconfigure") and getattr(_stream, "encoding", "").lower() != "utf-8":
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="core.api",
        description="Unified Web & HTTP API front end for the agent harness.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    parser.add_argument("--goal", action="store_true", help="run tasks in goal mode (same as main.py --goal)")
    args = parser.parse_args(argv)

    harness = Harness(goal_mode=args.goal)
    server = build_server(args.host, args.port, harness)

    logger.info("=" * 56)
    logger.info(f"api    http://{args.host}:{args.port}")
    logger.info(f"web    http://{args.host}:{args.port}/web/index.html")
    logger.info(f"model  {harness.agent.config.model}")
    logger.info(f"cwd    {ROOT}")
    logger.info("skills " + " ".join(t["function"]["name"] for t in harness.agent.tools))
    logger.info("state  .agent/{current_session,tasks,queue,subagents,providers.json}")
    logger.info("=" * 56)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())