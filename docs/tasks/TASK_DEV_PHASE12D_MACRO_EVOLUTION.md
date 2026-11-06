# Phase 12D 工程任务书：宏技能健康度账本与自主热重结晶自愈 (Macro Healing & Auto-Crystallization)

- **任务编号**: `TASK-DEV-PHASE12D-MACRO-EVOLUTION`
- **指派对象**: 执行工程师 D (Execution Engineer D)
- **所属阶段**: Phase 12 (7×24h 具身常驻中枢与事件驱动自主演进)
- **基线版本**: Phase 11 Milestone (`81412dc`)
- **前置依赖**: Phase 11D 工作流宏结晶编译器、Phase 8B 技能健康度账本契约
- **文档路径规范**: 本任务书全篇采用**项目根目录相对路径**，严禁引入本机绝对路径。

---

## 🎯 一、任务背景与工程定位

在 Phase 11D 中，我们构建了 0-Token 确定性工作流宏编译器（`core/macro_compiler.py`），实现了从成功探索轨迹到纯净 Python 技能的结晶，并引入了 `[MACRO_STALE_HANDOVER]` 断点热交接机制。

然而，在 **7×24h 常驻具身自动化** 场景下，桌面生态并非静态不变：
1. **Windows 软件持续更新与 UI 微变**：按钮位置微移、文案微调（如“导出报表”改为“批量导出”）、弹窗干扰等会导致既有宏脚本在第 $k$ 步匹配失败；
2. **缺乏自愈闭环导致重复降级**：传统方案在触发断点热交接并由大模型交互接管完成后，原有发生失效的宏脚本依然保留旧的选择器，下一次调用依然会失败，导致长程常驻退化为持续的高 Token 交互模式；
3. **缺少健康度量化与熔断**：缺乏对宏脚本连续失败次数的追踪，频繁在同一个陈旧宏上重试会导致无谓的延迟累积。

本任务的核心工程定位是**构建具身智能体的“工作流自我免疫与动态演进闭环”**：
1. **截尾宏块特征增强**：在宏步骤失败时，不仅返回步骤索引，更自动捕获失效上下文的局部 UI 树切片与截尾特征；
2. **宏健康度三振账本联动**：将每个宏脚本的调用结果实时记入 `.agent/skill_health.json`，连续失败 3 次自动标记为 `DEGRADED_NEEDS_HEALING`；
3. **无人值守热重结晶自愈（Autonomous Auto-Re-Crystallization）**：当大模型通过视觉-语义双模定位（`grounding`）成功走完剩余步骤并达成 `TASK_DONE` 后，收尾钩子自动将本次自愈后的新轨迹进行提纯与重编译（`overwrite=True`），自动替换旧宏并清零健康度账本，真正实现**“一次受阻、当场自愈、永久免疫”**。

---

## 📐 二、技术契约与架构规约

### 2.1 自愈闭环状态机 (Self-Healing State Machine)

```
       ┌────────────────────────┐
       │   调用 macro_<name>    │
       └───────────┬────────────┘
                   │
                   ▼
       ┌────────────────────────┐
       │  0-Token 确定性重放    │
       └───────────┬────────────┘
                   │
           ┌───────┴───────┐
      命中成功             命中失败 (步骤 k)
           │               │
           ▼               ▼
 ┌───────────────────┐ ┌──────────────────────────────────────────────┐
 │ .agent/skill_health│ │ 1. 健康度账本扣分 (fail_count += 1)          │
 │ 记录成功，退出    │ │ 2. 返回结构化 [MACRO_STALE_HANDOVER]         │
 └───────────────────┘ └──────────────────────┬───────────────────────┘
                                              │
                                              ▼
                       ┌──────────────────────────────────────────────┐
                       │ 大模型仅需对步骤 k..N 进行视觉-语义双模接管  │
                       └──────────────────────┬───────────────────────┘
                                              │
                                              ▼
                       ┌──────────────────────────────────────────────┐
                       │   主模型顺利达成 TASK_DONE 终结状态          │
                       └──────────────────────┬───────────────────────┘
                                              │
                                              ▼
                       ┌──────────────────────────────────────────────┐
                       │ 收尾钩子触发 Auto-Re-Crystallization:        │
                       │ 1. 提取自愈后的新 Winning Spine              │
                       │ 2. compile_and_deploy_macro(..., overwrite)  │
                       │ 3. 部署新代码并通过 AST 沙箱                 │
                       │ 4. .agent/skill_health 账本复位清零          │
                       └──────────────────────────────────────────────┘
```

### 2.2 宏健康度数据结构 (`.agent/skill_health.json`)
```json
{
  "skills": {
    "macro_export_financial_report": {
      "status": "HEALTHY",
      "consecutive_failures": 0,
      "total_executions": 42,
      "last_executed_at": "2026-11-01T14:45:00.123456",
      "last_healed_at": "2026-11-01T12:00:00.000000",
      "failure_history": []
    }
  }
}
```

---

## 🔨 三、实施子任务分解 (Work Breakdown)

