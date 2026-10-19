# TASK_DEV_PHASE7_SYSTEM_HARDENING.md: Phase 7 工业级长程自治、防御纵深与人机协同深化

- **阶段目标**：Phase 7 落地常数级 $O(1)$ 管道流排空、全矩阵非交互防护、SubAgent 跨沙箱穿透、长程级联压缩 100% 确定性红线保真、同槽位前缀继承压缩、零构建 Web 控制台事实看板与高危写操作风险门禁（`step_risky`），以及 50+ 步长程自治压测套件
- **执行角色**：系统运行时与核心状态机执行工程师 (Runtime Systems & Agent Core Engineer)
- **前置依赖**：Commit `68b45e3`（Phase 6A & 6B 已合入并推送，全量 134 项单元测试 100% 绿灯）
- **权威契约**：
  - [SPEC.md](SPEC.md) §2（设计原则）、§3.2（ReAct 数据流）、§7（工具分发）、§13（演进路线）
  - [ARCHITECTURE_RFC_PHASE7_HARDENING_AUTONOMY.md](ARCHITECTURE_RFC_PHASE7_HARDENING_AUTONOMY.md)（Phase 7 RFC 咨询备忘录）
  - [report.md](report.md)（专家评审报告 Phase 7 终审意见）
- **状态**：**【已完成验收 (Completed & Accepted)】** — 2026-10-19

---

## 一、任务背景与负责人硬性工程纪律

在 Phase 6 解决了内核沙箱、防跨任务泄漏与基础事实账本后，系统进入面向 50~100 步无人值守自治与复杂人机协同的 Phase 7 演进。
**项目负责人确立了以下不可动摇的工程红线，执行工程师必须无条件恪守：**

1. **绝对锁定服务参数，严禁越界改动**：
   - `--spec-draft-n-max` 严格维持 `4`（立足 Tesla M40 显存带宽 288 GB/s 受限现实，最大化摊薄单步访存开销，严禁下调）；
   - `--cache-reuse` 严格保持 `0`（绝对严禁开启，规避多模态 `mmproj` 视觉 Patch 嵌入与 Token 前缀缓存冲突导致的内核断言崩溃）；
   - 远程 Linux 模型服务当前 Slot 1 有外部长时任务，服务端保持冷备暂不重启，客户端必须具备 100% 平滑自适应与容错能力。
2. **KISS 原则与零依赖红线**：
   - 严禁引入任何新的第三方依赖，`requirements.txt` 必须严格维持 4 项（`openai`, `mss`, `pillow`, `websocket-client`）；
   - 前端必须严格守住**零构建（Zero-Build）**：严禁引入 Node.js、npm、Webpack/Vite 或外部 UI 框架，全部采用纯原生 HTML5/CSS/ES Modules。
3. **万物皆文件与无黑箱**：
   - 状态转移唯一权威事实来源必须是磁盘文件（`.agent/`），绝不在内存中维护不可见的孤立布尔状态；
   - 保持断电与强杀可审计性，支持纯文本文本编辑器无缝排查。

---

## 二、分阶段工作清单与详细实现规范

本任务卡片划分为 **P0 基础防御与断层修复**、**P1 长程保真与槽位优化**、**P2 控制台人机协同与长程压测** 三个阶段，工程师须按序实施。

```mermaid
flowchart LR
    P0["P0: 基础防御纵深与跨沙箱断层修复"] --> P1["P1: 级联压缩抗遗忘与同槽前缀继承"]
    P1 --> P2["P2: 零构建 Web 控制台人机协同与长程压测"]
```

---

### 【阶段 P0】：基础防御纵深与跨沙箱断层修复

