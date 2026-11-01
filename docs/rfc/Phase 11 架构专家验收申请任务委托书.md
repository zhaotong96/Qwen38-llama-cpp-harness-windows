# Phase 11 工程全量交付：架构专家终审验收申请任务委托书

- **文档编号**: `RFC-20261031-PHASE11-ACCEPTANCE-REQ`
- **委托方**: 项目负责人 / 首席架构师 (Project Lead / Lead Architect)
- **受托方**: 领域架构专家与外审顾问团 (Domain Architecture Expert Reviewers)
- **评审对象**: Phase 11 全套工程交付成果 (11A 上下文 GC, 11B 网格对齐, 11C Worktree 沙箱, 11D 宏结晶)
- **基线版本**: Phase 10 (`125d2b6`) $\rightarrow$ Phase 11 Milestone Candidate
- **适用环境**: Windows 真实宿主环境 / Tesla M40 异构计算 / `Qwen3.8-27B-Uncensored-IQ4_XS.gguf` (65536 上下文)
- **文档路径说明**: 本文档面向外部专家评审，全篇严格采用**项目根目录相对路径**。

---

## 📌 一、委托背景与评审目的

### 1.1 工程背景
在基于混合循环神经网络（DeltaNet / RWKV 变体层与注意力层混合架构）的大模型本地轻量部署场景中，长程任务执行普遍遭遇三大物理与系统级瓶颈：
1. **上下文无序膨胀与信噪比退化**：长程试错、工具报错与重试日志迅速消耗稀缺的上下文窗口，分散模型注意力；
2. **DeltaNet 循环状态缓存未命中**：宿主推理服务器（`llama-server`）基于 1024-token 周期对循环网络隐状态进行快照（`--checkpoint-min-step 1024`）。任意非对齐的提示词增量均会导致隐状态快照无法命中物理网格，退化为昂贵的全量预填充（Cold Prefill）；
3. **多智能体并发污染与幻觉风险**：传统子智能体直接在主工作区施工，任务失败或进程崩溃容易产生脏数据；且子智能体常产生“删除测试断言”或“注入 skip”的伪造绿灯欺骗行为；
4. **长链桌面操作高 Token 消耗与 UI 漂移脆弱性**：高频机械性桌面交互每步消耗数百 Tokens，且极易因 Windows 窗口尺寸、动态布局抖动而失败崩溃。

### 1.2 委托评审目的
项目团队已完成 Phase 11 四大专项工程（11A~11D）的开发、集成与回归测试，单元测试套件由基线 337 项扩充至 393 项（100% 通过）。
现正式委托领域专家对交付成果进行**深度穿透式架构评审与验收核定**，重点评估：
1. **形式化逻辑与因果一致性**：上下文剪枝与宏编译是否严格保持因果单调性与可追溯性；
2. **系统级鲁棒性与边界防御**：网格对齐降级、子智能体 5 类静态破坏拦截与断点热交接是否严密无漏洞；
3. **架构红线恪守度**：是否严格保持零额外依赖、零前端打包与 UNIX-like 透明存储不变性；
4. **Phase 12 战略衔接度**：本阶段成果是否坚实支撑下一阶段 7×24h 具身常驻中枢与事件驱动演进。

---

## 🗺️ 二、交付资产索引与代码地图（相对路径）

全部核心代码与测试均使用纯 Python 3.10+ 标准库实现，严格遵循无构建、无第三方膨胀设计原则：

```
Qwen38-llama-cpp-harness-windows/
├── core/
│   ├── context.py              # [11A] 上下文尾端 GC (Turn Span 原子成对剪枝、因果徽标)
│   ├── token_grid.py           # [11B] DeltaNet 1024-token 网格对齐、SHA-1 缓存与四态遥测
│   ├── subagent_gate.py        # [11C] Git Worktree 物理沙箱隔离、防篡改卡片与五类 HAZARD 拦截
│   ├── macro_compiler.py       # [11D] 因果主干提纯、污点槽位参数化、AST 纯净生成与断点交接
│   ├── agent.py                # 核心运行时主循环 (挂载 11A GC、11B 网格元数据、11C 门禁)
│   ├── persistence.py          # Git 风格 DAG 任务树与不可变只追加 Checkpoint
│   ├── skill_sandbox.py        # 技能 AST 纯度与运行时沙箱
│   └── subagent_runner.py      # 子智能体物理宿主执行器
├── skills/
│   ├── subagent_review.py      # [11C] 父智能体对立审阅与 fast-forward 合并/回滚技能
│   └── crystallize_macro.py    # [11D] 零 Token 确定性宏技能结晶命令
├── tests/
│   ├── test_context_gc.py      # [11A] 11 项专项测试 (原子跨度、受保护标记、帕累托阈值)
│   ├── test_token_grid.py      # [11B] 15 项专项测试 (零语义垫片、离线降级、四态遥测、重播种)
│   ├── test_subagent_gate.py   # [11C] 17 项专项测试 (物理 worktree、5 类 hazard、对立反事实)
│   ├── test_macro_compiler.py  # [11D] 13 项专项测试 (因果提纯、污点槽位、AST 门禁、断点热交接)
│   ├── test_subagent.py        # 子智能体集成回归测试
│   ├── test_skill_sandbox.py   # 技能沙箱 18 项纯度测试
│   └── test_repo_hygiene.py    # 仓库整洁度与隐私泄露门禁
├── docs/
│   ├── SPEC.md                 # 核心架构规约 (已更新 Phase 11 模块与文件契约)
│   ├── tasks/                  # 四份 Phase 11 详尽工程任务书 (含验收门禁与证据链)
│   │   ├── TASK_DEV_PHASE11A_CONTEXT_GC.md
│   │   ├── TASK_DEV_PHASE11B_TOKEN_GRID.md
│   │   ├── TASK_DEV_PHASE11C_SUBAGENT_WORKTREE.md
│   │   └── TASK_DEV_PHASE11D_MACRO_CRYSTALLIZATION.md
│   └── notes/report.md         # Phase 11 完工与实测综合技术报告
├── report.md                   # 根目录快速交付汇总报告
└── requirements.txt            # 严格锁定的 4 项依赖 (保持零变动)
```

