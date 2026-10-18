# 迁移与阶段性交付报告 (Phase 0 ~ Phase 4)

本项目已按照《智能体项目Web栈迁移方案》与 [SPEC.md](SPEC.md) 完成了 Phase 0 至 Phase 4 的全量代码实现、Web 控制台开发及 14 个自动化回归与专项测试套件验证：

* **顶层入口与核心模块（[core/](core/)）**：[main.py](main.py)、[core/models.py](core/models.py)、[core/persistence.py](core/persistence.py)、[core/context.py](core/context.py)、[core/config.py](core/config.py)、[core/queue_manager.py](core/queue_manager.py)、[core/interrupt.py](core/interrupt.py)、[core/subagent_runner.py](core/subagent_runner.py)、[core/tools.py](core/tools.py)、[core/validator.py](core/validator.py)、[core/agent.py](core/agent.py)、[core/api.py](core/api.py)
* **新增隔离技能（[skills/](skills/)）**：[skills/subagent.py](skills/subagent.py)
* **零构建原生 Web 控制台（[web/](web/)）**：[web/index.html](web/index.html)、[web/styles.css](web/styles.css)、[web/app.js](web/app.js)、[web/tree_view.js](web/tree_view.js)、[web/queue_panel.js](web/queue_panel.js)
* **自动化测试套件（[tests/](tests/)）**：[tests/test_branching.py](tests/test_branching.py)、[tests/test_queue.py](tests/test_queue.py)、[tests/test_interrupt.py](tests/test_interrupt.py)、[tests/test_subagent.py](tests/test_subagent.py)、[tests/test_config.py](tests/test_config.py)、[tests/test_api.py](tests/test_api.py)

### 五阶段架构演进与门禁落地明细

| 演进阶段 | 核心实现模块 | UNIX 文件标记契约 | 关键工程特性与门禁验证结果 |
| --- | --- | --- | --- |
| **Phase 0：协议筑基与单一会话收敛** | [core/persistence.py](core/persistence.py)、[core/agent.py](core/agent.py) | `.agent/current_session`、`.agent/tasks/<id>/live_stream.jsonl` | 实现带 NTFS 句柄冲突退避重试的 `atomic_write_text()` 原语，注入 `FileAndSseObserver` 解耦终端标准流并实时落盘增量事件。 |
| **Phase 1：Git 式上下文树与多 API 路由** | [core/context.py](core/context.py)、[core/config.py](core/config.py) | `checkpoint.jsonl`（含 `id`/`parent_id`）、`branches/<name>`、`current_branch`、`tags/<name>`、`.agent/providers.json`、`.agent/usage.jsonl` | 支持旧版线性快照自动升级为 `legacy_00000k` 节点、任意 Commit 分叉（内置 `tool_calls` 配对完整性校验）与非破坏性分支内压缩；支持 `main`/`subagent`/`compactor` 三级角色路由与 `GEMMA_*` 无损回退。 |
| **Phase 2：双端统一排队与三道打断门禁** | [core/queue_manager.py](core/queue_manager.py)、[core/interrupt.py](core/interrupt.py)、[core/tools.py](core/tools.py) | `.agent/queue/<prio>_<ts>_<id>.json`、`step_state`、`abort.flag`、`.agent/interrupt_processed/` | 双端同步可见、可挂起（`held` 状态严禁提前消费）、可改内容与优先级、可撤回；实现流式解码感知（Gate 1）、分发前硬拒绝（Gate 2）与子进程树强杀及输出丢弃（Gate 3）。 |
| **Phase 3：SubAgent 隔离沙箱系统** | [skills/subagent.py](skills/subagent.py)、[core/subagent_runner.py](core/subagent_runner.py) | `.agent/subagents/active.lock`、`.agent/subagents/<sub_id>/{checkpoint.jsonl,status,abort.flag}` | 严格遵循 [AGENTS.md](AGENTS.md) 单实例互斥锁（并发调用返回 `SUBAGENT_BUSY`），剥离递归元技能，支持主任务级联打断并映射 `completed`/`failed`/`cancelled` 终态。 |
| **Phase 4：统一网关与零构建 Web 控制台** | [core/api.py](core/api.py)、[web/index.html](web/index.html)、[web/app.js](web/app.js) | 零新增隐式状态文件，[requirements.txt](requirements.txt) 维持 4 项依赖零膨胀 | 网关启动自动执行断电自愈（清理孤儿 `active.lock`、重置 `processing` 排队项、续跑自动补齐 `[CRASH_RECOVERY]` 回执）；全量 14 个测试模块共 107 项测试与 [tests/test_repo_hygiene.py](tests/test_repo_hygiene.py) 隐私门禁全部通过。 |

在本地同步该目录后，运行 `py -3 -m core.api` 即可启动统一守护进程，并在浏览器打开 `http://127.0.0.1:8765/web/index.html` 进入双端协同控制台。