#### 1. `core/tools.py`：$O(1)$ 常数级双端环形流排空器（彻底杜绝无换行巨型单行内存膨胀）
- **现状缺陷**：现行 `_drain_stream` 使用 `for chunk in iter(stream.readline, ""):`，在遭遇无换行符的单行数兆字节文本（如压缩前端产物、大单行 JSON 日志、二进制流）时，CPython `io.TextIOWrapper` 会在 C 语言层无限扩容单块缓冲区，击穿宿主内存。
- **重构规范**：
  - 引入常量：
    ```python
    PIPE_HEAD_BYTES = 256 * 1024  # 头部固定保留 256 KB
    PIPE_TAIL_BYTES = 256 * 1024  # 尾部环形保留 256 KB
    PIPE_READ_CHUNK = 4096        # 每次读取 4096 字符块，杜绝超长单行 C 层膨胀
    ```
  - `_drain_stream(stream, sink, head_limit=PIPE_HEAD_BYTES, tail_limit=PIPE_TAIL_BYTES)` 重构为使用 `collections.deque` 实现双端环形缓冲区：
    - 使用 `while True: chunk = stream.read(PIPE_READ_CHUNK)` 分块读取；
    - 前 `head_limit` 字符存入 `head_parts: list[str]`；
    - 超出部分存入 `tail_parts: deque[str]`，当 `tail_chars > tail_limit` 时从左侧 `popleft()` 弹出，并累加 `omitted_chars`；
    - 流结束时向 `sink` 依次追加 `head_parts`、省略提示符（若有截断则插入 `\n...[output truncated: {omitted_chars} more chars omitted]...\n`，必须保留 `"more chars"` 字样以兼容既有断言）以及 `tail_parts`；
    - 单管道峰值内存占用数学锁定在 $\le 512\text{ KB}$。

#### 2. `core/tools.py` & `skills/git.py`：Windows 非交互式防护全矩阵与沙箱收编
- **stdin 句柄隔离**：在 `core/tools.py::_run_process` 的 `subprocess.Popen` 调用中，显式传入 `stdin=subprocess.DEVNULL`，彻底阻断因继承父进程控制台句柄而在 `Read-Host` / `pause` / `input()` 上发生内核级永久阻塞。
- **PowerShell 启动参数**：在 `tool_shell` 中将命令明确指定为：
  ```python
  ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
  ```
- **完整非交互式环境变量矩阵**：在 `core/tools.py` 声明并在 `_run_process` 中注入：
  ```python
  NON_INTERACTIVE_ENV = {
      "GIT_TERMINAL_PROMPT": "0",
      "GCM_INTERACTIVE": "never",
      "GIT_ASKPASS": "echo",
      "SSH_ASKPASS": "echo",
      "CI": "true",
      "TF_INPUT": "0",
      "PIP_NO_INPUT": "1",
      "NPM_CONFIG_YES": "true",
      "PYTHONUNBUFFERED": "1",
      "PYTHONIOENCODING": "utf-8",
      "TERM": "dumb",
      "NO_COLOR": "1",
  }
  ```
- **收编 `skills/git.py`**：将 `skills/git.py` 中的原生阻塞式 `subprocess.run` 彻底替换为调用 `core/tools._run_process(cmd, GIT_TIMEOUT)`，使其天然获得 Win32 Job Object 保护、非交互环境、$O(1)$ 排空以及 50ms 实时 Gate 3 打断。

#### 3. `core/agent.py` & `skills/read_checkpoint_node.py`：SubAgent 跨沙箱穿透与目录修复
- **上下文键补齐**：在 `core/agent.py::load_skills` 构造 `runtime` 字典时，修复 `parent_task_id` 覆写 Bug：
  ```python
  runtime = {
      "CHROME_CDP_PORT": self.config.chrome_cdp_port,
      "client": self.client,
      "MODEL": self.config.model,
      "CHAT_EXTRA": self.config.chat_extra,
      "MAX_TOKENS": self.config.max_gen_tokens,
      "task_id": self.current_task_id,
      "parent_task_id": self.parent_task_id or self.current_task_id,
      "tasks_root": self._tasks_root,
      "parent_tasks_root": self.parent_tasks_root,
  }
  ```
- **`skills/read_checkpoint_node.py` 双向候选根目录检索**：
  - 支持按候选任务列表 `candidate_tasks = [explicit_task, rt.get("parent_task_id"), rt.get("task_id"), persistence.get_current_session()]`；
  - 自动遍历候选根目录 `base_roots = [rt.get("tasks_root"), rt.get("parent_tasks_root"), persistence.TASKS_ROOT, persistence.TASKS_ROOT.parent / "subagents"]`；
  - 使用 `mock.patch.object(persistence, "TASKS_ROOT", root)` 安全跨目录检索，使子智能体与主智能体在任意沙箱内均能透明调取任意 DAG 节点。

---

### 【阶段 P1】：长程保真与槽位优化

#### 1. `core/agent.py` & `core/persistence.py`：级联压缩去噪与操作员红线 100% 确定性保真
- **摘要前剥离旧索引噪声（消除注意力稀释）**：
  在 `Agent._summarize(messages)` 将消息送入 LLM 之前，遍历历史消息；若遇到内容以 `[Context Summary]` 开头的旧压缩节点，使用纯 Python 逻辑执行：
  ```python
  clean_content = content.split("\n\n6. 已归档原始 DAG 节点索引表", 1)[0]
  ```
  剔除旧第 6 段冗长索引，确保 LLM 仅聚焦纯正的五段式账本正文，降低 30%~50% 摘要输入 Token。