---

## 🔍 三、重点评审议题与专家核验指南

### 议题 1：Phase 11A 上下文尾端自愈 GC（`core/context.py`）
- **核心实现机制**：
  - `ContextManager.prune_resolved_trial_tail(max_tail_tokens=3072, min_error_tokens=256)`
  - **Turn Span 原子保真**：将 `assistant` 决策与后续全部 `tool` 回执绑定为原子块，剪枝时成对移除，绝不残留单侧孤儿消息；
  - **5 类绝对豁免标记**：`[Operator]`, `[EXECUTION_REJECTED]`, `[EXECUTION_INTERRUPTED]`, `[Branch Rewind Lesson]`, `<immutable_redlines>` 绝对不剪；
  - **因果结晶徽标**：在胜出回合的首个 tool 回执注入 `[Transient Attempt Pruned: N prior failed attempt(s) (...) resolved via tool (DAG nodes M..N)]`；
  - **内存投影 vs 磁盘保真**：剪枝仅作用于上下文内存视窗，底层的 `.agent/tasks/<id>/checkpoint.jsonl` 保持严格只追加（Append-Only）只读保真。
- **请专家重点核验**：
  1. Turn Span 划分算法在多工具并行调用（Parallel Tool Calls）与部分失败时的完备性；
  2. 因果徽标的信息密度是否足以让 LLM 意识到“此路之前试错已通”，避免在后续回合中发生循环退化。

### 议题 2：Phase 11B DeltaNet 1024-Token 网格对齐与遥测（`core/token_grid.py`）
- **核心实现机制**：
  - `align_skeleton_block_to_grid(block_text, prior_tokens, grid_step=1024, max_pad_tokens=64)`；
  - **零语义结构垫片**：严格禁用带语义偏置的 HTML 注释（如 `<!-- pad -->`），仅使用 Markdown 格式水平分割线 `\n---`，在 Qwen 词表中稳定对应 2 个格式标记；
  - **安全垫片上限**：若到下一网格的空隙超过 64 tokens，则放弃强行对齐，避免无谓填充；
  - **`/tokenize` 毫秒级降级**：带 SHA-1 结果缓存与 300ms 超时限制，若服务端不可达则平滑降级至 CJK 加权估算器；
  - **四态遥测状态机**：审计推理响应的 `timings.cache_n`，判定 `RESONANT` / `COLD_RESET_DETECTED` / `CHECKPOINT_GAP_DRIFT` / `UNKNOWN`，并搭载 4-gram 重复率重播种熔断机制。
- **请专家重点核验**：
  1. `\n---` 垫片在长程多轮对话中是否会被模型误解析为意图切分或正文转折；
  2. 混合循环网络在状态快照边界附近的注意力衰减特性与本方案的几何契合度。

### 议题 3：Phase 11C SubAgent Git Worktree 物理隔离沙箱（`core/subagent_gate.py`）
- **核心实现机制**：
  - `create_subagent_worktree(subagent_id)`：在 `.agent/worktrees/<id>` 基于共享对象库秒级创建独立分支 `subtask_<id>`；
  - **防篡改交付卡片**：子智能体退出时，由 Python 宿主进程确定性生成 `artifact_card.json`（含变更文件列表、SHA-256 哈希、测试输出回执），绝不依赖子智能体自主陈述；
  - **五类 HAZARD 静态拦截**：
    - `HAZARD_TEST_ASSERTION_REMOVED`：检测删除 `def test_` 或 `assert` 语句；
    - `HAZARD_TEST_SKIP_INJECTED`：检测注入 `@skip`、`.skipTest` 或空 `pass` 存根；
    - `HAZARD_UNVERIFIED_CODE_EDIT`：检测修改业务代码后缺乏通过的测试记录；
    - `HAZARD_REDLINE_FILE_MODIFIED`：检测触碰核心架构红线文件；
    - `HAZARD_EMPTY_OR_NOOP_DIFF`：检测空提交与无效交付；
  - **对立审阅与回滚**：通过 `skills/subagent_review.py` 由父智能体实施 `--ff-only` 原子合并，或彻底销毁并在事实账本中沉淀 `[Branch Rewind Lesson]`。
