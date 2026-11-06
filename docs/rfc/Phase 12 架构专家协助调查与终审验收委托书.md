# Phase 12 工程全量交付：架构专家协助调查与终审验收委托书

- **文档编号**: `RFC-20261102-PHASE12-INVESTIGATION-ACCEPTANCE-REQ`
- **委托方**: 项目负责人 / 首席架构师 (Project Lead / Lead Architect)
- **受托方**: 领域架构专家与外审顾问团 (Domain Architecture Expert Reviewers)
- **协查与验收对象**: Phase 12 全套工程交付成果（12A 事件总线与单飞守护、12B Slot Sidecar 与四级 Saga、12C 三阶接力流水线、12D 宏健康账本与自愈重结晶）
- **基线版本**: Phase 11 Milestone ([`81412dc`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/81412dc)) $\rightarrow$ Phase 12 Milestone Candidate
- **适用环境**: Windows 真实宿主环境 / Tesla M40 异构计算 / `Qwen3.8-27B-Uncensored-IQ4_XS.gguf` (65536 上下文)
- **路径规范说明**: 本文档面向外部专家评审，全篇严格采用**项目根目录相对路径**（领域专家离线与独立沙箱可读）。

---

## 📌 一、委托背景与协查目的

### 1.1 工程演进背景
在 Phase 11 圆满完成“上下文 GC、DeltaNet 1024-Token 网格对齐、Worktree 沙箱门禁与宏结晶”四大认知自治基石后，系统由短程单任务执行器正式迈向 **7×24h 具身常驻中枢与事件驱动自主演进系统**。

在长时间长程具身运行（Embodied Long-Horizon Operations）与真实复杂桌面交互中，系统面临四类深水区系统级挑战：
1. **外部事件异步涌入与单飞原子性矛盾**：外部监控（文件变动、定时调度、人工干预打断）随时可能发生，如何在无常驻黑盒数据库、万物皆文件的约束下，建立微秒级因果全序、同键防抖、且绝对杜绝并发冲刷 Slot 0 缓存的单飞常驻中枢；
2. **推理端点网络抖动与长文本冷启动高昂代价**：Tesla M40 远程宿主在多轮对话积累万级别 Token 后，一旦遭遇 OOM 重启或网络拒连（WinError 10061），重新 Prefill 耗时数分钟。亟需在网格线捕获隐藏状态并具备指数退避自愈与秒级热恢复能力；
3. **长事务失败状态污染与不可逆动作边界**：任务中途失败或分支回溯时，文件写入、代码修改与 Shell 外部副作用交织，缺乏严格的可逆性数学代数会导致脏状态残留；
4. **子智能体能力失衡与单点信任欺骗**：单个子智能体集“调研、编码、自测”于一身，极易产生伪造测试绿灯、隐藏风险的黑盒行为；必须从制度与机制上实现分权制衡（Scout $\rightarrow$ Builder $\rightarrow$ Verifier）；
5. **UI 动态漂移导致的宏技能失效与退化**：桌面应用更新或渲染延迟使既有 0-Token 宏步骤断裂，若完全从第一步重新规划会导致巨大 Token 浪费；若盲目重试会导致账本雪崩。

### 1.2 委托协查与评审目的
项目团队已完成 Phase 12 四大专项工程（12A~12D）的落地与自测，项目负责人已完成初审复验，全量单元测试套件由 395 项扩充至 **456 项（32 个测试套件，100% 绿灯通过）**。

现正式委托领域架构专家协助对交付成果开展**穿透式架构协查与终审验收**，重点聚焦以下核心命题：
1. **并发与状态代数严格性**：协查基于文件的事件总线（Event Bus）全序出队、单飞排他锁与四级 Saga 可逆代数在形式化逻辑上的完备性；
2. **分权流水线与防篡改安全性**：协查三阶接力（Scout $\rightarrow$ Builder $\rightarrow$ Verifier）的角色权限硬隔离，以及宿主签发的 `verification_ticket.json` 在防伪验签上的抗攻击能力；
3. **具身自愈与自主演进自洽性**：协查 Slot 0 状态秒级热恢复与断点接管后 `TASK_DONE` 自动重结晶的因果闭环，验证是否存在递归自引用或状态漂移风险；
4. **架构不变性恪守度**：严格核验是否坚守零新增第三方依赖（纯标准库）、原生零构建 Web 控制台、UNIX-like 透明落盘以及代码合规零泄露红线。

