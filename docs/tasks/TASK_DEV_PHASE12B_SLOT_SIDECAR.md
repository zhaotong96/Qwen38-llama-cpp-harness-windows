# Phase 12B 工程任务书：推理端点状态 Sidecar 与四级 Saga 热恢复 (Inference Slot Sidecar & Hot Recovery)

- **任务编号**: `TASK-DEV-PHASE12B-SLOT-SIDECAR`
- **指派对象**: 执行工程师 B (Execution Engineer B)
- **所属阶段**: Phase 12 (7×24h 具身常驻中枢与事件驱动自主演进)
- **基线版本**: Phase 11 Milestone (`81412dc`)
- **前置依赖**: Phase 11B DeltaNet 1024-Token 网格对齐、Phase 12A 事件驱动总线
- **文档路径规范**: 本任务书全篇采用**项目根目录相对路径**，严禁引入本机绝对路径。

---

## 🎯 一、任务背景与工程定位

在基于混合循环神经网络（DeltaNet 循环层 + 全注意力层混合架构）的推理宿主中，`llama-server` 通过 `--ctx-checkpoints 64` 和 `--checkpoint-min-step 1024` 将循环层的状态张量快照暂存在主机 RAM 池中。

然而，在 **7×24h 常驻运行** 环境下，系统面临三大物理脆弱性：
1. **端点重启丢失 RAM 快照**：当推理服务器（Tesla M40 宿主）因系统维护、驱动微抖动或内存超限触发重启后，所有 RAM 检查点池瞬间清空；随后发起的长程任务将被迫退化为 `forcing full prompt re-processing`，重算一个数万 Token 的会话需要耗时数分钟；
2. **瞬态网络假死与连接拒绝**：如测试实测所示，宿主偶发 `[WinError 10061] No connection could be made because the target machine actively refused it`，传统客户端会直接抛出未捕获异常导致长程会话中断；
3. **不可逆物理动作与状态回滚冲突**：当任务因推理中断需要回滚重试时，某些外部动作（如已执行的 `write_file`、移动文件或桌面点击）具有物理副作用，若不加区别地执行逻辑回滚，会导致任务状态与现实世界脱节。

本任务的核心工程定位是**为长程具身常驻中枢装配“防灾黑匣子与自愈免疫系统”**：
1. **Slot 0 隐状态物理 Sidecar 持久化**：在纪元压缩与跨越 1024 网格线瞬间，调用 `POST /slots/0?action=save` 将 Slot 0 循环状态持久化至磁盘 `.agent/slots/slot0_<hash>.ckpt`，跨重启实现秒级 `restore`；
2. **端点心跳探活与指数退避自愈状态机**：封装非阻塞的心跳探测，遭遇网络闪断或重启时自动挂起当前 step 并执行热恢复重连；
3. **四级可逆性 Saga 代数（Reversibility Algebra）**：对每一个工具动作赋予形式化可逆性评级，支持无损自动补偿与安全回退。

---

## 📐 二、技术契约与架构规约

### 2.1 推理端点 Slot Sidecar 契约
- **服务端接口**:
  - `POST /slots/0?action=save&filepath=.agent/slots/slot0_<token_hash>.ckpt`：将当前 Slot 0 循环状态与注意力快照冻结落盘；
  - `POST /slots/0?action=restore&filepath=.agent/slots/slot0_<token_hash>.ckpt`：秒级恢复该快照，无需任何冷预填充；
  - `POST /slots/0?action=erase`：释放该 Slot。
- **快照元数据持久化契约**:
  - 存储位置：`.agent/slots/manifest.json`
  - 结构规范：
```json
{
  "slot_id": 0,
  "last_saved_at": "2026-11-01T14:35:00.123456",
  "aligned_token_count": 2048,
  "prefix_sha1": "7a8b9c0d1e2f3a4b...",
  "ckpt_filename": "slot0_2048_7a8b9c.ckpt",
  "ckpt_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "status": "VALID"
}
```

### 2.2 四级可逆性 Saga 代数 (Reversibility Algebra)
在执行动作链或发生断点回溯时，系统依据动作可逆性模型实施状态补偿：

| 等级标识 | 定性描述 | 代表工具动作 | 回退与补偿策略（Saga Strategy） |
| :--- | :--- | :--- | :--- |
| **Level 0** | **Pure Read (纯只读探针)** | `read_file`, `list_dir`, `screenshot`, `pic_zoom`, `ui_tree` | **零操作补偿**：逆动作恒等于空集 $A^{-1} = \varnothing$，直接回退 DAG 逻辑指针。 |
| **Level 1** | **Logical State (可瞬态重置)** | 内存变量、草稿状态、未合并的 Git 分支指针 | **逻辑回退**：重置内存上下文与分支引用。 |
| **Level 2** | **Compensable Write (可机械补偿)** | `write_file` (新建), `patch` (有备份), `worktree` 增删 | **确定性逆向补偿**：自动调用记录的逆动作 $A^{-1}$（如删除新文件、恢复原始文本备份、清理 worktree 物理目录）。 |
| **Level 3** | **Irreversible External (物理不可逆)** | 真实邮件发送、外部网络转账、覆盖无备份的关键文件 | **操作员前置卡点**：禁止静默回滚；在执行前必须申请操作员确认，回溯时生成需要人工介入的断点报告。 |

---

## 🔨 三、实施子任务分解 (Work Breakdown)

