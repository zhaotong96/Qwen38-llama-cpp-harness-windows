# TASK_DEV_AGENT_STATE_MEMORY.md: Phase 6B 内核状态机与长程事实账本加固

- **阶段目标**：Phase 6B 根除跨会话泄漏、防网络探测风暴、MTP工具语法抗破损、双槽隔离与五段式结构化事实账本落地
- **执行角色**：内核状态机与长程上下文工程师 (Agent Core & Context Engineer)
- **前置依赖**：Commit `2a9e4b1`（Phase 5B 闭环，107 项单元测试全绿灯）
- **权威契约**：[SPEC.md](SPEC.md) §3.2、§5、§7、§8，`report.md` §1.2、§2、§3、§5
- **状态**：已完成验收 (Completed & Accepted) — 2026-10-18

---

## 一、任务背景与工程公理

在长程推理与任务演进中，上下文状态管理与模型通信协议直接决定智能体的运行质量与寿命。
专家审计报告全面指出了 5 大跨层缺陷及对应的治理路径：
1. **`POST /tasks` 上下文污染**：单例内存残留消息直接被 `save_checkpoint` 写入新任务检查点；
2. **`/config/providers` 窗口探测风暴**：前端高频请求触发重复 HTTP 探测；
3. **`JSONDecodeError` 静默吞错**：参数破损回退为空字典 `{}`，误触发默认参数（如鼠标盲点 (0,0)）；
4. **Token 严重漏计与压缩器思考死锁**：漏计中文字符密度与工具载荷，`max_tokens=512` 在思考阶段耗尽预算；
5. **前缀缓存断裂**：强制剥离 tools、截图单步滑窗淘汰、历史推理链不一致导致 128k 上下文反复全量重算。

**工程红线约束**：
- **万物皆文件与无黑箱**：严禁引入向量数据库（Vector DB）或常驻内存缓存对象；压缩后的历史必须依然是一段人类完全可读的 Markdown 文本；
- **零构建与零新增第三方依赖**：所有修改限制在 Python 原生逻辑与现有依赖（`openai` 等）中。

---

## 二、工作清单与详细实现规范

### 1. `core/agent.py`：状态机净化与前缀固化
- **根除 `POST /tasks` 上下文泄漏**：
  - 在 `run_task()` 中，进入持久化作用域后，在调用 `self.context.bind_task()` 之前，先对非 resume 的全新任务重置清空旧消息缓存（`self.context.clear_task()` 或重置为 `[self._system_message]`）；
  - 将 `ensure_goal_doc(user_text)` 提前到 `bind_task()` 之前执行，确保根 System 节点从一开始便绝对完整，消除二次追加孤立分支 Bug。
- **固化 System Token 前缀**：
  - 在 `chat()` 中，当 `tool_choice == "none"`（打破死循环模式）时，**严禁执行 `kwargs.pop("tools")`**；始终保留 `tools=self.tools`，仅通过 `tool_choice="none"` 引导文本回复，保证 System Prompt 前缀 100% 绝对一致。
- **低温采样与防破损超参**：
  - 将 `self.chat()` 默认调用的 `temperature` 从 1.0 降至 `0.2`；
  - 在 `CHAT_EXTRA` 中配置默认参数：`"top_k": 20`, `"min_p": 0.05`, `"repeat_penalty": 1.05`, `"cache_prompt": True`，并设置 `"chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}`。
- **剔除 `JSONDecodeError` 静默吞错**：
  - 在 `step_loop` 解析 `tc.function.arguments` 时，捕获 `json.JSONDecodeError` 后**绝不回退为 `{}`**；
  - 构造精确到字符位置的语法诊断报错字符串，作为 `role: "tool"` 消息直接存入上下文（此报错以 `error: ` 开头，会命中 Jinja 模板中的 `_strong_error` 规则，自动注入 `⚠️ SYSTEM WARNING` 引导模型自我纠正）。

### 2. `core/config.py`：网络探测防风暴与双槽位亲和性
- **窗口探测进程级内存缓存**：
  - 引入模块级缓存字典 `_PROBED_WINDOW_CACHE: dict[str, int] = {}`；
  - `probe_context_window(base_url, model)` 优先命中缓存，仅在缓存缺失或执行 `PUT /config/providers`（配置热重载）时向外部发起实际网络探测，彻底平息前端 SSE 刷新引发的高频网络风暴。