---

## 🗺️ 二、交付资产索引与代码地图（相对路径）

全部 Phase 12 核心代码、适配层与专项测试套件均基于纯 Python 3.10+ 标准库实现，目录拓扑映射如下：

```
Qwen38-llama-cpp-harness-windows/
├── core/
│   ├── event_bus.py            # [12A 新增] 事件驱动文件总线、优先级全序队列与单飞常驻守护仲裁
│   ├── slot_sidecar.py         # [12B 新增] 推理端点状态 Sidecar、Slot 0 快照持久化与四态自愈探活
│   ├── saga_compensator.py     # [12B 新增] 四级可逆性 Saga 代数、只追加审计日志与机械事务回滚
│   ├── subagent_runner.py      # [12C 扩展] 三阶角色矩阵（Scout只读桩/Builder沙箱/Verifier审计桩）
│   ├── subagent_gate.py        # [12C 扩展] 防篡改验签门禁、SHA-256 自校验票据与 --ff-only 终裁
│   ├── macro_compiler.py       # [12D 扩展] 截尾回执物理态捕获、反递归主干提纯与覆盖重结晶
│   ├── skill_sandbox.py        # [12D 扩展] 宏技能三振健康账本 (.agent/skill_health.json)
│   ├── token_grid.py           # [12A 优化] 4096 条目有界 LRU 缓存改造 (OrderedDict 淘汰防泄漏)
│   ├── agent.py                # [12 全局集成] 守护循环 run_daemon、紧急打断嗅探与自动热重结晶
│   ├── context.py              # [12D 适配] [MACRO_STALE_HANDOVER] 标记纳入 GC 绝对豁免集
│   └── logger.py               # [负责人加固] 控制台 sys.stdout UTF-8 编码重配置 (Windows cp1252 免疫)
├── skills/
│   └── subagent_relay.py       # [12C 新增] 三阶多角色接力编排与 finalize 合流原子技能 (SPEC §4)
├── tests/
│   ├── test_event_bus.py       # [12A 专项] 14 项测试 (信封命名、全序升序、防抖合并、LRU淘汰、单飞锁)
│   ├── test_slot_sidecar.py    # [12B 专项] 15 项测试 (Slot0保存恢复、SHA-256双路径、四态探活、Saga补偿)
│   ├── test_subagent_relay.py  # [12C 专项] 18 项测试 (Scout越权拦截、Worktree隔离、验签防篡改、流水线串联)
│   ├── test_macro_evolution.py # [12D 专项] 14 项测试 (截尾回执、三振降级、TASK_DONE自愈、并发无锁死)
│   ├── test_skill_sandbox.py   # 技能沙箱 18 项 AST 纯度与准入测试 (全部绿灯)
│   └── test_repo_hygiene.py    # 仓库整洁度与隐私泄露门禁测试 (0 敏感命中)
├── docs/
│   ├── SPEC.md                 # 核心系统规格说明 (已同步 Phase 12 模块架构与文件系统契约)
│   ├── tasks/                  # 四份 Phase 12 生产实施任务书 (全部已勾选闭环)
│   │   ├── TASK_DEV_PHASE12A_EVENT_BUS.md
│   │   ├── TASK_DEV_PHASE12B_SLOT_SIDECAR.md
│   │   ├── TASK_DEV_PHASE12C_SUBAGENT_RELAY.md
│   │   └── TASK_DEV_PHASE12D_MACRO_EVOLUTION.md
│   ├── rfc/
│   │   ├── Phase 12 架构专家咨询方案.md              # 专家前期指导蓝图
│   │   └── Phase 12 全套工程交付终审验收裁决报告.md  # 项目负责人内部终审报告
│   └── notes/report.md         # Phase 12 完工与实测综合技术报告 (已完成脱敏与归档)
├── README.md                   # 根目录架构总览 (已同步 32 测试套件 / 456 项测试)
└── requirements.txt            # 运行时依赖清单 (严格保持 4 项零改动)
```

---

## 🔍 三、四大专项协查议题与专家核验指南

