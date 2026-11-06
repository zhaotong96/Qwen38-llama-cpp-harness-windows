# Phase 12C 工程任务书：三阶角色子智能体接力流水线 (Scout-Builder-Verifier Relay Pipeline)

- **任务编号**: `TASK-DEV-PHASE12C-SUBAGENT-RELAY`
- **指派对象**: 执行工程师 C (Execution Engineer C)
- **所属阶段**: Phase 12 (7×24h 具身常驻中枢与事件驱动自主演进)
- **基线版本**: Phase 11 Milestone (`81412dc`)
- **前置依赖**: Phase 11C SubAgent Git Worktree 沙箱、Phase 11B 网格对齐锚点
- **文档路径规范**: 本任务书全篇采用**项目根目录相对路径**，严禁引入本机绝对路径。

---

## 🎯 一、任务背景与工程定位

在 Phase 11C 中，我们建立了基于 Git Worktree 的物理沙箱与宿主防篡改交付卡片，初步解决了子智能体对主工作区的直接破坏与污染问题。

然而，在复杂常驻具身任务中，若仍然采用单一子智能体“从头包揽到尾（边调研、边修改、边验证）”的单体模式，仍存在三大隐性痛点：
1. **认知负荷过载与上下文膨胀**：子智能体在调研源码或排查 UI 时产生大量只读日志（`grep`、`read_file`、`ui_tree`），这些日志与后续的补丁代码混合在一个上下文中，导致推理注意力分散并过早耗尽上下文；
2. **“既当裁判又当运动员”的道德风险**：大模型在遭遇测试失败时，容易产生逆向诱导（删除报错测试断言或注入 `@unittest.skip` 存根以强行制造绿灯）；
3. **缓存复用中断**：单体子智能体如果直接从零开始推理，无法继承父智能体在前序调研中已经建立的前缀缓存。

本任务的核心工程定位是**实装“三阶多角色分权制衡接力流水线（Scout-Builder-Verifier Relay Pipeline）”**：
1. **Scout (侦察兵)**：以只读权限分析代码拓扑与影响范围，在跨越 1024 物理网格处固化带有 `ckpt_aligned=True` 的对齐上下文锚点；
2. **Builder (建造者)**：从 Scout 锚点分叉，在独立的 `.agent/worktrees/<id>` 沙箱中专心执行代码/宏合成，享受零重算前缀复用；
3. **Verifier (验证官)**：以独立沙箱挂载 Builder 的产物，独立运行测试套件、AST 纯度检测与五类静态 HAZARD 审计，确定性签发防篡改 `verification_ticket.json`；
4. **Parent 裁决**：父智能体仅依据 Verifier 的独立签章与统一 Diff 执行 `--ff-only` 原子合并或彻底回滚。

---

## 📐 二、技术契约与架构规约

### 2.1 角色职责与权限隔离矩阵

```
                ┌──────────────────────────────────────────────┐
                │             Parent Main Agent                │
                └───────┬──────────────────────────────▲───────┘
                        │                              │
         1. 启动接力任务│                              │ 4. 依据独立签章
                        ▼                              │    执行 --ff-only 合并
                ┌──────────────┐                       │
                │ Scout 侦察兵 │                       │
                └───────┬──────┘                       │
                        │ 2. 交付调研报告与            │
                        │    对齐上下文锚点            │
                        ▼                              │
                ┌──────────────┐                       │
                │Builder 建造者│                       │
                └───────┬──────┘                       │
                        │ 3. 交付 Worktree 隔离产物    │
                        ▼                              │
                ┌──────────────┐                       │
                │Verifier 验证官───────────────────────┘
                └──────────────┘
```

| 角色标识 | 运行时工作目录 | 工具权限白名单 | 交付物产出 (Artifacts) | 强制退出条件 |
| :--- | :--- | :--- | :--- | :--- |
| **`scout`** | 主工作区 (只读模式) | `read_file`, `list_dir`, `grep_search`, `screenshot`, `ui_tree`（**严禁所有 write/patch/shell 写命令**） | `scout_brief.json` + `aligned_anchor_node_id` | 输出调研报告与受影响文件清单，完成 1024 对齐后即刻退出 |
| **`builder`** | `.agent/worktrees/<id>` 物理沙箱 | 完整工具集（受沙箱目录限制，严禁触碰 `REDLINE_FILES`） | 工作树内代码修改 + 阶段性自测日志 | 任务代码修改完毕并产生非空 Diff 后即刻退出 |
| **`verifier`** | 挂载 Builder 工作树 (审查模式) | `shell` (仅限只读运行 `unittest`/`pytest`), `read_file` | `verification_ticket.json` (含测试回执、HAZARD 标志、判定) | 完成全量针对性测试与静态门禁扫描后签章退出 |

### 2.2 验证签章规范 (`verification_ticket.json`)
由宿主系统在 Verifier 结束时确定性生成，不可伪造：
```json
{
  "subagent_id": "sub_relay_20261101_144000",
  "builder_worktree": ".agent/worktrees/sub_relay_20261101_144000",
  "scout_anchor_node": "node_7a8b9c",
  "diff_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "hazards_detected": [],
  "test_execution": {
    "command": "python -m unittest discover -s tests -t . -p test_feature.py",
    "exit_code": 0,
    "tests_run": 8,
    "passed": true
  },
  "verdict": "VERIFIED_READY_TO_MERGE",
  "verified_at": "2026-11-01T14:42:00.123456"
}
```

---

## 🔨 三、实施子任务分解 (Work Breakdown)