### 子任务 1：增强 `core/macro_compiler.py` 的截尾上下文捕获
- **接口要求**:
  - 在生成的宏模板 `_MACRO_TEMPLATE` 中，当步骤 `k` 遭遇重试耗尽时，捕获当前活跃窗口标题、控件类名与错误快照；
  - 构造包含丰富特征的结构化回执：
    ```json
    {
      "ok": false,
      "status": "MACRO_STALE_AT_STEP",
      "completed_steps_count": 2,
      "failed_step_index": 3,
      "failed_step_op": "uia_click",
      "failed_target": "导出报表",
      "window_title": "财务报表管理系统 v2.0",
      "directive": "[MACRO_STALE_HANDOVER] Steps 0..1 already executed on screen. Step 3 ('导出报表') failed due to UI change. Please take over from Step 3."
    }
    ```

### 子任务 2：扩展 `core/skill_sandbox.py` 宏健康度三振记分
- **接口要求**:
  - `record_macro_execution_result(macro_name: str, success: bool, failure_step: int | None = None) -> dict`：
    - 成功时：`consecutive_failures` 置 0，状态置 `HEALTHY`；
    - 失败时：`consecutive_failures += 1`；若达 3 次，状态标记为 `DEGRADED_NEEDS_HEALING`；
  - `reset_macro_health(macro_name: str) -> None`：
    - 当重结晶部署成功后，将失败计数与降级标记清零。

### 子任务 3：在 `core/agent.py` 收尾钩子挂载自主重结晶自愈器
- **接口要求**:
  - 在 `step_loop` 检测到 `TASK_DONE` 且前序历史中包含 `[MACRO_STALE_HANDOVER]` 回执时：
    - 自动识别出触发断点交接的宏名称（如 `macro_export_report`）；
    - 自动从当前任务的完整 Winning Spine 中提纯出修复后的新主干序列；
    - 自动调用 `compile_and_deploy_macro(task_id, macro_name, description, overwrite=True)`；
    - 调用 `reset_macro_health(macro_name)` 复位健康度账本；
    - 在最终任务总结中记录 `[AUTONOMOUS_HEALING] Macro 'macro_export_report' was successfully re-crystallized with updated UI selectors.`。

---

## 🧪 四、专项测试套件规划 (`tests/test_macro_evolution.py`)

执行工程师必须在 `tests/test_macro_evolution.py` 中交付至少 14 项高质量单元测试：

1. `test_macro_stale_handover_captures_window_and_target_context`：验证步骤失败时回执包含完整的截尾元数据；
2. `test_macro_execution_success_resets_failure_counter`：验证执行成功时健康度账本记为 0 失败；
3. `test_macro_consecutive_failures_triggers_degraded_status`：验证连续 3 次失败触发 `DEGRADED_NEEDS_HEALING`；
4. `test_agent_identifies_stale_macro_in_trajectory`：验证 Agent 能准确从历史中识别出发生交接的宏名称；
5. `test_agent_auto_recrystallizes_healed_macro_on_task_done`：模拟模型接管完成任务，验证自动触发重结晶；
6. `test_auto_recrystallize_overwrites_existing_skill_safely`：验证覆盖部署后物理文件更新且通过 AST 检查；
7. `test_auto_recrystallize_resets_health_ledger`：验证重结晶成功后健康账本自动清除降级标记；
8. `test_recrystallized_macro_replays_with_updated_selector`：验证更新后的宏在后续重放中直接使用新控件命中；
9. `test_no_recrystallize_triggered_when_macro_fully_succeeded`：验证宏原本就全绿执行时，不触发多余的重编译；
10. `test_no_recrystallize_triggered_when_task_fails_or_blocked`：验证任务中途放弃或未完成时不触发误重写；
11. `test_ast_purity_guarantee_for_evolved_macro`：验证自愈合成出的宏代码 100% 满足技能沙箱 AST 规范；
12. `test_concurrent_macro_execution_health_ledger_safety`：验证多宏执行时健康度账本的写入互斥性；
13. `test_stale_handover_retains_immutable_context_facts`：验证断点回执作为物理事实不被 Phase 11A GC 误剪；
14. `test_macro_evolution_end_to_end_mock_lifecycle`：打桩模拟“宏受阻 $\rightarrow$ 接管 $\rightarrow$ 任务收官 $\rightarrow$ 自动重结晶”完整生命周期。

---

## 🛡️ 五、执行门禁与交付清单 (Checklists)

> **验收记录 (2026-11-02)**：全部勾选项已达成。全量回归 `Ran 456 tests — OK (skipped=2)`；本卡 14 项 = 14 项要求；`test_repo_hygiene.py` 0 违规；`requirements.txt` 4 项依赖零改动。门禁铁证详见 `docs/notes/report.md` Phase 12 汇报 §4。

- [x] 核心交付：升级 `core/macro_compiler.py`，升级 `core/skill_sandbox.py`，升级 `core/agent.py`；
- [x] 测试交付：新建 `tests/test_macro_evolution.py`（14 项测试全部通过）；
- [x] 全量回归：`python -m unittest discover -s tests -t .` 达到 456 项测试（≥ 451 项要求）100% 绿灯；
- [x] 架构红线：`requirements.txt` 保持 4 项依赖不变，纯 Python 标准库实现；
- [x] 卫生门禁：`python -m unittest discover -s tests -t . -p test_repo_hygiene.py` 0 违规，无绝对路径与私网 IP 泄露。