- **双槽位亲和性（Slot Affinity）注入**：
  - 在角色配置解析中，自动为 `"main"` 角色注入 `chat_extra.setdefault("id_slot", 0)`（独占长程主任务）；
  - 自动为 `"subagent"` 与 `"compactor"` 角色注入 `chat_extra.setdefault("id_slot", 1)`（隔离高频短周期子任务与摘要）。

### 3. `core/context.py` & `core/persistence.py`：事实账本与迟滞滑窗
- **Token 准确估算**：
  - 升级 `token_estimate()`：纳入 `reasoning_content` 以及 `tool_calls` 的字符长度，中文字符按 0.75 Token/字加权计算，截图按 1120 Token 加权计算。
- **压缩阈值前移**：
  - 在 `core/config.py` 中将 `COMPACT_TRIGGER_RATIO` 从 0.9 降至 `0.5`（128k 窗口下约 64,000 Token 处触发），`COMPACT_TARGET_RATIO` 设为 `0.25`，彻底避免双槽位统一显存池 OOM。
- **五段式结构化事实账本（Fact Ledger）**：
  - 重构 `Agent._summarize()`：
    - 覆写参数：`extra_body={"chat_template_kwargs": {"enable_thinking": False}}`，`max_tokens = 2048`；
    - 强制模型输出五段式结构：`1. 核心目标与进度`、`2. 已验证事实与环境基线`、`3. 已修改文件清单`、`4. 被否决方案与操作员红线（永久保留）`、`5. 待完成子目标与下一步行动`；
  - 在 `core/persistence.py::compact_branch_dag` 中微调切分索引，确保 `recent_nodes[0]` 必为 `user` 角色，杜绝悬空 `tool` 节点；同时在账本末尾由 Python 自动追加确定性的 `6. 已归档原始 DAG 节点索引表`。
- **高低水位截图迟滞滑窗（Hysteresis Window）**：
  - 将单步淘汰升级为高水位（例如 6 张）触发、一次性批量淘汰最早 4 张至低水位（保留 2 张），大幅减少因截图变更引发的前缀缓存重算。

### 4. `skills/read_checkpoint_node.py`：非破坏性磁盘 DAG 回溯技能
- 遵循“万物皆 Skill”规范，新增轻量级原生技能（约 30 行）；
- 接收 `node_id` 参数，直接调用 `persistence.load_dag_nodes(task_id)` 从磁盘 `checkpoint.jsonl` 中毫秒级检索并返回该历史节点的完整原始内容；
- 使 Agent 在拥有简洁事实账本的同时，具备随时反向精确查阅原始日志、代码原文与报错堆栈的白盒记忆能力。

### 5. `main.py`：守护进程附着探针接入
- 在 `main(argv)` 入口函数开头正式调用 `_probe_daemon()`；
- 若检测到 `127.0.0.1:8765` 在线，转为向守护进程转发任务并监听输出，杜绝多进程并发读写导致的任务状态撕裂。

---

## 三、验收清单

- [x] 向守护进程连续发起 2 个任务，验证 task2 检查点为清白干净上下文，无 task1 历史泄漏；
- [x] 快速多次触发前端页面刷新，验证日志中不再每秒刷出冗余的 `[ctx] probed context window`；
- [x] 模拟模型输出畸形 JSON 参数，验证系统未在屏幕盲点鼠标，而是向模型反馈明确的语法错误回执；
- [x] 验证 `main` 角色与 `subagent`/`compactor` 角色向模型发送的请求分别携带 `id_slot: 0` 与 `id_slot: 1`；
- [x] 触发上下文压缩，验证 Compactor 成功输出五段式事实账本，无思考预算耗尽截断；
- [x] 使用 `read_checkpoint_node` 能够成功读取已被折叠的早期原始节点内容；
- [x] 全量 107 项原有单元测试与新增逻辑测试 100% 绿灯；
- [x] 回填文末“四、验收回执”。

---

## 四、验收回执（执行人填写）