- **请专家重点核验**：
  1. 五类 HAZARD 规则的正则表达式是否能有效抵御针对性的代码混淆与对抗绕过；
  2. Windows 环境下 Git Worktree 频繁增删的文件句柄释放与锁机制在极端并发下的稳定性。

### 议题 4：Phase 11D 0-Token 宏结晶编译器（`core/macro_compiler.py`）
- **核心实现机制**：
  - `compile_and_deploy_macro(task_id, macro_name, description)`；
  - **因果主干提纯**：自动剔除失败重试节点及纯观察探针（`screenshot`, `pic_zoom`, `ui_tree`），并将 `grounding + mouse` 动作对折叠为空间位置不变量 `uia_click`；
  - **污点槽位参数化**：基于用户原始 Goal 提取动态实体（ISO 日期、文件路径、引号字面量、数值常量），自动替换为 `$slot:<name>` 可变参数；
  - **AST 纯净度强制保证**：顶层仅允许常量赋值与纯函数声明，零执行期副作用代码，通过 `test_skill_sandbox.py` 严格校验；
  - **断点热交接机制**：执行遇到 UI 变动时返回结构化 `[MACRO_STALE_HANDOVER]`，由 LLM 就地接管剩余步骤。
- **请专家重点核验**：
  1. 污点传播算法在处理嵌套格式或自然语言模糊引用时的健壮性；
  2. 宏执行失败后的 `[MACRO_STALE_HANDOVER]` 状态恢复点与 DAG 任务树的对齐精度。

---

## ⚖️ 四、架构红线与系统不变性审查基准

评审专家需对照以下项目既定架构不变性进行一票否决制核验：

| 架构不变性 | 规约要求 | 实际代码核验路径 | 判定标准 |
| :--- | :--- | :--- | :--- |
| **依赖纯净性** | 锁定 4 项核心依赖，严禁任何第三方膨胀 | `requirements.txt` | 必须严格保持 4 项依赖不变 |
| **原生 Web 零构建** | 原生 ES Modules，绝对禁止 Webpack/Vite/Babel 构建工具链 | `web/` | 零变动，零 package.json 引入 |
| **万物皆文件透明度** | 所有状态、日志与拓扑在文件系统透明可读，支持跨机器冷迁移 | `.agent/tasks/` | 纯 JSON Lines 与标准 Markdown 沉淀 |
| **代码与运行整洁度** | 绝对禁止任何私网网段、硬编码主机 IP 字面量泄露 | `tests/test_repo_hygiene.py` | 100% 绿灯通过，零 IP 泄露 |

---

## 📝 五、专家评审意见反馈表模版

请领域专家依据上述代码与规范，在独立评审后填写下表并反馈：

```markdown
# 领域专家 Phase 11 工程终审验收评审意见书

### 1. 综合评审结论
- [ ] **正式通过 (ACCEPTED)**：实现完全满足预期，具备高可靠性，同意并入主干并启动 Phase 12。
- [ ] **有条件通过 (CONDITIONALLY ACCEPTED)**：主体架构合格，需针对特定边缘情况进行补丁修复。
- [ ] **驳回重构 (REJECTED)**：存在严重破坏系统不变性或形式化一致性的缺陷，需退回重构。

### 2. 四大模块独立评审评分 (0 - 100)
- **Phase 11A (上下文尾端 GC)**: _____ 分 | 核心评价: ____________________
- **Phase 11B (Token 网格对齐)**: _____ 分 | 核心评价: ____________________
- **Phase 11C (Worktree 物理沙箱)**: _____ 分 | 核心评价: ____________________
- **Phase 11D (宏结晶编译器)**: _____ 分 | 核心评价: ____________________

### 3. 隐性缺陷、边界漏洞与风险提示
1. [漏洞/风险 1]: _________________________________________________
2. [漏洞/风险 2]: _________________________________________________

### 4. 对 Phase 12 (7×24h 具身常驻中枢) 的战略演进建议
____________________________________________________________________
```

---

**任务委托签发**: 项目负责人 (Project Lead)  
**签发日期**: 2026-10-31  
**工程交付状态**: `393 Passed, 0 Failed, Working Tree Clean`
