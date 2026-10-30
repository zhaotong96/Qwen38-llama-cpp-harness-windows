# SPEC.md — Qwen38-llama-cpp-harness-windows

本文件是本仓库的持久化规格说明（Persistent Spec），描述系统的**目标、设计原则、契约与状态模型**。
所有代码改动、Skill 扩展、以及外部 AI 助手（codex / claude 等）对本 harness 的调用，都以本文件为准。

- 状态：生效中（§10 为已决事项记录）
- 权威来源：本文件 + 磁盘上的文件（`.agent/`）；代码与本文档冲突时，先修正其一再继续。

---

## 1. 定位

本地 Windows 计算机操作 Agent（ReAct 单循环）。它把「本机操作能力」暴露为三样东西：

1. **技能（skills/）**：一切能力都是 `skills/*.py` 文件，动态注册。
2. **CLI（main.py）**：单人交互 / 一次性任务 / 断点续跑。
3. **HTTP API（core/api.py）**：给外部 AI 助手（codex、claude 等）驱动本机使用。

**核心诉求：CLI 与 HTTP API 不是两套逻辑，而是同一个核心（`core/`）的两个薄前端。**
两者共享同一份配置、同一个 Agent、同一批 Skill 分发器、同一组磁盘状态文件。

---

## 2. 设计原则（不可动摇）

| 原则 | 含义 | 落地证据 |
| --- | --- | --- |
| 单一核心，多前端 | 业务逻辑只在 `core/`；`main.py`、`core/api.py` 只做传输层 | 两端都调用 `Agent.run_task / resume_task / reload_skills`、`core.persistence`、`core.config.load_config` |
| 万物皆 skill | 能力 = `skills/` 下的自描述文件；核心不含任何具体能力 | `discover_skills()` 按文件名加载，`DESCRIPTION` + `PARAMETERS` + `HANDLER/run` 即契约；HTTP API 的 skill 目录同样直接来自注册表 |
| 万物皆文件 | 状态不藏在内存或数据库；文件即接口 | 任务=`.agent/tasks/<id>/`，消息总线=`.agent/inbox/*.json`，日志=`logs/agent.log` |
| 显式文件标记 | 用可读文本标记表达生命周期，而非隐式推断 | `.agent/tasks/<id>/status` 内容为 `running|done|blocked|finished` |
| KISS / 奥卡姆 | 最小充分机制；不引入框架与依赖 | HTTP 服务仅用标准库 `http.server`；不引入队列、数据库、鉴权中间件 |
| 单飞（single-flight） | 一个 Agent 同一时刻只做一件事（与单循环设计一致） | 所有变更操作共享同一把锁，忙时返回 `409` |
| 透明状态与反黑盒 | 绝不允许任何进程内不可检的内存黑盒；所有状态与未来演进特性（树状分支、WebUI）唯一事实必须是磁盘可读文件 | WebUI 纯粹作为文件状态的只读/透传投影；任何节点与分支直接落盘明文 JSONL/文本指针，记事本中清晰可查 |

---

## 3. 架构

```
main.py          薄前端 1：CLI（一次性任务 / 交互 Shell / --resume）
core/
  api.py         薄前端 2：HTTP JSON API（启动：py -3 -m core.api）
  agent.py       Agent：ReAct 循环、skill 注册与分发、task/checkpoint 生命周期
  config.py      load_config()：唯一配置装配入口（探测模型窗口并推导预算）
  models.py      路径与常量（ROOT、.agent/、logs/）+ 端点环境变量
  tools.py       内置工具实现 + skill 发现（discover_skills）
  context.py     会话消息、token 估算、截图裁剪、上下文压缩
  validator.py   参数校验（schema）与死循环防护（repetition guard）
  persistence.py .agent/tasks/<id>/{checkpoint.jsonl,status} 读写 + list_tasks
skills/*.py      能力文件（见 §4）
tests/           单元/集成测试（见 §11）
.agent/          运行时状态（见 §5）：AGENTS.md、GOAL.md、tasks/、inbox/
logs/            agent.log（运行日志）、safety_guard.log（安全拦截记录）
README.md        使用说明；requirements.txt 依赖；pytest.ini 测试配置；.gitignore 忽略规则
```