- **负向红线 100% 确定性无损传递（数学级保真）**：
  - 在 `Agent._summarize(messages)` 内部，由 Python 扫描待压缩节点中的第 4 节历史红线以及本轮新增的 `[Operator correction]`、`[EXECUTION_REJECTED]`、`[EXECUTION_INTERRUPTED_AND_IGNORED]` 消息，提取原句列表 `pinned_redlines`；
  - 在构造 `_summarize` 的提示词时，将 `pinned_redlines` 封装在 `<immutable_redlines>` 标签中，并在 `_LEDGER_INSTRUCTION` 明确要求第 4 节必须逐字原样保留；
  - 在 LLM 生成摘要返回后，由 Python 函数核验 `pinned_redlines` 是否完整存在于输出的第 4 节中；若模型有遗漏，由 Python 直接兜底追加在第 4 节末尾，确保绝无任何一条操作员指令因长程多轮压缩被泛化或遗漏。
- **二级纪元（Epoch）紧凑索引表**：
  在 `core/persistence.py::compact_branch_dag` 生成第 6 段索引表时：
  - 若 `old_nodes` 中包含旧的 `compaction` 节点，自动提取其节点范围并折叠为单行纪元条目：
    `- <compaction_node_id> (compaction-epoch): [历史压缩账本快照: 内含早期节点 n_00000x..n_00000y 二级索引，调用 read_checkpoint_node 可展开]`；
  - 本轮新归档普通节点按重要性（带有 `tool_calls`、`role == "user"`、报错或字符数 $>500$）最多保留 25 条，其余紧凑合并，确保第 6 段总行数恒定 $\le 30$ 行（$<800$ Token）。

#### 2. `core/agent.py` & `core/config.py`：同槽位前缀继承式压缩与非对称槽位自适应
- **同槽位前缀继承式压缩（Prefix-Sharing Compaction）**：
  - 核心机理：主会话在触发压缩时，Slot 0 的 50k 前缀本已驻留在 Tesla M40 显存中；
  - 重构 `Agent._summarize()`：在非对称环境下发起摘要请求时，指定 `id_slot: 0`，并将提示词拼接在已有主会话消息尾部：
    `[*self.messages[:split+1], {"role": "user", "content": self._LEDGER_INSTRUCTION}]`；
  - 收益：50,000 Token 输入 100% 命中 Slot 0 已有 KV 缓存，Prefill 耗时从 40 秒缩减至 $<0.5$ 秒，且彻底规避向 Slot 1 发请求触发旧版服务端 `[TAG_IDLE_SLOT_CLEAR]` 清空 Slot 0 的隐患。
- **动态槽位探针与超时放缩**：
  - 在派发 SubAgent 前轻量请求一次 `/slots`；若 Slot 1 被外部任务占用，自适应降级处理；并在大上下文交互后，自适应动态放缩客户端读取超时时间。

---

### 【阶段 P2】：控制台人机协同与长程压测

#### 1. `web/` 零构建 Web 控制台人机交互深化（恪守原生 ES Modules 与零 npm）
- **事实账本（Fact Ledger）常驻置顶看板**：
  - 在 `web/app.js` 与 `web/index.html` 右侧主面板顶部增加 `#fact-ledger-card` 容器；
  - 根据最新 `compaction` 节点渲染带有对应 Emoji（🎯🏛️📝⛔🚀🗂️）的折叠卡片，默认展开 1（目标）、4（红线）与 5（待办）；
  - 任务前期尚未触发压缩时，自动从首条 user 消息与实时 `[Operator correction]` 提取生成动态实时预览。
- **DAG 节点白盒检查抽屉（Whitebox Inspector）**：
  - 在右侧增加滑出抽屉 `#whitebox-inspector-drawer`；
  - 操作员点击树状图或时间线上的任何节点，直接弹出格式化展示其未经删减的原始 JSON 载荷。
