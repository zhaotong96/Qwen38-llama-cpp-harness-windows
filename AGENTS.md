# AGENTS.md

## Execution Rules

* Establish ground truth through read-only inspection before forming conclusions; state conclusions only when supported by reproducible evidence.
* Maintain **single-SubAgent execution**: at most one SubAgent may remain active at any time. Start the next SubAgent only after the current one reaches a terminal state: `completed`, `failed`, or `cancelled`.
* Use the execution tool best suited to the operation:

  * `git bash` for POSIX-style shell operations
  * `python` for programmatic processing and data transformation
  * `pwsh.exe` for PowerShell operations
  * `cmd.exe` for cmd-native operations
  * Windows PowerShell 5.1 only for compatibility requirements
* Prefer repository files, runtime state, logs, metrics, and command output over memory or assumptions.

## Computer Use Agent

Automates Windows through shell, filesystem, keyboard/mouse input, and Chrome DevTools Protocol.

## Architecture Goal

Evolve the single-loop ReAct agent into a reliable long-running agent runtime with:

* unattended multi-step execution
* interruption recovery
* isolated task context
* extensible skills

## Engineering Principles

* Prefer the minimum sufficient mechanism.
* Optimize for maintainability, readability, reliability, high cohesion, and loose coupling.
* Express dependencies explicitly through function signatures and module boundaries.
* Write code and documentation in explicit positive logic: describe the intended component, behavior, state, path, or action directly.
* Extend capabilities through `skills/`; keep core changes within:

  * execution lifecycle
  * context management
  * task state
  * scheduling
  * reliability mechanisms
* Introduce an abstraction when a recurring pattern appears at least three times.
* Keep abstractions concrete, cohesive, and justified by repeated behavior.

## Deployment Topology

Placeholder example (fill in your own environment; this repository ships no real addresses):

| Node | Address | Platform | Role |
| ---- | ------- | -------- | ---- |
| agent host | `<agent-host>` | Windows | harness CLI / HTTP API (`main.py`, `core.api`) |
| model host | `<model-host>` | Linux or Windows | OpenAI-compatible inference server (`/v1`, e.g. llama.cpp) |

### Deployment Invariants

* The inference endpoint is configured through environment variables only: `GEMMA_BASE_URL` (default `http://127.0.0.1:8080/v1`) and `GEMMA_MODEL` (default `local-model`).
* The runtime rules file `.agent/AGENTS.md` is required; the agent fails fast when it is missing. Task state lives under `.agent/tasks/`.
* The HTTP API binds to loopback by default; do not expose it to an untrusted network (skills can execute arbitrary local commands).