### 3.1 运行配置（环境变量，两个前端共用）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GEMMA_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容推理服务地址；空值按未设置处理 |
| `GEMMA_MODEL` | `local-model` | 模型名；若服务校验则填 `/v1/models` 返回的 id |

代码中不硬编码任何私有端点；`n_ctx` 启动时探测，失败回退 `FALLBACK_N_CTX`（32768）。

### 3.2 ReAct 数据流（CLI 与 API 完全一致）

```
任务文本 ──► Agent.run_task()
              ├─ 写 status=running（显式文件标记）
              ├─ 循环 step_loop()
              │    ├─ 达阈值 → 上下文压缩（LLM 摘要）
              │    ├─ 消费 .agent/inbox/*.json（操作员纠偏，移动至 inbox_processed/）
              │    ├─ LLM（OpenAI 兼容 /v1，流式）→ 文本 或 tool_calls
              │    ├─ validator：参数校验 + 重复调用防护
              │    ├─ dispatcher：执行 skill（HANDLER 或 run(args, runtime)）
              │    └─ 工具结果回填上下文（截图附 base64 图像）
              ├─ 收尾：save_checkpoint（会话快照）+ status=done|blocked|finished
              └─ 返回最终文本（含 TASK_DONE / TASK_BLOCKED 标记）
```

---

## 4. Skill 契约（万物皆 skill）

一个 skill = `skills/<name>.py`（文件名即工具名；以 `_` 开头的文件忽略）。

```python
DESCRIPTION = "一句话描述（给 LLM 与 API 调用方）"          # 必需
PARAMETERS = {"type": "object", "properties": {...}, ...}  # 必需，OpenAI function JSON Schema
HANDLER = some_callable          # 方式一：直接可调用；参数以 **kwargs 传入
# 或
def run(args: dict, runtime: dict) -> str   # 方式二：需要运行时上下文时使用
```

- `runtime` 注入内容：`CHROME_CDP_PORT`、`client`（LLM 客户端）、`MODEL`、`CHAT_EXTRA`、`MAX_TOKENS`。
- 返回值：`str`，或 `(str, base64_image)` 元组（如截图）。
- 加载时机：进程启动时发现一次；CLI `reload` / `POST /reload` 可热加载（含 AI 通过 `add_skill`/`modify_skill` 自举新增的 skill）。
- **设计目的**：新增能力 = 新增文件，零核心改动；核心保持最小；LLM 与外部助手看到的工具清单来自同一注册表（`GET /skills` 即该注册表的投影）。

---

## 5. 文件系统契约（状态即文件）

| 路径 | 格式 | 写入者 | 语义 |
| --- | --- | --- | --- |
| `.agent/tasks/<task_id>/status` | 单行词：`running` `done` `blocked` `finished` | `core.persistence.save_status`（由 `Agent.run_task/resume_task`、API 提交时调用） | 任务生命周期标记。`done`=最终答复含 TASK_DONE；`blocked`=含 TASK_BLOCKED 或执行异常；`finished`=循环正常结束但未显式标记；`unknown`=旧任务无该文件 |
| `.agent/tasks/<task_id>/checkpoint.jsonl` | 每行一条 OpenAI 消息 JSON（首行可选 `{"_meta": ...}`） | `save_checkpoint`（任务结束时整份落盘） | 会话快照，`--resume` / `POST /tasks/<id>/resume` 的唯一事实来源 |
| `.agent/inbox/<时间戳>.json` | `{"message": "..."}` | CLI 操作员暂停输入 / `POST /inbox` | 操作员纠偏总线；`Agent._consume_inbox()` 每轮读取并移动到 `.agent/inbox_processed/` |
| `.agent/screen_*.jpg` | JPEG | 截图 skill | 最近截图（上下文中仅保留少量，旧图以文字路径占位） |
| `.agent/AGENTS.md` | Markdown | 人工维护 | **必需**的系统提示（运行规则）；agent 启动与每次构建系统提示时读取，缺失即报错，无内置默认提示 |
| `.agent/GOAL.md` | Markdown | `--goal` 模式下由 LLM 生成 | 长期目标（可选）；goal 模式强制循环至 TASK_DONE/TASK_BLOCKED |
| `logs/agent.log` | 文本 | `core/logger.py` | 运行日志（CLI 与 API 同时追加写入） |
| `logs/safety_guard.log` | 文本 | `core/tools.log_safety` | 危险命令拦截、inbox 解析失败等安全记录 |

