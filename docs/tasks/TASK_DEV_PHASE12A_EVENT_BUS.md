# Phase 12A 工程任务书：事件驱动文件总线与单飞常驻守护进程 (Event Bus & Single-Flight Daemon)

- **任务编号**: `TASK-DEV-PHASE12A-EVENT-BUS`
- **指派对象**: 执行工程师 A (Execution Engineer A)
- **所属阶段**: Phase 12 (7×24h 具身常驻中枢与事件驱动自主演进)
- **基线版本**: Phase 11 Milestone (`81412dc`)
- **前置依赖**: Phase 11A~11D 全套工程交付、`tests/` 395 项全绿基线
- **文档路径规范**: 本任务书全篇采用**项目根目录相对路径**，严禁引入本机绝对路径。

---

## 🎯 一、任务背景与工程定位

当前系统的核心运行时（`core/agent.py`）采用单次任务驱动模式：通过 CLI 参数 `--goal` 或 HTTP API `POST /tasks` 提交后，执行一次性 ReAct 循环，并在返回 `TASK_DONE` 或 `BLOCKED` 时退出循环。

在进入 Phase 12 具身常驻中枢演进后，系统必须支持 **7×24h 无人值守长程在线运行**，能够自主响应现实世界的异步事件（如 Win32 窗口焦点变动、定时 Cron 触发器、文件目录变更 Watchdog、Web 紧急纠偏指令）。

本任务的核心工程定位是为常驻中枢建立**高可靠、反黑盒的事件脉搏系统**：
1. **坚决捍卫单飞（Single-Flight）执行铁律**：严禁在 Python 进程内建立任何无序多线程执行器或并发请求池，避免多线程并发请求交叉冲刷破坏 `llama-server` 的 Slot 0 前缀缓存；
2. **UNIX-like 万物皆文件事件总线**：所有外部现实事件以轻量 JSON 信封原子写入 `.agent/inbox/` 目录，肉眼透明可查，支持断电断线恢复；
3. **优先级调度与事件防抖**：建立清晰的事件优先级全序关系，并对高频事件（如连续窗口微抖动、高频文件保存）提供防抖（Debounce）合并；
4. **内存水位治理（有界 LRU 缓存）**：将 `core/token_grid.py` 中的无界字典 `_TOKEN_CACHE` 重构为基于标准库 `collections.OrderedDict` 的有界 LRU 缓存（默认容量 4096），根除 7×24h 常驻内存泄漏隐患。

---

## 📐 二、技术契约与架构规约

### 2.1 事件信封规范 (Event Envelope Schema)
所有外部事件源（Win32 钩子、Watchdog、Cron）均将事件持久化至磁盘：
- **存储路径**: `.agent/inbox/<timestamp>_<priority>_<event_id>.json`
- **命名规范**:
  - `timestamp`: 14 位微秒时间戳，如 `20261101_143000_123456`
  - `priority`: 单字符数字 `0`~`9`（`0` 为最高紧急优先级，`9` 为最低后台优先级）
  - `event_id`: 8 位十六进制唯一哈希
- **数据结构 (JSON)**:
```json
{
  "event_id": "e4f8a912",
  "priority": 0,
  "event_type": "OPERATOR_INTERRUPT",
  "source": "web_console",
  "created_at": "2026-11-01T14:30:00.123456",
  "debounce_key": "operator_pause",
  "payload": {
    "directive": "暂停当前工作，优先检查导出目录",
    "abort_current": true
  }
}
```

### 2.2 事件优先级全序关系 (Priority Hierarchy)
```
Level 0: 紧急操作员指令 / Web 紧急打断 (OPERATOR_INTERRUPT / ABORT_STEP)
Level 1: 交互式用户指令 (USER_INBOX_PROMPT)
Level 2: 关键系统异常 / 探活自愈事件 (HEALTH_ALERT / RECOVERY_TRIGGER)
Level 3: 定时计划任务 (SCHEDULED_CRON)
Level 4: 文件系统变更感知 (FS_WATCHDOG_CHANGE)
Level 5: 桌面窗口焦点变动 (WIN32_WINDOW_FOCUS)
Level 9: 周期性状态快照与闲时整理 (IDLE_MAINTENANCE)
```

### 2.3 单飞仲裁与状态机切片
常驻守护进程 `daemon_loop(poll_interval=0.5)` 的调度状态机：
```
           ┌───────────────────────────────┐
           │        IDLE_WAITING           │◄─────────────────────┐
           └──────────────┬────────────────┘                      │
                          │ 检测到 .agent/inbox/ 事件             │
                          ▼                                       │
           ┌───────────────────────────────┐                      │
           │       PRIORITY_DEQUEUE        │                      │
           │   (原子读取并移除最高优事件)  │                      │
           └──────────────┬────────────────┘                      │
                          │                                       │
                          ▼                                       │
           ┌───────────────────────────────┐                      │
           │        TASK_DISPATCH          │                      │
           │  (启动/恢复单飞 Agent 任务链) │                      │
           └──────────────┬────────────────┘                      │
                          │                                       │
                          ▼                                       │
           ┌───────────────────────────────┐                      │
           │      STEP_LOOP_EXECUTION      │                      │
           │    (执行单步 ReAct / 工具调用)│                      │
           └──────┬────────────────┬───────┘                      │
                  │                │                              │
(Level 0 紧急打断)│                │ (任务收敛 / TASK_DONE / 等待)│
                  ▼                ▼                              │
     ┌──────────────────┐   ┌──────────────────────────────┐      │
     │ EMERGENCY_ABORT  │   │     POST_TASK_CHECKPOINT     │──────┘
     │ (就地保存检查点) │   │   (对齐网格 / 释放单飞锁)    │
     └────────┬─────────┘   └──────────────────────────────┘
              │
              └─────────► 回退至 IDLE_WAITING
```