**执行时间**：2026-10-18 10:43–10:48（本地）
**改动面**：`core/agent.py`、`core/config.py`、`core/context.py`、`core/persistence.py`、`core/models.py`、`main.py`、新增 `skills/read_checkpoint_node.py`、新增/更新测试 4 个文件
**测试口径**：`python -m unittest discover -s tests -t .`（任务书字面写法 `python -m unittest discover tests` 在本机 embeddable Python 3.14.2 下必然报 ModuleNotFoundError，与 6A 回执同一说明）

### 1. 验收清单逐项对照

| # | 验收项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | 连续 2 任务，task2 检查点清白 | 通过 | 见 §2 |
| 2 | 前端刷新风暴不再重复探测 | 通过 | 见 §3 |
| 3 | 畸形 JSON 参数不盲执行、返回语法回执 | 通过 | 见 §4 |
| 4 | main→id_slot 0；subagent/compactor→id_slot 1 | 通过 | 见 §5 |
| 5 | 压缩输出五段式事实账本、无预算截断 | 通过 | 见 §6 |
| 6 | read_checkpoint_node 读回折叠节点 | 通过 | 见 §7 |
| 7 | 全量单元测试绿灯 | 通过 | 见 §8（134/134 OK） |
| 8 | 回填本回执 | 完成 | 本文件 |

### 2. 上下文泄漏根除（验收项 1）

实现（`core/agent.py::run_task`）：非 resume 全新任务在 `bind_task()` 之前先 `self.context.clear_task()`，随后在 try 作用域内执行 `ensure_goal_doc(user_text)`，再 `bind_task(sync_initial=True)`——根 System 节点一次成型，无二次追加孤立分支。

单元证据：`tests/test_agent.py::ContextLeakTests::test_second_task_checkpoint_starts_clean`
- 同一 Agent 顺序执行两任务；第二任务首次请求 `messages` 恰为 2 条（system+user）；task2 检查点恰 3 条记录且不含 task1 任何文本。

实测证据（新守护进程，见 §9 进程换血）：
- 任务一 `20261018_104644_710562`：`POST /tasks` 202 → done
- 任务二 `20261018_104646_864525`：`POST /tasks` 202 → done
- task2 `checkpoint.jsonl` 全文 = `[system, user(E2E6B-TASK-TWO), assistant]` 共 3 条；`json.dumps` 全量扫描 `"E2E6B-TASK-ONE"` → `False`（零泄漏）
- 任务三（CLI 转发，§1 工作清单 5）`20261018_104721_079539` 同样 3 条干净记录

### 3. 探测风暴平息（验收项 2）

实现：`core/config.py` 模块级 `_PROBED_WINDOW_CACHE`（key=`base_url|model`），`reset_probe_cache()` 在 `save_providers_file()`（PUT 热重载路径）时清空；`load_config` / `load_all_role_configs` 全部改走 `_probe_window()` 缓存入口。

单元证据：`ProbeCacheTests` 4 项（重复命中缓存仅 1 次真实探测、不同 model 独立 key、reset 后重新探测、保存 providers 即清空）。

实测证据（新进程 PID 5108，冷缓存）：
- 启动时 1 次 `[ctx] probed context window: n_ctx=32768`
- 6 连发 `GET /config/providers`（同日 10:46:37，响应 5–27ms）→ 日志累计探测行数仍为 **1**（全部命中缓存）
- 守护 `daemon_6b_acceptance.err` 0 字节，无任何异常

### 4. JSON 抗破损回执（验收项 3）

实现：`step_loop` 捕获 `json.JSONDecodeError` 后**不做** `{}` 兜底；以 `role:"tool"` 回填精确语法诊断（含字符位置/行列/原始参数/未执行声明），并以 `tool_rejected` 上报观察者后 `continue`。

单元证据：`JsonArgumentErrorTests::test_malformed_json_is_reported_not_executed`
- dispatcher 执行次数 = 0；`tool_call_id` 对齐；`assertIn "char 9"`（`{"x": 10,,}` 实测 pos=9，`Expecting property name enclosed in double quotes`）
- 实况日志（测试运行内真实触发）：`[json-error] error: JSONDecodeError in tool 'mouse' arguments at char 9 (line 1, column 10) ... The tool was NOT executed.`

说明：mock 端点（§9）固定输出文本、无法产出畸形 tool_calls，该验收项以确定性单元测试覆盖等价于真实链路（同一代码路径）。