- `task_id` 格式：`YYYYMMDD_HHMMSS_ffffff`（本地时间，微秒级唯一，CLI 与 API 相同）。
- 任何任务目录一旦存在即代表「该任务发生过」：只有 `status` 无 `checkpoint` = 运行中被硬杀（状态停留在当时写入值，需人工判断）。

---

## 6. CLI 契约（main.py）

```
py -3 main.py [--goal] [--resume [<task_id>]] ["initial task"]
```

- 无参数进入交互 Shell；命令：任意任务文本、`reload`（热加载 skills）、`resume [<id>]`（列出/恢复任务）、`exit|quit|q`。
- `--resume` 不带 id：列出 `.agent/tasks` 下的任务（含 `status` 与消息数）。
- 任务运行中按空格键进入操作员暂停 Shell（可输入纠偏，效果等同 inbox）。
- 配置装配唯一入口：`core.config.load_config()`（探测 `/v1/models` 的上下文窗口并推导生成/压缩预算）。
- 系统提示来自 `.agent/AGENTS.md`（必需文件；缺失或为空时 `build_system()` 直接报错退出，不提供任何内置默认提示）。

---

## 7. HTTP API 契约（core/api.py）

```
py -3 -m core.api [--host 127.0.0.1] [--port 8765] [--goal]
```

（在项目根目录以模块方式启动；`core/api.py` 不做 `sys.path` 魔法，只走标准包导入。）

- 默认只绑定回环地址 `127.0.0.1`。**安全提示：shell/skill 可在本机执行任意命令，绑定非回环地址等于开放本机 RCE，仅在完全信任的网络内使用 `--host`。**
- 全部响应为 JSON（`application/json; charset=utf-8`）；错误统一为 `{"error": "..."}`。
- 并发模型：**单飞**。任务运行、任务恢复、skill 直接调用、reload 共享同一把锁，忙时返回 `409`（附 `active_task`）；`/inbox` 是唯一在忙时仍可写的端点（设计如此：它是运行中任务的转向通道）。
- 写操作立即返回，任务在后台线程执行；进度与结果通过 **§5 的文件** 与 `GET /tasks/<id>` 轮询获得。

### 7.1 端点

| 方法 | 路径 | 请求体 | 成功响应 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/` | — | `200` 端点索引 | 供 AI 助手自发现 |
| GET | `/health` | — | `200 {"ok","model","base_url","n_ctx","goal_mode","busy","active_task"}` | 存活与配置 |
| GET | `/skills` | — | `200 {"skills":[{"name","description","parameters"}]}` | 与内部 LLM 所见工具表一致 |
| GET | `/tasks` | — | `200 {"tasks":[{id,created,status,message_count}]}` | 即 `list_tasks()` |
| GET | `/tasks/<id>` | — | `200 {id,status,created,message_count,last_assistant}`；未知 `404` | 轮询任务 |
| POST | `/tasks` | `{"task":"..."}` | `202 {"task_id","status":"running"}`；忙 `409`；空任务 `400` | 异步执行任务（=CLI 一次性模式） |
| POST | `/tasks/<id>/resume` | — | `202 {"task_id","status":"running"}`；无 checkpoint `404`；忙 `409` | =CLI `resume <id>` |
| POST | `/skills/<name>` | skill 参数对象（即该 skill 的 `PARAMETERS` 实例） | `200 {"result": str, "image_base64": str\|null}`；未知 skill `404`；参数不合法 `400`；执行异常 `500` | 直接调用 skill（复用同一 validator 与 dispatcher） |
| POST | `/inbox` | `{"message":"..."}` | `202 {"ok":true,"file":"<名>.json"}` | 向运行中的任务注入纠偏信息 |
| POST | `/reload` | — | `200 {"ok":true,"skills":N}`；忙 `409` | =CLI `reload` |

### 7.2 典型流程（外部 AI 助手视角）

```bash
# 发现能力
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/skills