### 子任务 1：升级 `core/subagent_runner.py` 角色调度器
- **接口要求**:
  - `run_subagent(task_id: str, prompt: str, role_mode: str = "general", timeout: float = 300.0) -> dict`：
    - 支持 `role_mode` 参数：`general`（兼容原有单体）、`scout`、`builder`、`verifier`；
  - **角色权限沙箱强制咬合**:
    - 当 `role_mode == "scout"` 时，拦截所有写入类工具（`write_file`, `patch`, `modify_skill`, `add_skill`，以及非只读 shell 命令），越权调用直接返回 `[SECURITY_VIOLATION] Scout role is strictly read-only`；
    - 当 `role_mode == "builder"` 时，强制在 `.agent/worktrees/<id>` 沙箱中启动，工作目录与 tools.ROOT 完全锁定至沙箱内；
    - 当 `role_mode == "verifier"` 时，挂载 Builder 的 Worktree 路径作为只读目标，独立执行测试。

### 子任务 2：扩展 `core/subagent_gate.py` 签章与接力协调
- **接口要求**:
  - `generate_verification_ticket(subagent_id: str, builder_diff: str, test_output: dict, hazards: list[str]) -> dict`：
    - 判定条件：若 `hazards` 包含任何严重标志（如删测试断言、注入 skip、改动红线文件）或 `test_output["exit_code"] != 0`，`verdict` 强制为 `REJECTED_VERIFICATION_FAILED`；
    - 仅当测试 100% 绿灯且 0 HAZARD 时，签发 `VERIFIED_READY_TO_MERGE`；
  - `finalize_subagent_relay(subagent_id: str, ticket: dict) -> dict`：
    - 依据签章执行主干 fast-forward 合并，或彻底销毁沙箱并沉淀经验教训。

### 子任务 3：新建技能 `skills/subagent_relay.py`
- **接口要求**:
  - 提供给父智能体调用的统一入口：
    `subagent_relay(goal: str, target_modules: list[str], test_target: str) -> str`；
  - 内部自动按序拉起：Scout（输出调查报告与对齐锚点）$\rightarrow$ Builder（沙箱施工）$\rightarrow$ Verifier（独立测试审计并签章）$\rightarrow$ 输出结构化摘要供父智能体一键裁决。

---

## 🧪 四、专项测试套件规划 (`tests/test_subagent_relay.py`)

执行工程师必须在 `tests/test_subagent_relay.py` 中交付至少 16 项高质量单元测试：

1. `test_scout_role_strictly_bans_write_file`：验证 Scout 尝试写入文件被宿主安全拦截并记录；
2. `test_scout_role_strictly_bans_patch_skill`：验证 Scout 尝试修改技能代码被拦截；
3. `test_scout_creates_grid_aligned_context_anchor`：验证 Scout 完成调研后，末尾正确吸附至 1024 网格锚点；
4. `test_builder_inherits_scout_anchor_prefix`：验证 Builder 从该锚点分叉，复用前缀缓存；
5. `test_builder_modifications_stay_isolated_in_worktree`：验证 Builder 的代码写入对主仓库完全透明无污染；
6. `test_verifier_detects_builder_fake_green_skip`：模拟 Builder 注入 `@unittest.skip`，验证 Verifier 签发 REJECTED；
7. `test_verifier_detects_builder_removed_assertions`：模拟 Builder 删除测试函数，验证 Verifier 签发 REJECTED；
8. `test_verifier_detects_builder_redline_violation`：模拟 Builder 触碰 `requirements.txt`，验证 Verifier 拦截；
9. `test_verifier_issues_valid_ticket_on_clean_diff`：验证合格交付下正常生成 `VERIFIED_READY_TO_MERGE` 签章；
10. `test_verification_ticket_sha256_tamper_proofing`：验证篡改 ticket 文件内容会被校验机制感知；
11. `test_parent_merges_clean_relay_delivery`：验证父智能体依据合法签章成功执行 `--ff-only` 合并；
12. `test_parent_reverts_corrupt_relay_delivery`：验证父智能体依据失败签章干净销毁沙箱并沉淀 Lesson；
13. `test_subagent_relay_skill_schema_and_ast_purity`：验证新技能通过技能沙箱 AST 纯度门禁；
14. `test_relay_pipeline_end_to_end_mock_flow`：打桩模拟完整三阶接力流程的无缝串联；
15. `test_worktree_cleanup_on_relay_exception`：验证流水线中途崩溃时残余目录被安全清理；
16. `test_active_lock_guards_relay_concurrency`：验证接力流水线执行期间严格保持单飞互斥。

---

## 🛡️ 五、执行门禁与交付清单 (Checklists)

> **验收记录 (2026-11-02)**：全部勾选项已达成。全量回归 `Ran 456 tests — OK (skipped=2)`；本卡 18 项 ≥ 16 项要求；`test_repo_hygiene.py` 0 违规；`requirements.txt` 4 项依赖零改动。门禁铁证详见 `docs/notes/report.md` Phase 12 汇报 §3。

- [x] 核心交付：升级 `core/subagent_runner.py`，升级 `core/subagent_gate.py`，新建 `skills/subagent_relay.py`；
- [x] 测试交付：新建 `tests/test_subagent_relay.py`（18 项测试全部通过，≥ 16 项要求）；
- [x] 全量回归：`python -m unittest discover -s tests -t .` 达到 456 项测试（≥ 437 项要求）100% 绿灯；
- [x] 架构红线：`requirements.txt` 保持 4 项依赖不变，纯 Python 标准库实现；
- [x] 卫生门禁：`python -m unittest discover -s tests -t . -p test_repo_hygiene.py` 0 违规，无绝对路径与私网 IP 泄露。