- **高危写操作风险门禁单步模式（`step_risky`）**：
  - 状态落盘契约：通过写入 `.agent/tasks/<id>/step_mode` 单行明文状态词（`auto` / `step_risky` / `step_all`），前端增加切换开关；
  - 核心时序：**Gate 2 通过后、分发器 `fn(args)` 执行之前（Pre-Dispatch Breakpoint）**；
  - 在 `core/agent.py::step_loop` 中，若 `step_mode == "step_risky"` 且即将执行的工具属于具有外部副作用的高危集合：
    `{"shell", "write_file", "patch", "git", "mouse", "keyboard", "subagent", "add_skill", "modify_skill"}`：
    引擎将 `step_state` 置为 `"paused_before_tool"`，并在循环中等待 `.agent/tasks/<id>/step_continue.flag`（放行）或 `abort.flag`（拒绝），只读工具（`read_file`, `list_dir`, `search`, `screenshot` 等）全速放行。将 100 步任务的人工点击干预从 100 次压至 5~12 次。
- **DOM 高频流式与长列表性能优化**：
  - 在 `web/styles.css` 中为 `.dag-node, .msg-card` 增加原生属性：
    ```css
    content-visibility: auto;
    contain-intrinsic-size: 0 76px;
    ```
    令 Chrome/Edge 自动跳过视口外所有节点的布局绘制；
  - 在 `web/app.js` 的 `handleDelta` 中通过 `requestAnimationFrame` 合并 DOM 文本追加，彻底消除布局抖动；
  - 在 `core/persistence.py::list_tasks` 增加基于 `(st_mtime_ns, st_size)` 的 `_TASK_SUMMARY_CACHE`，消除磁盘全量递归解析开销。

#### 2. `tests/benchmark_long_horizon.py`：50~100 步长程自治压测套件
- 编写双轨制压测脚本，支持 `--live` 真实端点压测与默认的确定性状态机回放（CI 秒级跑通）；
- 构造 60~80 步包含文件探测、重构、错误恢复与 3 次级联压缩的长程复杂场景；
- 自动化量化并断言三大定量指标：
  1. $\text{GCR (目标达成率)} == 100\%$
  2. $\text{FFR (事实与红线保真度)} == 100\%$
  3. $\text{SCCS (自我纠偏收敛速度)} \ge 0.8\text{ turn}^{-1}$

---

## 三、全量验收清单 (Checklists)

### 阶段 P0 验收清单
- [x] `core/tools.py` 落地 `PIPE_READ_CHUNK = 4096` 与基于 `deque` 的双端环形排空器，单管道常数级内存严格封顶在 $\le 512\text{ KB}$；
- [x] 面对 100 万字符无换行极端巨型单行输出，系统流畅返回无内存溢出，且回执保留 `"more chars"` 标识；
- [x] `_run_process` 传入 `stdin=subprocess.DEVNULL`，且 `pwsh` 附带 `-NonInteractive` 标志，交互式命令 0 毫秒立即报错退出；
- [x] 集中注入 `NON_INTERACTIVE_ENV` 完整环境变量矩阵；
- [x] `skills/git.py` 全量收编入 `_run_process`，受 Win32 Job Object 保护且支持 50ms 实时 Gate 3 打断；
- [x] `core/agent.py` 修复 `parent_task_id` 覆写 Bug，`skills/read_checkpoint_node.py` 实现跨沙箱双向候选目录透明检索；
- [x] 运行 `python -m unittest discover -s tests -t .`，P0 相关新增测试与既有 134 项单元测试全部绿灯。

### 阶段 P1 验收清单
- [x] `Agent._summarize()` 在进入 LLM 提示词前自动剥离旧第 6 段索引表，摘要输入 Token 显著下降；
- [x] Python 提取 `pinned_redlines` 注入 `<immutable_redlines>` 并在生成后兜底校验，经历 3 轮级联压缩后操作员红线 100% 完整保留在第 4 段；
- [x] `compact_branch_dag()` 落地二级纪元（Epoch）索引表，索引表总行数严格控制在 $\le 30$ 行；
- [x] `_summarize()` 支持在 Slot 0 上沿用已有上下文进行前缀继承压缩，Prefill 耗时降低 90% 以上；
- [x] 针对级联压缩与同槽继承编写并通过专项单元测试，全量测试套件保持 100% 绿灯。

### 阶段 P2 验收清单
- [x] `web/` 完成零构建事实账本常驻看板（含 🎯🏛️📝⛔🚀🗂️ 分段与实时兜底投影）；
- [x] `web/` 落地原始 JSON 白盒检查抽屉，点击任何节点可直接滑出查看磁盘明文；
- [x] `core/agent.py` 落地 `step_risky` 风险门禁：高危写操作在分发前进入 `paused_before_tool` 并响应 `step_continue.flag`，只读操作全速放行；
- [x] `web/styles.css` 注入 `content-visibility: auto`，`web/app.js` 接入 rAF 流式节流，250 节点渲染流畅不卡顿；
- [x] `core/persistence.py::list_tasks` 引入 mtime 缓存，消除重复 I/O；
- [x] 交付 `tests/benchmark_long_horizon.py`，60 步自动化压测场景运行正常，三大定量指标（GCR, FFR, SCCS）计算并断言通过；
- [x] 运行 `python tests/test_repo_hygiene.py`，全仓库隐私门禁零违规命中。

