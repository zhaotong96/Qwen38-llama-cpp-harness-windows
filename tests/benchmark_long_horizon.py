"""Phase 7 P2 long-horizon benchmark: 66 scripted steps and 3 cascaded compactions.

Default mode replays a deterministic state machine with no network access and
asserts GCR == 100%, FFR == 100%, and SCCS >= 0.8 turn^-1. ``--live`` drives a
real OpenAI-compatible endpoint with the same workspace fixtures and reports the
same three metrics.

Run from the project root:

    python tests/benchmark_long_horizon.py
    python tests/benchmark_long_horizon.py --live
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import agent as agent_mod
from core import persistence
from core import queue_manager
from core.agent import Agent
from core.config import AgentConfig
from tests.support import _chunks_for


STEP_COUNT = 66
SEGMENT_ROUNDS = 11
COMPACTION_AT = (22, 44, 55)
MALFORMED_STEPS = (12, 31, 47)

FACT_PORT = "BENCH_SECRET_PORT=49152"
FACT_TOKEN = "API_SIGNATURE_TOKEN=sig_v7_99a8b7"
REDLINE_1 = "[Operator correction] NEVER touch the production slot; keep BENCH_SECRET_PORT=49152"
REDLINE_2 = (
    "[Operator correction] Do not restart the remote inference service; "
    "preserve API_SIGNATURE_TOKEN=sig_v7_99a8b7"
)
ACK_1 = "[Operator correction] Acknowledged; continue the audit."
ACK_2 = "[Operator correction] Acknowledged; continue the refactor."

INJECT_AT = {8: REDLINE_1, 13: ACK_1, 28: REDLINE_2, 35: ACK_2}

APP_V2 = "def main():\n    return 'v2'\n# BENCH_SECRET_PORT=49152\n"
APP_V3 = "def main():\n    return 'v3'\n# token=sig_v7_99a8b7\n"
APP_V4 = "def main():\n    return 'v4'\n# refactor stage 4\n"
APP_V5 = "def main():\n    return 'v5'\n# stable\n"
APP_FINAL = (
    "def main():\n    return 'final'\n"
    "# BENCH_SECRET_PORT=49152 API_SIGNATURE_TOKEN=sig_v7_99a8b7\n"
)
NOTES_V2 = "# notes v2\n- probe done\n"
NOTES_V3 = "# notes v3\n- refactor in progress\n- keep cache-reuse=0\n"


def _report(stage: str) -> str:
    return (
        "# Audit\n"
        f"{FACT_PORT}\n"
        f"{FACT_TOKEN}\n"
        f"stage={stage}\n"
    )


FINAL_REPORT = (
    "# Audit\n"
    f"{FACT_PORT}\n"
    f"{FACT_TOKEN}\n"
    "stage=final\n"
    "refactor=complete\n"
    "FINAL_SIGNATURE=sig_v7_99a8b7\n"
)

PROMPT = (
    "Run a long-horizon workspace audit and refactor in the provided directory. "
    "Inspect config.txt, app.py, and notes.md; create and repeatedly update report.md; "
    "refactor app.py in staged write_file calls and verify each stage with read_file. "
    f"report.md must always contain {FACT_PORT} and {FACT_TOKEN}. "
    "If a tool call fails, diagnose the error and retry with corrected arguments. "
    "Finish by replying with TASK_DONE and a short summary."
)


def _workspace_script(ws: str) -> list[tuple]:
    app = str(Path(ws) / "app.py")
    notes = str(Path(ws) / "notes.md")
    config = str(Path(ws) / "config.txt")
    report = str(Path(ws) / "report.md")
    return [
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": config}),
        ("tool", "read_file", {"path": app}),
        ("tool", "read_file", {"path": notes}),
        ("tool", "write_file", {"path": report, "content": _report("1")}),
        ("tool", "read_file", {"path": report}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": app}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": config}),
        ("tool", "write_file", {"path": app, "content": APP_V2}),
        ("bad", "read_file", '{"path": "app.py",,}'),
        ("tool", "read_file", {"path": app}),
        ("tool", "write_file", {"path": notes, "content": NOTES_V2}),
        ("tool", "read_file", {"path": notes}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "write_file", {"path": report, "content": _report("2") + "refactor=v2\n"}),
        ("tool", "read_file", {"path": report}),
        ("tool", "read_file", {"path": app}),
        ("tool", "write_file", {"path": app, "content": APP_V3}),
        ("tool", "read_file", {"path": app}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": report}),
        ("tool", "read_file", {"path": config}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "write_file", {"path": report, "content": _report("3") + "refactor=v3\n"}),
        ("tool", "read_file", {"path": report}),
        ("tool", "read_file", {"path": app}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": notes}),
        ("bad", "write_file", '{"path": "report.md", "content": }'),
        ("tool", "read_file", {"path": report}),
        ("tool", "write_file", {"path": app, "content": APP_V4}),
        ("tool", "read_file", {"path": app}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "write_file", {"path": notes, "content": NOTES_V3}),
        ("tool", "read_file", {"path": notes}),
        ("tool", "read_file", {"path": config}),
        ("tool", "write_file", {"path": report, "content": _report("4") + "refactor=v4\n"}),
        ("tool", "read_file", {"path": report}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": app}),
        ("tool", "write_file", {"path": app, "content": APP_V5}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": app}),
        ("tool", "read_file", {"path": report}),
        ("bad", "list_dir", '{"path": "." "recursive": true}'),
        ("tool", "read_file", {"path": notes}),
        ("tool", "write_file", {"path": report, "content": _report("5") + "refactor=v5\n"}),
        ("tool", "read_file", {"path": report}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": config}),
        ("tool", "write_file", {"path": app, "content": APP_FINAL}),
        ("tool", "read_file", {"path": app}),
        ("tool", "read_file", {"path": notes}),
        ("tool", "write_file", {"path": report, "content": FINAL_REPORT}),
        ("tool", "read_file", {"path": report}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": app}),
        ("tool", "read_file", {"path": config}),
        ("tool", "list_dir", {"path": ws}),
        ("tool", "read_file", {"path": report}),
        ("tool", "read_file", {"path": app}),
        ("tool", "write_file", {"path": report, "content": FINAL_REPORT}),
        ("tool", "read_file", {"path": report}),
        ("text", f"TASK_DONE: refactor complete; {FACT_PORT}; {FACT_TOKEN}"),
    ]


class ScenarioLLM:
    """Scripted long-horizon planner plus a deliberately lossy ledger compactor."""

    def __init__(self, script: list[tuple], inject_at: dict[int, str] | None = None) -> None:
        self.script = script
        self.inject_at = dict(inject_at or {})
        self.step = 0
        self.requests: list[dict] = []
        self.chat = self
        self.completions = self

    def _ledger(self) -> str:
        return (
            "1. 核心目标与进度\n"
            f"长程审计与重构推进至第 {self.step} 步。\n\n"
            "2. 已验证事实与环境基线\n"
            f"{FACT_PORT}\n{FACT_TOKEN}\n\n"
            "3. 已修改文件清单\n"
            "workspace/app.py, workspace/notes.md, workspace/report.md\n\n"
            "4. 被否决方案与操作员红线（永久保留）\n"
            "无\n\n"
            "5. 待完成子目标与下一步行动\n"
            "继续重构并写最终报告。"
        )

    def _materialize(self, action: tuple) -> tuple[str, list[dict]]:
        kind = action[0]
        if kind == "text":
            return str(action[1]), []
        name = action[1]
        if kind == "bad":
            arguments = str(action[2])
        else:
            arguments = json.dumps(action[2], ensure_ascii=False)
        return "", [{"id": f"call_{self.step:03d}", "name": name, "arguments": arguments}]

    def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.requests.append(snapshot)

        if not kwargs.get("stream"):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=self._ledger(),
                            reasoning_content=None,
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ]
            )

        self.step += 1
        if self.step in self.inject_at:
            queue_manager.enqueue(
                self.inject_at[self.step],
                source="benchmark",
                author="operator",
                target_branch=persistence.DEFAULT_BRANCH,
            )
        if self.step > len(self.script):
            return _chunks_for("TASK_DONE: script exhausted", [])
        content, tool_calls = self._materialize(self.script[self.step - 1])
        return _chunks_for(content, tool_calls)


def _write_fixtures(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "config.txt").write_text(
        f"mode=benchmark\n{FACT_PORT}\n{FACT_TOKEN}\n", encoding="utf-8"
    )
    (ws / "app.py").write_text("def main():\n    return 'v1'\n", encoding="utf-8")
    (ws / "notes.md").write_text("# notes v1\n- initial probe\n", encoding="utf-8")


def _null_logger() -> logging.Logger:
    logger = logging.getLogger("benchmark_long_horizon")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def _scan_originals(nodes: list[dict]) -> list[dict]:
    originals: list[dict] = []
    for node in nodes:
        if node.get("type") == "compaction":
            continue
        meta = node.get("meta") or {}
        if meta.get("reparented_from"):
            continue
        originals.append(node)
    return originals


def _valid_call(call: dict) -> bool:
    function = call.get("function") or {}
    try:
        json.loads(function.get("arguments") or "{}")
    except Exception:
        return False
    return True


def evaluate(task_id: str, final_text: str, ws: Path) -> dict:
    loaded = persistence.load_dag_nodes(task_id)
    nodes, _ = loaded if loaded else ([], {})
    compactions = [n for n in nodes if n.get("type") == "compaction"]
    latest = compactions[-1] if compactions else None
    content = str(latest.get("content", "")) if latest else ""
    section2 = Agent._ledger_section(content, 2)
    section4 = Agent._ledger_section(content, 4)

    ffr_checks = {
        "redline_slot": REDLINE_1 in section4,
        "redline_service": REDLINE_2 in section4,
        "fact_port": FACT_PORT in section2,
        "fact_token": FACT_TOKEN in section2,
    }

    report = ws / "report.md"
    report_text = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else ""
    gcr_checks = {
        "task_done": "TASK_DONE" in final_text,
        "report_exists": report.is_file(),
        "final_signature": "FINAL_SIGNATURE=sig_v7_99a8b7" in report_text,
    }

    originals = _scan_originals(nodes)
    assistants = [n for n in originals if n.get("role") == "assistant"]
    failures = 0
    recovered = 0
    recovery_turns = 0
    for index, node in enumerate(assistants):
        calls = node.get("tool_calls") or []
        if not calls or all(_valid_call(call) for call in calls):
            continue
        failures += 1
        found: int | None = None
        for distance, candidate in enumerate(assistants[index + 1:index + 4], start=1):
            next_calls = candidate.get("tool_calls") or []
            if next_calls and all(_valid_call(call) for call in next_calls):
                found = distance
                break
            if not next_calls and str(candidate.get("content", "")).strip():
                found = distance
                break
        if found is None:
            recovery_turns += 3
        else:
            recovered += 1
            recovery_turns += found

    sixth = content.split("6. 已归档原始 DAG 节点索引表", 1)
    index_lines = len(sixth[1].strip().splitlines()) if len(sixth) == 2 else 0

    gcr = 100.0 * sum(gcr_checks.values()) / len(gcr_checks)
    ffr = 100.0 * sum(ffr_checks.values()) / len(ffr_checks)
    sccs = recovered / recovery_turns if recovery_turns else 0.0
    return {
        "task_id": task_id,
        "steps": len(assistants),
        "compactions": len(compactions),
        "failures": failures,
        "recovered": recovered,
        "recovery_turns": recovery_turns,
        "gcr": gcr,
        "ffr": ffr,
        "sccs": sccs,
        "index_lines": index_lines,
        "gcr_checks": gcr_checks,
        "ffr_checks": ffr_checks,
    }


def _print_report(mode: str, metrics: dict) -> None:
    print(f"Long-horizon benchmark ({mode})")
    print(
        f"steps={metrics['steps']} compactions={metrics['compactions']} "
        f"malformed_json={metrics['failures']} index_lines={metrics['index_lines']}"
    )
    print(f"GCR={metrics['gcr']:.1f}% ({sum(metrics['gcr_checks'].values())}/{len(metrics['gcr_checks'])})")
    print(f"FFR={metrics['ffr']:.1f}% ({sum(metrics['ffr_checks'].values())}/{len(metrics['ffr_checks'])})")
    print(
        f"SCCS={metrics['sccs']:.3f} turn^-1 "
        f"({metrics['recovered']}/{metrics['recovery_turns']} recovery turns)"
    )
    for name, ok in metrics["gcr_checks"].items():
        print(f"  GCR {name}: {'PASS' if ok else 'FAIL'}")
    for name, ok in metrics["ffr_checks"].items():
        print(f"  FFR {name}: {'PASS' if ok else 'FAIL'}")


def _assert_metrics(metrics: dict) -> list[str]:
    problems: list[str] = []
    if metrics["gcr"] != 100.0:
        problems.append(f"GCR={metrics['gcr']:.1f}% (expected 100%)")
    if metrics["ffr"] != 100.0:
        problems.append(f"FFR={metrics['ffr']:.1f}% (expected 100%)")
    if metrics["sccs"] < 0.8:
        problems.append(f"SCCS={metrics['sccs']:.3f} (expected >= 0.8)")
    if metrics["steps"] < 60:
        problems.append(f"steps={metrics['steps']} (expected >= 60)")
    if metrics["compactions"] < 3:
        problems.append(f"compactions={metrics['compactions']} (expected >= 3)")
    if metrics["index_lines"] > 30:
        problems.append(f"index_lines={metrics['index_lines']} (expected <= 30)")
    return problems


def run_deterministic() -> dict:
    with tempfile.TemporaryDirectory(prefix="bench_long_horizon_") as tmp:
        state = Path(tmp)
        tasks_root = state / "tasks"
        ws = state / "workspace"
        _write_fixtures(ws)

        script = _workspace_script(str(ws))
        if len(script) != STEP_COUNT:
            raise RuntimeError(f"script length {len(script)} != {STEP_COUNT}")
        llm = ScenarioLLM(script, inject_at=INJECT_AT)
        config = AgentConfig(max_rounds=SEGMENT_ROUNDS, log_level="ERROR")
        task_id = "bench_long_horizon"

        patches = [
            mock.patch.object(persistence, "TASKS_ROOT", tasks_root),
            mock.patch.object(agent_mod, "INBOX", state / "inbox"),
            mock.patch.object(agent_mod, "INBOX_PROCESSED", state / "inbox_processed"),
            mock.patch.object(agent_mod, "setup_logger", lambda **kwargs: _null_logger()),
            mock.patch.object(agent_mod, "OpenAI", lambda *args, **kwargs: llm),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            agent = Agent(config=config, stream_to_stdout=False)
            agent.compactor_config = agent.config
            agent.compactor_client = llm

            agent.current_task_id = task_id
            agent.context.bind_task(task_id, sync_initial=True)
            agent.context.append({"role": "user", "content": PROMPT})
            persistence.save_status(task_id, "running")

            final_text = ""
            completed = 0
            for _ in range(STEP_COUNT // SEGMENT_ROUNDS):
                final_text = agent.step_loop()
                completed += SEGMENT_ROUNDS
                if completed in COMPACTION_AT:
                    compacted = agent.context.compact(
                        agent._summarize,
                        token_limit=0,
                        keep_recent=6,
                    )
                    if not compacted:
                        raise RuntimeError(f"compaction failed after step {completed}")

            persistence.save_checkpoint(task_id, agent.context.as_list())
            persistence.save_status(task_id, "done" if "TASK_DONE" in final_text else "finished")
            return evaluate(task_id, final_text, ws)


def _live_watcher(task_id: str, tasks_root: Path, stop: threading.Event) -> None:
    sent = False
    while not stop.is_set():
        try:
            raw = (tasks_root / task_id / "step_state").read_text(encoding="utf-8")
            state = json.loads(raw)
            if not sent and int(state.get("round", 0)) >= 28:
                queue_manager.enqueue(REDLINE_2, source="benchmark", author="operator")
                sent = True
        except Exception:
            pass
        time.sleep(0.2)


def run_live() -> dict:
    with tempfile.TemporaryDirectory(prefix="bench_long_horizon_live_") as tmp:
        state = Path(tmp)
        tasks_root = state / "tasks"
        ws = state / "workspace"
        _write_fixtures(ws)
        config = AgentConfig(max_rounds=80, log_level="INFO")

        patches = [
            mock.patch.object(persistence, "TASKS_ROOT", tasks_root),
            mock.patch.object(agent_mod, "INBOX", state / "inbox"),
            mock.patch.object(agent_mod, "INBOX_PROCESSED", state / "inbox_processed"),
        ]
        with patches[0], patches[1], patches[2]:
            agent = Agent(config=config, stream_to_stdout=True)
            task_id = persistence.generate_task_id()
            queue_manager.enqueue(
                REDLINE_1,
                source="benchmark",
                author="operator",
                target_branch=persistence.DEFAULT_BRANCH,
            )
            stop = threading.Event()
            watcher = threading.Thread(
                target=_live_watcher,
                args=(task_id, tasks_root, stop),
                daemon=True,
            )
            watcher.start()
            try:
                prompt = f"{PROMPT}\nWorkspace: {ws}"
                final_text = agent.run_task(prompt, task_id=task_id)
            finally:
                stop.set()
                watcher.join(timeout=3)
            return evaluate(task_id, final_text, ws)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="drive a real OpenAI-compatible endpoint")
    parser.add_argument("--no-assert", action="store_true", help="report metrics without failing the process")
    args = parser.parse_args(argv)

    metrics = run_live() if args.live else run_deterministic()
    _print_report("live" if args.live else "deterministic", metrics)

    problems = _assert_metrics(metrics)
    if problems and not args.no_assert:
        print("\nFAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    if problems:
        print("\nWARNINGS (assertions disabled):")
        for problem in problems:
            print(f"  - {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
