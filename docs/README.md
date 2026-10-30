# Qwen38-llama-cpp-harness-windows 文档中心 (Documentation Hub)

欢迎查阅 `Qwen38-llama-cpp-harness-windows` 项目的设计、架构、任务与运维文档库。本目录对项目的规格契约、技术演进 RFC、任务卡片及运维手册进行了结构化分门别类归档。

---

## 📁 目录结构与索引导航

```
docs/
├── README.md               # [当前文件] 文档中心导航索引
├── SPEC.md                 # 核心持久化规格说明（系统的权威工程契约）
│
├── rfc/                    # 架构演进方案与领域专家咨询备忘录 (RFCs)
│   ├── ARCHITECTURE_RFC_LLAMA_CPP_AGENT.md                  # Phase 1~6: 初始长程架构、状态机与沙箱规划
│   ├── ARCHITECTURE_RFC_PHASE7_HARDENING_AUTONOMY.md        # Phase 7: 生产硬化、红线保真与三道打断门禁
│   ├── ARCHITECTURE_RFC_PHASE8_PHASE9_CONSULTATION.md       # Phase 8/9: 技能预审沙箱、DPI V2/UIA与因果回溯咨询
│   ├── ARCHITECTURE_RFC_PHASE10_WEB_GROUNDING_CONSULTATION.md # Phase 10: 双模自动对准、窗口管理与Web透视画布咨询 (RFC)
│   ├── Phase 10 架构专家方案研究.md                           # 🌟 Phase 10 专家深度评审与工程落地方案（完整源码与数学论证）
│   ├── ARCHITECTURE_RFC_PHASE10_EXPERT_REVIEW_REQUEST.md     # 📋 Phase 10 领域专家协查与全量实现复验请求书
│   ├── Phase 10 验收与 Phase 11 架构前瞻.md                  # 🏆 Phase 10 全量工程复验裁决书与 Phase 11 认知自治架构蓝图
│   └── ARCHITECTURE_RFC_PHASE11_VISION_CONSULTATION.md       # 🚀 Phase 11 前瞻展望：上下文GC、DeltaNet网格对齐与宏结晶
│
├── tasks/                  # 历史演进与生产实施任务卡片 (Checklists & Receipts)
│   ├── TASK_DELEGATION_HYGIENE.md                           # 仓库隐私基线与敏感信息治理任务
│   ├── TASK_DEV_BACKEND.md                                  # 后端单例 HTTP REST / SSE 网关落地卡片
│   ├── TASK_DEV_FRONTEND.md                                 # 零构建原生 ES Modules Web 双控台任务卡片
│   ├── TASK_DEV_AGENT_STATE_MEMORY.md                       # Git 式上下文 DAG 存储与状态一致性模型
│   ├── TASK_DEV_REAL_INFERENCE.md                           # 本地/远程模型推理端点对接与流式解码验证
│   ├── TASK_DEV_SYS_SANDBOX.md                              # Win32 Job Object 内核沙箱与双端流控环形缓冲
│   ├── TASK_DEV_PHASE7_SYSTEM_HARDENING.md                  # Phase 7 生产硬化长程自愈验收卡片（已闭环签署）
│   ├── TASK_DEV_PHASE8_PHASE9A_AUTONOMY.md                  # Phase 8 & 9A 技能治理与因果回溯验收卡片（已闭环签署）
│   ├── TASK_DEV_PHASE10A_WIN32_SKILLS.md                    # 🌟 Phase 10A Win32 窗口编排与原子剪贴板实施任务卡片
│   ├── TASK_DEV_PHASE10B_DUAL_GROUNDING.md                  # 🌟 Phase 10B 视觉-语义双模自适应对准与高清切片实施任务卡片
│   └── TASK_DEV_PHASE10C_WEB_CONSOLE.md                     # 🌟 Phase 10C 零构建 Web 控制台协同与透视画布实施任务卡片
│
├── ops/                    # 运维操作流程与宿主配置手册 (Operations)
│   └── OPS_LLAMA_SERVER_UPGRADE.md                          # 远程模型宿主 (Tesla M40) 服务升级与运维 SOP
│
└── notes/                  # 研发笔记、阶段沟通汇报与协同草稿 (Notes & Drafts)
    ├── report.md                                            # 阶段交付工程师自测与沟通汇报单
    ├── TODO修改shell套件.md                                 # 工作目录 (cwd) 参数化与上下文暂态报错清理优化笔记
    ├── 智能体项目Web栈迁移方案.gdoc                         # Web 控制台技术栈演进分析草稿
    └── Qwen3.8 Agent 架构深度分析.gdoc                      # Qwen3.8 混合注意力机制 (DeltaNet) 架构深度分析草稿
```