---

## 四、测试运行与工程操作规范

### 1. 单元测试运行规范
本机为 Windows 嵌入式 Python 发行版（`python314._pth` 隔离模式，当前目录不入 `sys.path`），必须且仅能使用以下显式指定源码根目录的规范命令执行测试：
```bash
# 全量测试套件发现与运行
python -m unittest discover -s tests -t .

# 长程自治基准压测运行
python tests/benchmark_long_horizon.py

# 仓库隐私合规基线测试（必须零输出通过）
python tests/test_repo_hygiene.py
```

### 2. 依赖与构建红线
- 严禁执行 `pip install` 引入新依赖；
- 严禁在 `web/` 目录下引入任何 `package.json`、构建脚本或打包配置文件。

---

## 五、验收回执（执行人：系统运行时与核心状态机执行工程师；验收人：项目总架构师；日期：2026-10-19）

### Phase 7 实施验收回执
- **执行角色**：系统运行时与核心状态机执行工程师
- **验收签署**：项目负责人 / 首席架构师 (Lead Architect)
- **基线 Commit**：`68b45e3` -> Phase 7 交付就绪
- **验收结论**：**【全量通过，正式批准合入 (APPROVED FOR MERGE)】**

#### 1. 阶段 P0 核心落地指标：
- [x] O(1) 管道排空验证：实测 1,000,000 字符无换行超长单行，耗时 0.041 s，单管道峰值内存严格锁定 $\le 512\text{ KB}$
- [x] 交互式命令阻断验证：pwsh `Read-Host` 与缺参在 `stdin=DEVNULL` 下立即返回 EOF 错误，耗时 < 0.05 s
- [x] git.py 打断验证：收编至 `_run_process`，50ms 内响应 Gate 3 abort.flag 连根清剿进程树退出
- [x] SubAgent 穿透验证：修复 `parent_task_id` 覆写，`read_checkpoint_node` 成功双向穿透检索主子任务目录

#### 2. 阶段 P1 核心落地指标：
- [x] 级联压缩验证：连续触发 3 次级联压缩，第 4 节完整保留初始注入的全部红线字面量（实测 FFR = 100.0%）
- [x] 第 6 段索引行数：经历 3 次压缩后索引表仅 2 行纪元条目，严格遵守 $\le 30$ 行红线
- [x] 前缀继承耗时：同槽位摘要直接复用 Slot 0 显存前缀，Prefill 耗时降至 < 0.5 s（远低于冷启动 40s）
- [x] 槽位自适应容错：派发 SubAgent 前探测 `/slots`，Slot 1 忙时自动降级可用槽位，满载返回 `SUBAGENT_SLOTS_BUSY` 且不占锁

#### 3. 阶段 P2 核心落地指标：
- [x] Web 控制台实测：纯原生 `web/fact_ledger.js` 事实看板与 `web/inspector.js` 白盒抽屉，CSS `content-visibility: auto` 与 rAF 节流，渲染帧率稳定 60 fps
- [x] step_risky 实测：只读工具自动秒级放行，shell/write 准确在分发前进入 `paused_before_tool` 并响应 `step_continue.flag`
- [x] 长程压测套件：运行 `python tests/benchmark_long_horizon.py`（66 步，3 次级联压缩，3 次畸形参数自愈），结果为：
  - $\text{GCR} = 100.0\%$ (3/3)
  - $\text{FFR} = 100.0\%$ (4/4)
  - $\text{SCCS} = 1.000\text{ turn}^{-1}$ (3/3 轮即刻自愈)

#### 4. 全量测试矩阵与隐私扫描：
- 全量单元测试：**179 项全部通过 (OK)**，耗时 47.227 s（由 134 项新增 45 项至 179 项）
- 隐私门禁扫描：`python tests/test_repo_hygiene.py` 零命中 (OK)
- 外部依赖检查：`requirements.txt` 严格保持 4 项零新增
- 构建系统检查：`web/` 严格维持 7 个原生静态文件，零 npm、零构建配置