### 协查议题 1：Phase 12A 事件驱动文件总线与单飞守护（`core/event_bus.py`）
- **实现机制**：
  - **信封协议与全序队列**：外部事件以明文 JSON 固化于 `.agent/inbox/<YYYYMMDD_HHMMSS_ffffff>_<priority>_<8hex>.json`。出队遵循 `(priority, 纳秒时间戳, event_id)` 全序升序。出队过程原子移动至 `.agent/inbox/processed/` 审计归档；
  - **高频防抖折叠**：`emit_event(..., debounce_key="...", debounce_window_seconds=1.0)` 扫描未决信封，在窗口期内原地更新 payload 并累加 `merge_count`，避免无谓 I/O；
  - **Level-0 紧急打断**：`Agent._check_event_bus_emergency` 在每个 ReAct 回合边界窥视总线，发现 `priority=0` 且 `abort_current=true` 时立即出队，经既有 `set_abort_flag` 优雅停止当前任务并落盘 Checkpoint，无粗暴硬杀；
  - **单飞互斥仲裁**：通过 `os.O_CREAT | os.O_EXCL` 创建 `.agent/daemon_flight.lock`。跨进程/跨线程单飞保护；支持基于文件 mtime 的 `break_stale_flight_lock`（默认超期 6 小时自动破锁回收）；
  - **有界 LRU 缓存**：`core/token_grid.py` 的 SHA-1 Token 记忆化缓存升级为 `MAX_TOKEN_CACHE_SIZE=4096` 的 `OrderedDict`，命中 `move_to_end`，超额 `popitem(last=False)`。
- **请专家重点协查**：
  1. 纯文件系统总线在 Windows NTFS 下的高频 `rename`（`replace`）操作在高吞吐场景是否存在句柄延迟释放竞争；
  2. 紧急打断在回合间隙（Step Boundary）注入与 OpenAI Tool Call 多工具回执交错时的状态一致性。

### 协查议题 2：Phase 12B 推理端点状态 Sidecar 与四级 Saga 热恢复（`core/slot_sidecar.py`, `core/saga_compensator.py`）
- **实现机制**：
  - **Slot 0 状态秒级热恢复**：当模型遥测处于 `RESONANT` 且前缀恰为 1024 整数倍网格线时，调用 `POST /slots/0?action=save`。快照元数据（含 SHA-256、Token 数、前缀摘要）持久化至 `.agent/slots/manifest.json`。重启后发起 `restore` 前执行文件存在性、清单状态、SHA-256 三重校验，拒绝任何篡改与网络空耗；
  - **心跳四态探活与自愈**：`probe_endpoint_health` 映射 200 $\rightarrow$ `HEALTHY`、502/503/504/超时 $\rightarrow$ `OOM_RESTARTING`、10061/拒连 $\rightarrow$ `REFUSED`。在断线时按 $(1.0s, 2.0s, 4.0s)$ 指数退避重试，连接恢复后自动触发 Slot 0 热恢复；
  - **四级可逆性代数模型**：
    - **Level 0（纯只读探针）**：`read_file`, `list_dir`, `screenshot`, `ui_tree`, `grounding` 等，逆动作恒为空集 $\emptyset$；
    - **Level 1（瞬态逻辑态）**：窗口焦点、鼠标键盘指针、Git 分支引用，通过上下文重置进行逻辑回滚；
    - **Level 2（可机械补偿写）**：`write_file`, `patch`, `add_skill`, `modify_skill`。写操作前原子捕获前置态（$\le 1\text{MB}$ 全文备份与 SHA-256）。逆动作确定性回滚：新建文件执行 `unlink`，修改文件覆写还原；
    - **Level 3（物理不可逆动作）**：`shell` 命令以及对核心红线文件（`requirements.txt`, `docs/SPEC.md`, `package.json`）的修改。遭遇 Level 3 时，**回滚强行挂起**，生成带唯一时间戳的人工确认工单 `.agent/tasks/<id>/saga_confirmation_<ts>.json`，严禁任何越权静默猜测。
- **请专家重点协查**：
  1. 四级可逆性代数在处理“修改不存在文件”或“跨目录软链接”时的边界安全性；
  2. 远端宿主快照与本地快照文件哈希在跨机网络不透明场景下的可靠性保证。