### 子任务 1：新建 `core/slot_sidecar.py`
- **接口要求**:
  - `save_slot_checkpoint(base_url: str, slot_id: int, aligned_tokens: int, prefix_text: str, timeout: float = 3.0) -> dict`：
    - 调用 `POST /slots/<id>?action=save`，计算并校验落盘快照的 SHA-256，更新 `manifest.json`；
    - 当服务端不支持此接口或超时时，静默记录日志并返回 `{"ok": False, "reason": "unsupported"}`（绝不抛出阻断异常）；
  - `restore_slot_checkpoint(base_url: str, slot_id: int, manifest: dict | None = None, timeout: float = 5.0) -> bool`：
    - 校验快照物理文件完整性，调用 `POST /slots/<id>?action=restore`，恢复 Slot 0 缓存；
  - `probe_endpoint_health(base_url: str, timeout: float = 1.0) -> dict`：
    - 返回端点连通性状态（`HEALTHY`, `REFUSED`, `OOM_RESTARTING`, `UNKNOWN`）。

### 子任务 2：实现四级 Saga 补偿执行器 (`core/saga_compensator.py`)
- **接口要求**:
  - `classify_action_reversibility(tool_name: str, args: dict) -> int`：
    - 返回 `0`、`1`、`2` 或 `3`；
  - `record_action_journal(task_id: str, step_index: int, tool_name: str, args: dict, before_state: dict | None = None) -> None`：
    - 在 `.agent/tasks/<id>/action_journal.jsonl` 原子追加可逆性日志与前置快照（如文件原内容哈希）；
  - `execute_saga_rollback(task_id: str, target_step_index: int) -> dict`：
    - 逆序遍历日志并逐步执行 Level 2 机械补偿动作；若遭遇 Level 3 动作则安全中断并生成交互确认工单。

### 子任务 3：与 `core/agent.py` 及 `core/token_grid.py` 咬合
- **接口要求**:
  - 在 `step_loop` 中，当 `align_skeleton_block_to_grid` 返回 `aligned=True` 且遥测判定为 `RESONANT` 时，触发一次轻量 Slot 快照持久化；
  - 在 LLM 请求捕获到 `URLError` / `10061` 拒连时，激活端点自愈状态机：执行 3 次指数退避（1s, 2s, 4s），并在重连成功后自动执行 `restore_slot_checkpoint` 恢复对齐缓存。

---

## 🧪 四、专项测试套件规划 (`tests/test_slot_sidecar.py`)

执行工程师必须在 `tests/test_slot_sidecar.py` 中交付至少 14 项高质量单元测试：

1. `test_sidecar_save_slot_request_contract`：验证 `save_slot_checkpoint` 发起的 HTTP POST 请求路径与参数；
2. `test_sidecar_save_manifest_metadata_integrity`：验证 `manifest.json` 的字段完整性与 SHA-256 计算；
3. `test_sidecar_save_graceful_degradation_on_http_error`：验证端点返回 400/500 时平滑降级为 `{"ok": False}`；
4. `test_sidecar_restore_slot_request_contract`：验证 `restore_slot_checkpoint` 的恢复请求与返回值校验；
5. `test_sidecar_restore_fails_fast_on_corrupt_file`：验证快照文件被意外截断或哈希不匹配时拒绝恢复；
6. `test_endpoint_health_probe_states`：验证探针在正常、超时与 10061 拒连下的四态映射；
7. `test_classify_reversibility_level0_probes`：验证所有观察类探针均归入 Level 0；
8. `test_classify_reversibility_level2_file_writes`：验证 `write_file` / `patch` 正确分类为 Level 2；
9. `test_classify_reversibility_level3_critical_actions`：验证高风险外部操作归入 Level 3；
10. `test_saga_journal_append_and_serialization`：验证动作日志的只追加不可变持久化；
11. `test_saga_rollback_compensates_level2_file_creation`：验证回滚时自动删除新建的文件并恢复环境；
12. `test_saga_rollback_halts_safely_at_level3_boundary`：验证遭遇不可逆操作时安全挂起并输出确认工单；
13. `test_agent_step_loop_auto_heals_from_endpoint_refusal`：模拟服务端闪断，验证主循环自动重试并恢复；
14. `test_slot_checkpoint_skips_when_not_grid_aligned`：验证非 1024 整数倍网格点不触发无谓的磁盘快照。

---

## 🛡️ 五、执行门禁与交付清单 (Checklists)

> **验收记录 (2026-11-02)**：全部勾选项已达成。全量回归 `Ran 456 tests — OK (skipped=2)`；本卡 15 项 ≥ 14 项要求；`test_repo_hygiene.py` 0 违规；`requirements.txt` 4 项依赖零改动。门禁铁证详见 `docs/notes/report.md` Phase 12 汇报 §2。

- [x] 核心交付：新建 `core/slot_sidecar.py`，新建 `core/saga_compensator.py`，更新 `core/agent.py`；
- [x] 测试交付：新建 `tests/test_slot_sidecar.py`（15 项测试全部通过，≥ 14 项要求）；
- [x] 全量回归：`python -m unittest discover -s tests -t .` 达到 456 项测试（≥ 421 项要求）100% 绿灯；
- [x] 架构红线：`requirements.txt` 保持 4 项依赖不变，纯 Python 标准库实现；
- [x] 卫生门禁：`python -m unittest discover -s tests -t . -p test_repo_hygiene.py` 0 违规，无绝对路径与私网 IP 泄露。