# 直接使用原子能力（无需本机模型回合）
curl -X POST http://127.0.0.1:8765/skills/chrome_dom -d '{"action":"links"}'
curl -X POST http://127.0.0.1:8765/skills/screenshot -d '{}'

# 委派一个多步任务并轮询
curl -X POST http://127.0.0.1:8765/tasks -d '{"task":"打开 Chrome 搜索天气并截图"}'
# -> {"task_id":"20261007_171214_792655","status":"running"}
curl http://127.0.0.1:8765/tasks/20261007_171214_792655
# -> {"status":"done","last_assistant":"...TASK_DONE...", ...}

# 运行中纠偏 / 中止
curl -X POST http://127.0.0.1:8765/inbox -d '{"message":"跳过登录，直接使用已登录的标签页"}'
```

### 7.3 CLI ↔ API 能力对照

| 用途 | CLI | HTTP API |
| --- | --- | --- |
| 提交任务 | `py -3 main.py "task"`（阻塞直至完成） | `POST /tasks` → 轮询 `GET /tasks/<id>` |
| 恢复任务 | `--resume <id>` / 交互 `resume <id>` | `POST /tasks/<id>/resume` |
| 任务列表 | `--resume` | `GET /tasks` |
| 热加载 skills | 交互 `reload` | `POST /reload` |
| 调用单个 skill | 仅经 LLM 循环间接调用 | `POST /skills/<name>`（同一 dispatcher） |
| 纠偏运行中任务 | 空格暂停 Shell；写 inbox 文件 | `POST /inbox`（写同一 inbox 文件） |
| 长期目标模式 | `--goal` | `--goal`（服务启动参数） |

---

## 8. 状态一致性模型

1. **`.agent/` 中的文件是唯一共享事实来源。** 内存中的 Agent/会话只是当前进程的工作副本；进程结束前必须把结果落盘（checkpoint + status）。
2. **每个任务执行完必写两个标记文件**：`checkpoint.jsonl`（内容）与 `status`（结论）。运行开始即写 `status=running`，因此轮询方能在任务开始后立刻观测到它。
3. **会话语义与 CLI 交互模式一致**：同一进程内多个任务共享同一会话上下文（`run_task` 追加用户消息，不清空历史）；`resume` 会用 checkpoint 覆盖当前上下文。跨进程共享的是文件，不是内存。
4. **崩溃语义（显式、不隐藏）**：进程被硬杀时任务停留在 `status=running` 且可能无 checkpoint；恢复手段 = 用 CLI/API 对该 id 执行 resume（有 checkpoint）或重开任务（无 checkpoint）。
5. **单飞约束**：同一时刻只有一个任务/一次 skill 调用执行（`409` 即状态信号，不是错误隐藏）。队列有意不做——排队状态会引入不可见的隐藏逻辑。
6. **goal 模式**是服务/进程级开关（与 CLI 的 `--goal` 语义一致），不是按请求参数。

---

## 9. 当前阶段边界（明确不做）

- 无鉴权、无 TLS、无多用户——本 API 面向本机可信 AI 助手；默认回环绑定。
- 当前不引入复杂多代理网络与重型任务队列——严格保持 Single-flight 单飞与 KISS 原则，轮询文件状态即可。
- 不做 skill 的远程市场或复杂生命周期管理——一个 skill 就是一个自描述本地 Python 脚本。
- 不引入不可变内存树对象与复杂长驻事件总线——所有上下文与演进必须落在文件系统（详见 §13 演进路线）。

---

## 10. 已决事项记录

1. **运行规则文件位置**（已决）：系统提示固定来自 `.agent/AGENTS.md`，目标固定写入 `.agent/GOAL.md`（`core.models.AGENTS_PATH / GOAL_PATH`）。
2. **无默认系统提示**（已决）：代码中不存在任何内置 system prompt（`DEFAULT_SYSTEM` 已删除）；`.agent/AGENTS.md` 缺失或为空时 `build_system()` 抛出 `RuntimeError`，问题显式暴露而不是静默回退。
3. **端点配置外置与视频功能剥离**（已决）：`GEMMA_BASE_URL` / `GEMMA_MODEL` 环境变量是唯一配置途径，默认值为中性回环地址；非 Harness 范畴的视频处理与抽帧逻辑（如 `segment_video_dance`）已彻底从目标仓库剥离。
4. **第三方数据清理**（已决）：`.agent/GOAL.md.bak`（含外部账号信息）已从仓库移除；工作区隐私基线由 `tests/test_repo_hygiene.py` 守护。
5. **保留的已知边界**：CLI 单次运行与 API 服务并发写同一任务目录属未定义行为（正常使用中不会发生：任务 id 微秒级唯一，且各自写各自目录）；git 历史中仍保留旧提交的敏感内容，推送前需按 §12 处理。
6. **作废外部异构重构尝试（dsh）**（已决）：正式作废在外部试验目录中尝试的 TypeScript / Node.js 重构项目。原因在于其引入不可变内存树、长驻事件总线、私有编译链与异构运行环境，违背奥卡姆剃刀与极简轻量原则，造成严重心智负担与黑盒化倾向。未来所有 WebUI 与树状分支演进均基于当前 Python 原生核心（`core/` + `core/api.py`）以极简、透明、无黑盒的文件基方式演进（见 §13）。

---

## 11. 测试（tests/）

- 框架：标准库 `unittest`，零新增依赖；不访问真实模型服务。
- 隔离：`tests/support.py::IsolatedCase` 把 `TASKS_ROOT`、`INBOX` 等状态目录重定向到临时目录；`ScriptedLLM` 顶替 OpenAI 客户端，回复可编排（内容 / tool_calls / 延迟 / 抛错）。
- 覆盖：

| 文件 | 覆盖对象 |
| --- | --- |
| `test_persistence.py` | task_id 唯一性、checkpoint 往返、status 标记读写、list_tasks（含旧任务 `unknown`）、非法 id 拒绝 |
| `test_validator.py` | 参数 schema 校验、单调用与批量重复死循环防护、reset |
| `test_context.py` | token 估算、截图保留与降级、上下文压缩、clear_task |
| `test_config.py` | 预算推导、`load_config()` 探测注入、上下文窗口探测（llama.cpp/OpenRouter 字段、不可达与畸形载荷回退）、环境变量覆盖与中性默认值、无默认系统提示 |
| `test_agent.py` | 系统提示读取（`.agent/AGENTS.md` 必需、缺失即失败）、skill 契约强制与热加载、`collect_stream` 文本/推理/tool_calls 重组与断流传播、goal 模式续跑、max_rounds 终止、run_task/resume_task 的 status+checkpoint 落盘 |
| `test_skills.py` | 对 `skills/` 全量文件的契约巡检（DESCRIPTION/PARAMETERS/HANDLER|run） |
| `test_tools.py` | load_text、路径解析、文件读写与大文件截断、ripgrep 搜索、危险命令正则多分支拦截与安全日志、read_skill、write→shell→read 工具链 |
| `test_api.py` | 进程内真实 HTTP 服务：发现端点、skill 调用与校验、任务生命周期、单飞 409、inbox 通道、resume、错误码 |
| `test_cli.py` | 真实 CLI 入口冒烟（子进程：`main.py exit`、交互式退出）；缺 `openai` 包时跳过 |
| `test_repo_hygiene.py` | 工作区隐私基线：禁止出现私有网段、外部账号/域名、服务器家目录路径等模式 |

- 运行（项目根目录）：

```bash
py -3 -m unittest discover -s tests -t . -v
pytest            # 兼容运行（见 pytest.ini）
```

---

## 12. 发布检查（推送到公开仓库前必读）

**背景**：远端 `origin` 是公开仓库；本地未推送的 `init` 提交（`3bb63a5`）在创建时包含过 `.agent/GOAL.md.bak`（外部账号信息）与旧的内网端点字面量。工作区现已清理，但 **git 历史里仍存在旧版本内容**，直接 `git push` 会把它们永久公开。

工作区扫描（应无输出）：

```bash
git grep -n -I -E "192\.168\.|MS4w\w+|[d]ouyin[.]com|/home/[a-z]+/bin/"
py -3 -m unittest tests.test_repo_hygiene -v
```

推送前必须先重写那一个未推送提交（父提交不变，推送仍是 fast-forward）：

```bash
git add -A   # 把全部清理、脱敏与规范加固结果并入 init 提交
git commit --amend --no-edit
git push
```

其余改动正常提交即可。若不想改写历史，则不要推送当前分支（或让仓库保持私有）。

---

## 13. 演进路线（Roadmap）：文件基树状上下文与透明 Web 控制台

为满足长远开发需要，系统规划支持**树状多分支上下文版本管理**与**极简 Web 控制台**。这两项演进必须无条件遵守 §2 的「透明状态与反黑盒」原则与「奥卡姆剃刀」原则，绝不引入不可控的认知与维护负担。

### 13.1 文件基树状上下文（File-backed Context Tree）

- **设计目标**：支持在长程探索任务中安全分叉（Fork）、切换（Switch）和回退（Rewind），而无需把树状结构包裹在复杂的内存图对象中。
- **持久化契约（借鉴 Git 思想，状态全显式）**：
  - 任务目录：`.agent/tasks/<task_id>/`
  - 消息记录拓展：`checkpoint.jsonl` 中每个节点显式包含 `id` 与 `parent_id`（线性历史等价于每个节点的 `parent_id` 指向前序节点；完全向下兼容）。
  - 分支即指针文件：`.agent/tasks/<task_id>/branches/<branch_name>` 为普通 UTF-8 文本文件，单行记录当前叶子节点的 `node_id`。默认分支为 `main`。
  - 活跃分支标记：`.agent/tasks/<task_id>/current_branch` 单行记录当前分支名（如 `main` 或 `experiment_1`）。
- **心智负担控制**：
  - 开发者用任何文本编辑器打开 `checkpoint.jsonl` 和 `branches/` 目录，肉眼即可 100% 还原因果链路，没有任何隐藏状态。
  - 运行时 ReAct 循环一次仅遍历当前分支的父链投影，依然严格遵守 **单飞执行（Single-flight）** 约束，无跨分支并发并发症。

### 13.2 零构建透明 Web 控制台（Zero-build Web Console）

- **设计目标**：为人类操作员提供更直观的可视化界面，随时查看因果树形图、实时回放对话、下发暂停或纠偏指令（替代纯终端空格按键）。
- **零依赖与无打包器选型**：
  - 纯原生前端：基于标准 HTML5、原生 ES Modules、极简 CSS 实现。
  - 绝不引入 Node.js 构建链、Webpack/Vite、npm 包管理器或庞大 UI 框架。
  - 部署形态：直接由 `core/api.py` 的轻量静态文件路由托管（`GET /web/...`），随 Harness 一键随起随停。
- **反黑盒交互原则（镜子定位）**：
  - WebUI 是底层文件状态的投影，绝不拥有独立于文件系统的持久化状态。
  - Web 端刷新即从 HTTP API 获取文件当前投影；Web 端的交互动作（如提交任务、创建分支、发送 inbox 纠偏）均直接调用现有 REST API 转化为显式文件写入。

### 13.3 演进门禁（Simplicity & Transparency Gate）

未来任何针对树状分支或 WebUI 的 PR/改动，必须满足以下三重验证门禁：
1. **断电/强杀测试**：进程在任意执行时刻被强杀后，所有状态依然能仅通过磁盘文件完整审计与恢复。
2. **纯文本可读性**：脱离 WebUI，普通开发者仅通过 CLI 和文件资源管理器即可完成分支排查与任务继续。
3. **依赖零增长**：不引入额外的常驻后台守护进程或外部数据库依赖。

---

## 附：验证方式

- 启动：`py -3 -m core.api`，`GET /health` 应返回 `ok=true` 且 `n_ctx > 0`。
- 一致性自测：`POST /tasks` 提交后，`py -3 main.py --resume` 应列出同一 `task_id` 且 `status` 一致。
- 回归自测：`py -3 -m unittest discover -s tests -t .` 应全部通过。