---

## 📌 核心文档速查索引

### 1. 系统契约与设计规范
- **[SPEC.md](SPEC.md)**：定义系统架构底座、万物皆文件持久化契约、Git 式 DAG 分支模型、ReAct 消息格式与工具分发协议，是整个仓库最权威的规格书。

### 2. 架构演进与专家咨询 (RFC)
- **[Phase 10 验收裁决书与 Phase 11 架构前瞻](<rfc/Phase 10 验收与 Phase 11 架构前瞻.md>)**：🏆 领域专家终审复验裁决书与 Phase 11 认知自治蓝图，正式签批 Phase 10 成果并推导上下文尾端 GC、DeltaNet 1024-Token 网格对齐、Worktree 门禁与宏结晶路线图。
- **[Phase 10 专家协查与全量复验请求](rfc/ARCHITECTURE_RFC_PHASE10_EXPERT_REVIEW_REQUEST.md)**：📋 向微软 MVP / 桌面虚拟化与智能体专家呈递的 Phase 10 全量代码穿透与实测数据对照核验请求。
- **[Phase 11 架构前瞻展望 RFC](rfc/ARCHITECTURE_RFC_PHASE11_VISION_CONSULTATION.md)**：🚀 Phase 11 战略前瞻备忘录，聚焦上下文暂态报错剪枝（GC）、DeltaNet 1024-Token 网格对齐、分层子智能体产物审查门禁与自动化桌面宏结晶。
- **[Phase 10 架构专家方案研究](<rfc/Phase 10 架构专家方案研究.md>)**：🌟 领域专家针对 Phase 10 与零构建 Web 演进出具的权威评审报告，提供纯 `ctypes` 窗口编排、原子剪贴板、Chromium 动态唤醒与 Web Canvas 透视层完整实现源码与数学证明。
- **[Phase 10 & Web 控制台 RFC](rfc/ARCHITECTURE_RFC_PHASE10_WEB_GROUNDING_CONSULTATION.md)**：Phase 10 架构征求意见稿，聚焦视觉-语义双模对准器（`grounding.py`）、Win32 窗口置顶编排（`window_manager.py`）、原生剪贴板通道（`clipboard.py`）以及纯原生零构建 Canvas 透视叠加层。
- **[Phase 8 & 9 专家咨询备忘录](rfc/ARCHITECTURE_RFC_PHASE8_PHASE9_CONSULTATION.md)**：涵盖 AST 顶层零 Call 纯度定理、纯标准库 OpenAI JSON Schema 校验、Per-Monitor V2 物理坐标以及自主因果回溯四维抗振荡机制。

### 3. 运维与部署 (Ops)
- **[OPS_LLAMA_SERVER_UPGRADE.md](ops/OPS_LLAMA_SERVER_UPGRADE.md)**：记录在 Tesla M40 宿主机上部署 Qwen3.8-27B 时的关键参数设定（`--spec-draft-n-max 4` 锁定、`--cache-reuse 0` 锁定、16GB RAM 检查点池开启），待 Slot 1 任务释放后执行。