---

## 🔨 三、实施子任务分解 (Work Breakdown)

### 子任务 1：新建 `core/event_bus.py` 纯标准库事件总线
- **接口要求**:
  - `emit_event(event_type: str, payload: dict, priority: int = 3, debounce_key: str | None = None, debounce_window_seconds: float = 1.0) -> str`：
    - 原子写入 `.agent/inbox/`；若 `debounce_key` 命中且在窗口时间内，合并更新而非产生新事件；
  - `peek_highest_priority_event() -> dict | None`：
    - 快速扫描 `.agent/inbox/`，按 `priority` 升序与 `timestamp` 升序返回首个最高优事件元数据，不移出队列；
  - `pop_highest_priority_event() -> dict | None`：
    - 原子拉取并移出最高优事件（文件移至 `.agent/inbox/processed/` 归档，保留审计轨迹）；
  - `purge_stale_events(max_age_seconds: float = 86400) -> int`：
    - 清理归档目录超期事件。

### 子任务 2：重构 `core/token_grid.py` 内存有界 LRU 缓存
- **接口要求**:
  - 将无界全局变量 `_TOKEN_CACHE: dict[str, int] = {}` 重构为 `_TOKEN_CACHE: OrderedDict[str, int] = OrderedDict()`；
  - 设定上限常量 `MAX_TOKEN_CACHE_SIZE = 4096`；
  - 在 `exact_or_estimated_tokens` 中：
    - 命中期将其提升至末尾 `_TOKEN_CACHE.move_to_end(key)`；
    - 插入新键时，若长度超过上限，触发 `_TOKEN_CACHE.popitem(last=False)`（FIFO 弹出最旧项）；
  - 保持现有函数签名与 300ms 超时优雅降级完全兼容。

### 子任务 3：在 `core/agent.py` 中挂载常驻守护循环
- **接口要求**:
  - 新增 `run_daemon(poll_interval: float = 0.5, max_idle_seconds: float | None = None) -> None`；
  - 在每个 `step_loop` 迭代边界，调用 `event_bus.peek_highest_priority_event()` 检查是否存在 Level 0（`priority == 0`）紧急打断信封；
  - 若检测到 `abort_current=True`，通过现有的 `abort-gate` 机制在当前回合安全落盘断点后退出当前任务，并立即消费紧急事件。

---

## 🧪 四、专项测试套件规划 (`tests/test_event_bus.py`)

执行工程师必须在 `tests/test_event_bus.py` 中交付至少 12 项高质量单元测试：

1. `test_event_envelope_serialization_and_disk_structure`：验证事件信封的 JSON 结构与文件名规范；
2. `test_priority_ordering_strict_dequeue`：验证多优先级并发写入时，严格按优先级数值升序出队；
3. `test_fifo_order_for_identical_priority`：验证同优先级事件严格按微秒时间戳先进先出；
4. `test_debounce_deduplication_window`：验证同一 `debounce_key` 在窗口期内聚合更新，不产生重复事件；
5. `test_debounce_expired_produces_new_event`：验证超出窗口期后正常产生新事件；
6. `test_pop_moves_to_processed_archive`：验证事件消费后原子归档至 `processed/` 目录；
7. `test_purge_stale_archived_events`：验证超期审计事件的自动回收；
8. `test_token_cache_lru_eviction_at_capacity`：验证 `_TOKEN_CACHE` 达到 4096 条目时的精确淘汰与内存常驻稳定性；
9. `test_token_cache_move_to_end_on_hit`：验证缓存命中时的访问热度刷新；
10. `test_daemon_idle_poll_and_event_ingestion`：验证守护进程在空闲状态下平稳轮询并快速消费事件；
11. `test_level0_emergency_abort_injection`：验证紧急打断信封在长程 ReAct 回合间隙被毫秒级识别；
12. `test_single_flight_mutual_exclusion`：验证守护进程在活跃任务未闭环时，绝不并发启动第二个任务。

---

## 🛡️ 五、执行门禁与交付清单 (Checklists)

> **验收记录 (2026-11-02)**：全部勾选项已达成。全量回归 `Ran 456 tests — OK (skipped=2)`（395 基线零回归 + 本卡 14 项 ≥ 12 项要求）；`test_repo_hygiene.py` 0 违规；`requirements.txt` 4 项依赖零改动。门禁铁证详见 `docs/notes/report.md` Phase 12 汇报 §1。

- [x] 核心交付：新建 `core/event_bus.py`，更新 `core/token_grid.py`，更新 `core/agent.py`；
- [x] 测试交付：新建 `tests/test_event_bus.py`（14 项测试全部通过，≥ 12 项要求）；
- [x] 全量回归：`python -m unittest discover -s tests -t .` 达到 456 项测试（≥ 407 项要求）100% 绿灯；
- [x] 架构红线：`requirements.txt` 保持 4 项依赖不变，纯 Python 标准库实现；
- [x] 卫生门禁：`python -m unittest discover -s tests -t . -p test_repo_hygiene.py` 0 违规，无绝对路径与私网 IP 泄露。
