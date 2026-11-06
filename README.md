# Qwen38-llama-cpp-harness-windows

![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?style=flat-square&logo=windows)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python)
![Architecture](https://img.shields.io/badge/Architecture-KISS%20%7C%20Zero--Build-success?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

> 本地 Windows 计算机操作智能体（Computer Use Agent）长程运行时与 Web/API/CLI 双端协同控制网关。

---

## 🌟 项目定位与设计哲学

本项目为运行在 Windows 宿主机上的本地长程计算机操作智能体，支持连接本地推理服务（如 `llama.cpp` 运行的 Qwen 3.8 等模型）以及标准 OpenAI 兼容端点。系统提供统一单例内核，驱动原生 Web 双控控制台、CLI 交互终端与外部 AI 助手 HTTP API。

### 核心不可动摇原则

1. **单一核心，多前端**：CLI（`main.py`）、HTTP API（`core/api.py`）与原生 Web 控制台仅作为传输层薄前端，共享同一单例 Agent 内核。
2. **万物皆 Skill**：所有操作能力解耦为 `skills/*.py` 自描述脚本文件，遵循标准 OpenAI Function Calling 契约，支持热加载。
3. **万物皆文件 & 透明状态反黑盒**：拒绝黑盒内存数据库或不可见状态。任务快照（`checkpoint.jsonl`）、分支指针（`branches/`）、队列项（`.agent/queue/`）、打断标记（`abort.flag`）均为可读明文文件。
4. **单飞约束（Single-flight）与单 SubAgent 互斥**：同一时刻单任务执行（409 保护）；SubAgent 沙箱通过 `active.lock` 严格单实例互斥，剥离递归元技能，杜绝上下文污染。
5. **KISS / 奥卡姆剃刀**：零构建链（无 Node.js/npm）、标准库 HTTP/SSE 网关；运行时依赖严格精简为 4 项。

---

## 🛠️ 快速开始

### 1. 安装依赖

环境要求：Python 3.11+ (Windows)

```bash
pip install -r requirements.txt
```

*仅依赖 4 个基础库：`openai`（协议客户端）、`mss`（屏幕捕获）、`pillow`（图像处理）、`websocket-client`（Chrome CDP）。*

### 2. 配置推理端点（可选）

系统默认连接本地回环地址 `http://127.0.0.1:8080/v1`。如需自定义，可配置环境变量：

```powershell
$env:GEMMA_BASE_URL = "http://127.0.0.1:8080/v1"
$env:GEMMA_MODEL = "local-model"
```

系统亦支持在 `.agent/providers.json` 中配置 `main` / `subagent` / `compactor` 三级分角色路由。

### 3. 启动运行

#### 方式 A：启动双端协同 Web 控制台（推荐）

```bash
py -3 -m core.api
```

启动后在浏览器打开：`http://127.0.0.1:8765/web/index.html`，即可进入包含 Git 式上下文分支树、实时流式解码与文件排队面板的原生 Web 双控台。

#### 方式 B：启动 CLI 交互模式

```bash
py -3 main.py
```

*支持 `--resume` 列出与恢复历史任务，支持 `--goal` 长期目标模式。*

---

## 📁 架构全景

```
Qwen38-llama-cpp-harness-windows/
├── main.py                     # 薄前端 1：CLI 交互入口
├── core/                       # 核心业务模块（单例内核）
│   ├── api.py                  # 薄前端 2：HTTP REST & SSE 网关服务
│   ├── agent.py                # ReAct 循环引擎与技能分发
│   ├── event_bus.py            # 事件驱动文件总线与单飞守护仲裁 (.agent/inbox/)
│   ├── slot_sidecar.py         # 推理端点状态 Sidecar 与 Slot 0 检查点热恢复
│   ├── saga_compensator.py     # 四级可逆性代数与确定性逆动作补偿事务
│   ├── persistence.py          # Git 式上下文 DAG 存储与 NTFS 安全写入
│   ├── queue_manager.py        # 显式文件基双端同步队列 (.agent/queue/)
│   ├── interrupt.py            # 三道实时打断门禁 (Gate 1-3) 与崩溃自愈
│   ├── subagent_runner.py      # 单实例隔离 SubAgent 沙箱与三阶接力运行器
│   ├── subagent_gate.py        # SubAgent Git Worktree 隔离、防篡改验签门禁
│   ├── skill_sandbox.py        # 动态技能 AST 纯度沙箱、干运行准入与宏三振健康账本
│   ├── macro_compiler.py       # 成功轨迹宏结晶编译器与断点热交接 (0-Token 重放)
│   ├── token_grid.py           # DeltaNet 1024-Token 网格对齐、遥测自检与有界 LRU
│   ├── context.py              # 会话上下文生命周期、分支重组与尾端暂态 GC
│   ├── config.py               # 三级角色路由、模型窗口探测与费用审计
│   ├── tools.py                # 内置安全命令执行、文件读写与 CDP 通信
│   ├── validator.py            # 参数 Schema 校验与死循环防护
│   └── logger.py               # 控制台与文件日志输出（跨平台 UTF-8 编码安全）
├── skills/                     # 可扩展原子技能目录（万物皆 Skill）
│   ├── shell.py                # PowerShell 7 安全执行
│   ├── chrome_dom.py           # Chrome DevTools Protocol (CDP) 交互
│   ├── grounding.py            # 双模语义-视觉对准定位（UIA 穿透 + 高清切片）
│   ├── window_manager.py       # Win32 六步防死锁窗口激活状态机
│   ├── screenshot.py           # 屏幕捕获与无损降级
│   ├── subagent.py             # 派生隔离子智能体沙箱
│   ├── subagent_review.py      # 父级对立证伪审查裁决（快进合并 / 一键回滚）
│   ├── subagent_relay.py       # 三阶多角色 (Scout→Builder→Verifier) 接力流水线
│   ├── crystallize_macro.py    # 将成功工作流轨迹结晶为确定性宏技能
│   └── ...                     # read_file, write_file, mouse, keyboard 等
├── web/                        # 零构建原生 ES Modules Web 控制台
│   ├── index.html              # 控制台页面结构
│   ├── styles.css              # 原生自适应样式
│   ├── app.js                  # 核心控制器与 SSE 事件总线
│   ├── store.js                # 响应式状态容器
│   ├── tree_view.js            # DAG 分支树可视化投影
│   ├── inspector.js            # 检查器与单步断点交互
│   ├── fact_ledger.js          # 五段式事实账本实时投影
│   ├── queue_panel.js          # 队列操作面板
│   ├── canvas_overlay.js       # 双模对准 Canvas 透视叠加层
│   └── dag_timeline.js         # 因果时序回溯与重放
├── tests/                      # 32 个自动化测试套件（456 项测试）
├── .agent/                     # 运行时状态（仅 AGENTS.md 纳入版本控制）
│   └── AGENTS.md               # 核心系统提示词与行为规则
├── docs/                       # 项目文档中心（SPEC, RFC, 任务卡片, 运维手册）
│   ├── README.md               # 文档导航总览
│   ├── SPEC.md                 # 项目持久化规格说明（权威契约）
│   ├── rfc/                    # 架构 RFC 征求意见稿与专家咨询备忘录
│   ├── tasks/                  # 历史演进与生产实施任务卡片 (Checklists)
│   ├── ops/                    # 远程模型宿主升级与运维手册
│   └── notes/                  # 研发笔记与阶段沟通汇报
└── requirements.txt            # 运行时依赖清单（严格 4 项）
```

---

## 🧠 Phase 11 认知自治能力

在基础 ReAct 循环之上，运行时内建四层长程自治机制（均可通过磁盘文件审计）：

- **尾端锚定上下文 GC**（`core/context.py`）：局部试错闭环成功后，紧邻尾部的失败回合块从内存投影中折叠为一条 `[Transient Attempt Pruned: ...]` 因果徽标；磁盘 `checkpoint.jsonl` 永不篡改，OpenAI tool_calls 拓扑 1:1 保真，TDD 门禁开启期间禁剪。
- **DeltaNet 1024-Token 网格对齐**（`core/token_grid.py`）：静态骨架前缀（系统提示 + 事实账本）通过零语义 `\n---` 结构分隔线吸附至服务端检查点网格线；`/tokenize` 精确计数 + SHA-1 记忆化，离线平滑回退加权估算；`timings.cache_n` 遥测审计（RESONANT / COLD_RESET_DETECTED / CHECKPOINT_GAP_DRIFT）与确定性重播种熔断。
- **SubAgent Git Worktree 沙箱门禁**（`core/subagent_gate.py` + `skills/subagent_review.py`）：子智能体在 `.agent/worktrees/<id>` 物理隔离工作树中执行；宿主（非 LLM）确定性生成防篡改产物卡片（文件清单 + SHA-256 + 统一 Diff + 测试回执 + 五类静态风险标记）；父智能体对立证伪审查后 `--ff-only` 快进合并或一键回滚并沉淀 `[Branch Rewind Lesson]`。
- **工作流宏结晶**（`core/macro_compiler.py` + `skills/crystallize_macro.py`）：将成功 DAG 轨迹提纯（剔除失败重试与只读探针、合并 grounding+mouse 为语义 `uia_click`）、按目标污点抽取 `$slot` 参数、发射 AST 顶层零 Call 纯度的宏技能；界面改版时返回 `[MACRO_STALE_HANDOVER]` 断点热交接指令，0 Token 毫秒级确定性重放。

---

## 🏛️ Phase 12 具身常驻中枢与自主演进

系统演进为具备 7×24h 长期无人值守运行能力的具身常驻内核：

- **事件驱动文件总线与单飞守护**（`core/event_bus.py`）：以 `.agent/inbox/` 文件系统为总线基座，0-9 优先级与微秒全序队列；同键高频事件原地合并防抖；Level-0 紧急打断（`priority=0` + `abort_current=true`）在回合边界毫秒级熔断当前任务；`daemon_flight.lock` 物理排他互斥保证全系统单飞（Single-Flight）执行红线。
- **推理端点状态 Sidecar 与四级 Saga 热恢复**（`core/slot_sidecar.py` + `core/saga_compensator.py`）：Slot 0 隐藏状态持久化秒级热恢复，规避服务重启导致的万 Token 耗时冷预填充；10061 断线指数退避自愈探活；四级可逆性代数（Level 0 只读、Level 1 瞬态、Level 2 机械回退、Level 3 物理不可逆确认工单），逆序遍历实现确定性事务回滚。
- **三阶多角色子智能体接力流水线**（`core/subagent_runner.py` + `skills/subagent_relay.py` + `core/subagent_gate.py`）：Scout（只读代码拓扑调研与 1024 网格对齐锚点吸附）→ Builder（独立 Git Worktree 沙箱施工，主工作区 0 污染）→ Verifier（独立执行测试套件与静态风险扫描，由宿主签发含 SHA-256 防伪验签的 `verification_ticket.json`），父级一键 `--ff-only` 合并或回滚。
- **宏健康度账本与自主热重结晶自愈**（`core/macro_compiler.py` + `core/skill_sandbox.py`）：`.agent/skill_health.json` 记录宏回放状态；三次连续失败触发 `DEGRADED_NEEDS_HEALING` 降级预警；界面改版发生时，Agent 自动从截尾失败步无缝接管，并在 `TASK_DONE` 自动以最新获胜主干执行零人工介入覆盖编译与热部署（`overwrite=True`）。

---

## 🧪 测试与质量门禁

运行全量测试套件（零外部服务依赖，测试全套件约 2.5 分钟完成）：

```bash
py -3 -m unittest discover -s tests -t . -v
```

或使用 pytest：

```bash
pytest
```

---

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。