### 协查议题 3：Phase 12C 三阶多角色子智能体接力流水线（`core/subagent_runner.py`, `core/subagent_gate.py`, `skills/subagent_relay.py`）
- **实现机制**：
  - **分权制衡流水线架构**：
    - **Scout（侦察员）**：严格白名单只读工具。写操作被宿主拦截抛出 `[SECURITY_VIOLATION]`；输出侦察报告并吸附父分支最近的 `ckpt_aligned=True` 网格节点，实现后续阶段 KV Cache 零重算前缀复用；
    - **Builder（施工员）**：强制分配独立的 Git Worktree 沙箱（`.agent/worktrees/<id>`），写工具与 Shell 运行工作目录严格锁定在沙箱内，主工作区保持绝对干净（`git status --porcelain` 零污染）；
    - **Verifier（审核员）**：只读挂载 Builder 工作树，执行目标测试套件并运行 5 类静态破坏扫描（删断言、加 skip、未测修改、触碰红线、空提交）；
  - **防篡改票据签名机制**：Verifier 收尾时由宿主确定性签发 `verification_ticket.json`。核心字段经规范化排序计算 `payload_sha256`。`verdict` 只有在 `exit_code == 0` 且 `hazards == []` 时才签发 `VERIFIED_READY_TO_MERGE`，否则强制签发 `REJECTED_VERIFICATION_FAILED`；
  - **父级原子终裁**：父智能体调用 `subagent_relay(action='finalize')` 时，宿主核验票据签名完整性。仅合规验签通过才执行 `git merge --ff-only` 并销毁沙箱；被拒绝时强制销毁沙箱并在事实账本第 4 节固化 `[Branch Rewind Lesson]`。
- **请专家重点协查**：
  1. `_canonical_ticket_digest` 验签机制在防范第三方恶意篡改方面的密码学强度；
  2. Windows 下长路径与未完全关闭的子进程句柄对 `git worktree remove` 的潜在干扰。

### 协查议题 4：Phase 12D 宏健康度账本与自主热重结晶自愈（`core/macro_compiler.py`, `core/skill_sandbox.py`, `core/agent.py`）
- **实现机制**：
  - **截尾回执物理态捕获**：UI 发生变更导致宏技能某步失败时，宏模块绝不抛出异常崩溃，而是返回结构化 JSON：
    ```json
    {
      "ok": false,
      "status": "MACRO_STALE_AT_STEP",
      "macro_name": "macro_export_report",
      "completed_steps_count": 2,
      "failed_step_index": 3,
      "failed_step_op": "uia_click",
      "failed_target": "导出为PDF",
      "window_title": "报表生成器 v2.0",
      "error_snapshot": "grounding miss for target '导出为PDF'...",
      "directive": "[MACRO_STALE_HANDOVER] Steps 0..1 already executed on screen. Step 3 ('导出为PDF') failed due to UI change. Please take over from Step 3."
    }
    ```
  - **GC 绝对豁免保护**：`core/context.py` 将 `[MACRO_STALE_HANDOVER]` 纳入 `_PROTECTED_MARKERS`，保证 Phase 11A 尾端 GC 绝不折叠该断点物理事实；
  - **健康度三振账本**：在 `.agent/skill_health.json` 的 `skills` 命名空间下独立记分。执行成功立即清零；连续 3 次失败标记 `DEGRADED_NEEDS_HEALING`；受共享 `_HEALTH_LOCK` 保护，支持多线程高并发；
  - **任务收敛点自动热重结晶**：主模型接管并成功执行 `task_done` 时，触发 `_maybe_autorecrystallize_stale_macro`。系统提取愈合后的 Winning Spine，以 `overwrite=True` 重新编译部署宏技能（经历 AST 顶层零 Call、Schema 校验与独立进程干运行三闸检验），热加载技能并复位账本，写入 DAG `autonomous_healing` 元数据。**旧宏步骤被判定为失败回合，绝不递归自引用进新宏**。
- **请专家重点协查**：
  1. `extract_winning_spine` 提纯机制在混合多轮报错接管时的因果去重准确率；
  2. 自动覆盖重结晶在极端高频写入时与本地技能沙箱热重载的线程安全性。

---

## ⚖️ 四、架构红线与系统不变性审查基准

评审专家需对照以下项目既定四大架构不变性进行“一票否决制”核验：