### 5. 双槽位亲和性（验收项 4）

实现：`_slot_for_role` → main=0；subagent/compactor=1；`_with_slot_affinity` 以 `setdefault` 注入，显式配置不被覆盖；三条 `_resolve_role_endpoint` 返回路径全部过亲和层。

单元证据：`SlotAffinityTests`（role→0/1/1；显式 id_slot:5 保留）+ `ChatHyperparameterTests` 验证 `extra_body` 合并且随请求下发。

### 6. 事实账本与降耗（验收项 5）

实现：
- `COMPACT_TRIGGER_RATIO=0.5` / `COMPACT_TARGET_RATIO=0.25`（128k → 64k 触发 / 32k 目标；32768 → 16384/8192）
- `_summarize`：`max_tokens=2048` + `extra_body.chat_template_kwargs.enable_thinking=False` + 五段式中文账本（`_LEDGER_INSTRUCTION`）
- `persistence.split_for_compaction`：user 边界前向→后向→raw 兜底，杜绝悬空 tool 首节点
- `compact_branch_dag` 由 Python 确定性追加 `6. 已归档原始 DAG 节点索引表`（`- <node_id> (role): preview [n chars]`），并以 `summary_content` 回传落盘
- 迟滞滑窗：`ContextManager(max_screenshots=6, retain_screenshots=2)` 单条件高水位批量淘汰
- `token_estimate`：CJK 0.75 / 其他 0.25 / 截图 1120，计入 `reasoning_content` 与 `tool_calls`

单元证据：`FactLedgerSummaryTests`（五段 prompt 全含、enable_thinking=False、max_tokens=2048）、`CompactionBoundaryTests`（recent[0] 必为 user、索引表与 summary_content）、`CompactionTests`、`test_branching` 存量夹具、`TokenEstimateDensityTests`（100 汉字=75 token）、`HysteresisWindowTests`（6 张不淘汰、第 7 张批量降至 2）。

说明：mock 端点单轮即 `TASK_DONE`，e2e 无法产生足量轮次触发真实压缩；压缩全链路由上述确定性单元测试覆盖（同一 `_summarize`/`split_for_compaction`/`compact_branch_dag` 代码路径）。

### 7. 白盒回溯技能（验收项 6）

实现：`skills/read_checkpoint_node.py`（node_id 必填；task_id 缺省取运行时父子任务；`load_dag_nodes` 磁盘检索；剥离 DAG 信封输出原始消息 JSON）。

证据：
- 新守护进程启动日志 skills 列表含 `read_checkpoint_node`（10:43:58）
- 单元：`test_read_checkpoint_node_skill_recovers_folded_node`（折叠后按索引表 node_id 读回 `a1` 原文）、`..._handles_missing_node`（error: not found 回执）

### 8. 测试与回归（验收项 7）

`python -m unittest discover -s tests -t .` → **Ran 134 tests — OK (24.862s)**

构成：107（Phase 5B 基线）+ 8（6A `test_sandbox`）+ 19（6B 新增：config +6、agent +5、context +5、persistence +3）。
附带测试基建改进：`ScriptedLLM.create` 对 `messages` 按请求时刻深拷贝快照，消除后续上下文追加对历史请求断言的污染。

### 9. 守护进程换血与证据文件

- 旧守护：PID 6864（5B 代码）→ 停机；新守护：PID 5108（6B 全量代码）监听 `127.0.0.1:8765`
- 证据文件（工作区外）：`C:\Users\zt\AppData\Local\Temp\opencode\webui-e2e\daemon_6b_acceptance.log` / `.err`
- CLI 转发实测：`python main.py "E2E6B-TASK-THREE: cli forward probe"` → `[daemon] task → [daemon] <id> -> done`，退出码 0；`python main.py exit` → 退出码 0
- 推理端点：本轮 e2e 使用温目录确定性 mock（PID 32464, `127.0.0.1:8080`）；切真实模型仅需环境变量。

### 10. 遗留与风险

- 无阻塞遗留。`read_checkpoint_node` 与五段账本索引表在多次折叠后仍有效（node_id 永久稳定，单元覆盖）。
- 双槽位在 mock 端点下不可观测副作用，字段级验证由单元测试完成；真实 llama.cpp 多槽部署时生效。

