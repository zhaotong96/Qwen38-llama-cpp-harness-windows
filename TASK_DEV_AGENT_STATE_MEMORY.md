# TASK_DEV_AGENT_STATE_MEMORY.md: Phase 6B 内核状态机与长程事实账本加固

- **阶段目标**：Phase 6B 根除跨会话泄漏、防网络探测风暴、MTP工具语法抗破损、双槽隔离与五段式结构化事实账本落地
- **执行角色**：内核状态机与长程上下文工程师 (Agent Core & Context Engineer)
- **前置依赖**：Commit `2a9e4b1`（Phase 5B 闭环，107 项单元测试全绿灯）
- **权威契约**：[SPEC.md](SPEC.md) §3.2、§5、§7、§8，`report.md` §1.2、§2、§3、§5
- **状态**：待领用 (Pending Assignment)

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

- [ ] 向守护进程连续发起 2 个任务，验证 task2 检查点为清白干净上下文，无 task1 历史泄漏；
- [ ] 快速多次触发前端页面刷新，验证日志中不再每秒刷出冗余的 `[ctx] probed context window`；
- [ ] 模拟模型输出畸形 JSON 参数，验证系统未在屏幕盲点鼠标，而是向模型反馈明确的语法错误回执；
- [ ] 验证 `main` 角色与 `subagent`/`compactor` 角色向模型发送的请求分别携带 `id_slot: 0` 与 `id_slot: 1`；
- [ ] 触发上下文压缩，验证 Compactor 成功输出五段式事实账本，无思考预算耗尽截断；
- [ ] 使用 `read_checkpoint_node` 能够成功读取已被折叠的早期原始节点内容；
- [ ] 全量 107 项原有单元测试与新增逻辑测试 100% 绿灯；
- [ ] 回填文末“四、验收回执”。

---

## 四、验收回执（执行人填写）

*(待执行工程师完成后回填)*