| 架构不变性 | 规约要求 | 实际代码核验路径 | 判定标准 |
| :--- | :--- | :--- | :--- |
| **依赖纯净性** | 锁定 4 项核心基础依赖，严禁任何第三方膨胀 | `requirements.txt` | 必须保持 `openai`, `mss`, `pillow`, `websocket-client` 4 项不变 |
| **原生 Web 零构建** | 原生 ES Modules，绝对禁止 Node/Webpack/Vite 构建链 | `web/` | 零改动，零 `package.json`，纯原生浏览器运行 |
| **万物皆文件透明度** | 所有状态、事件、快照、账本均为文件系统明文，严禁黑盒 DB | `.agent/` | `.agent/inbox/`, `.agent/slots/`, `.agent/skill_health.json` 透明可读 |
| **单飞执行约束** | 物理互斥，同一时刻全系统仅允许单个任务派发 | `core/event_bus.py`, `core/subagent_runner.py` | `daemon_flight.lock` 与 `active.lock` 100% 互斥生效应 |
| **代码与运行整洁度** | 严禁任何私网 IP、内网域名、本地绝对路径字面量泄露 | `tests/test_repo_hygiene.py` | 100% 绿灯，零私网信息与绝对链接泄露 |

---

## 🧪 五、全量实测与质量门禁证据链

1. **全量单元测试与回归套件**：
   - 运行命令：`python -m unittest discover -s tests -t .`
   - 测试结果：**Ran 456 tests in 106.963s — OK**（0 失败、0 错误）；
   - 测试套件分布：
     * 既有基线套件：28 个套件（395 项测试，100% 保持通过）；
     * Phase 12A 专项：[`tests/test_event_bus.py`](../../tests/test_event_bus.py)（14 项测试）；
     * Phase 12B 专项：[`tests/test_slot_sidecar.py`](../../tests/test_slot_sidecar.py)（15 项测试）；
     * Phase 12C 专项：[`tests/test_subagent_relay.py`](../../tests/test_subagent_relay.py)（18 项测试）；
     * Phase 12D 专项：[`tests/test_macro_evolution.py`](../../tests/test_macro_evolution.py)（14 项测试）；
     * 净增测试：**61 项测试**（总数达 456 项，显著超越任务书 $\ge 451$ 目标）。
2. **代码卫生与脱敏合规性**：
   - 运行命令：`python -m unittest discover -s tests -t . -p test_repo_hygiene.py`
   - 测试结果：**1 test, OK（0 违规命中）**。全工作树无任何私有 IP、敏感账号、内部路径字面量。
3. **技能沙箱纯度门禁**：
   - 运行命令：`python -m unittest discover -s tests -t . -p test_skill_sandbox.py`
   - 测试结果：**18 tests, OK**。AST 顶层零 Call、三闸准入与三振隔离机制完好无损。
4. **控制台编码加固验证**：
   - 在 Windows cp1252 默认控制台环境下，经由 [`core/logger.py`](../../core/logger.py) 的 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` 加固，彻底消除了日志记录中文字符时的 `charmap` 编码警告。

---

## 📝 六、专家协查意见与终审验收反馈表模版

请领域架构专家在独立穿透审查后，填写下表并反馈：

```markdown
# 领域专家 Phase 12 工程终审验收与协查评审意见书

### 1. 综合评审结论
- [ ] **正式通过 (ACCEPTED)**：架构设计高超，456 项测试全绿，严格坚守四大架构不变性，同意签发 Phase 12 里程碑并推送到 GitHub 远端。
- [ ] **有条件通过 (CONDITIONALLY ACCEPTED)**：主体设计优良，需针对个别边缘边界补充测试或加固。
- [ ] **驳回重构 (REJECTED)**：存在严重破坏单飞不变性、状态不一致或安全沙箱穿透隐患，退回重构。

### 2. 四大核心模块独立评审评分 (0 - 100)
- **Phase 12A (事件驱动文件总线与单飞守护)**: _____ 分 | 评价: ____________________
- **Phase 12B (Slot Sidecar 与四级 Saga)**: _____ 分 | 评价: ____________________
- **Phase 12C (三阶多角色接力流水线)**: _____ 分 | 评价: ____________________
- **Phase 12D (宏健康账本与自主重结晶)**: _____ 分 | 评价: ____________________

### 3. 隐性缺陷、极端边界与系统级风险提示
1. [协查项 1]: _________________________________________________
2. [协查项 2]: _________________________________________________

### 4. 面向未来生产级常驻具身运行的演进建议
____________________________________________________________________
```

---

**任务委托签发**: 项目负责人 (Project Lead / Lead Architect)  
**签发日期**: 2026-11-02  
**当前代码库状态**: `456 Tests OK (0 Failures, 0 Errors), Working Tree Clean, Ready for Commit`
