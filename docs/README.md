# Qwen38-llama-cpp-harness-windows 文档中心 (Documentation Hub)

欢迎查阅 Qwen38-llama-cpp-harness-windows 项目的设计、架构、任务与运维文档库。本目录对项目的规格契约、技术演进 RFC、任务卡片、交接档案及运维手册进行了结构化分门别类归档。

---

## 📁 目录结构与索引导航

`
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
│   ├── ARCHITECTURE_RFC_PHASE11_VISION_CONSULTATION.md       # 🚀 Phase 11 前瞻展望：上下文GC、DeltaNet网格对齐与宏结晶
│   ├── Phase 11 架构专家验收申请任务委托书.md                # 📋 Phase 11 全量交付：架构专家终审验收申请任务委托书
│   ├── Phase 11 架构专家验收报告.md                          # 🏆 Phase 11 领域专家终审验收裁决报告（全量 ACCEPTED）
│   ├── ARCHITECTURE_RFC_PHASE12_EXPERT_CONSULTATION_REQUEST.md # 🚀 Phase 12: 具身常驻中枢与事件驱动自主演进专家咨询请求书
│   ├── Phase 12 架构专家咨询方案.md                          # 🌟 Phase 12: 领域专家具身常驻演进方案（形式化模型与自愈架构）
│   ├── Phase 12 架构专家协助调查与终审验收委托书.md          # 📋 Phase 12 全量交付：架构专家终审验收申请与协助调查委托书
│   ├── Phase 12 架构专家终审验收报告.md                      # 🏆 Phase 12: 领域专家终审验收裁决报告（准予终审通过 Conditional Pass）
│   ├── Phase 12 全套工程交付终审验收裁决报告.md              # 🏆 Phase 12: 项目负责人终审验收裁决报告（全量 ACCEPTED）
│   ├── Phase 12 全量内核防御加固与生产基线专家复验申请委托书.md # 📋 Phase 12 全量内核防御加固与生产基线专家复验申请委托书
│   └── Phase 12 内核加固复验审计.md                          # 🏆 Phase 12: 外部专家全量内核防御加固穿透式复验报告（准予生产基线固化 ACCEPTED）
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
│   ├── TASK_DEV_PHASE10C_WEB_CONSOLE.md                     # 🌟 Phase 10C 零构建 Web 控制台协同与透视画布实施任务卡片
│   ├── TASK_DEV_PHASE11A_CONTEXT_GC.md                      # 🧠 Phase 11A 上下文尾端暂态报错自愈剪枝任务卡片
│   ├── TASK_DEV_PHASE11B_TOKEN_GRID.md                      # 🧠 Phase 11B DeltaNet 1024-Token 网格对齐与遥测自检任务卡片
│   ├── TASK_DEV_PHASE11C_SUBAGENT_WORKTREE.md               # 🧠 Phase 11C 子智能体 Git Worktree 隔离与对立证伪审查任务卡片
│   ├── TASK_DEV_PHASE11D_MACRO_CRYSTALLIZATION.md           # 🧠 Phase 11D 工作流宏结晶与断点热交接任务卡片
│   ├── TASK_DEV_PHASE12A_EVENT_BUS.md                       # 🏛️ Phase 12A 事件驱动文件总线与单飞常驻守护进程任务卡片
│   ├── TASK_DEV_PHASE12B_SLOT_SIDECAR.md                    # 🏛️ Phase 12B 推理端点状态 Sidecar 与四级 Saga 热恢复任务卡片
│   ├── TASK_DEV_PHASE12C_SUBAGENT_RELAY.md                  # 🏛️ Phase 12C 三阶多角色子智能体接力流水线任务卡片
│   └── TASK_DEV_PHASE12D_MACRO_EVOLUTION.md                 # 🏛️ Phase 12D 宏技能健康度账本与自主热重结晶自愈任务卡片
│
├── handover/               # 跨阶段工程交接与会话状态迁移档案 (Handovers)
│   └── HANDOVER_PHASE10_TO_PHASE11.md                       # Phase 10 至 Phase 11 跨会话环境事实与工程交接档案
│
├── ops/                    # 运维操作流程与宿主配置手册 (Operations)
│   └── OPS_LLAMA_SERVER_UPGRADE.md                          # 远程模型宿主 (Tesla M40) 服务升级与运维 SOP
│
└── notes/                  # 研发笔记、阶段沟通汇报与技术沉淀 (Notes & Reports)
    ├── report.md                                            # 阶段交付工程师自测与沟通汇报单
    ├── audit.md                                             # 阶段交付推送前最终安全与合规审计报告
    └── TODO修改shell套件.md                                 # 工作目录 (cwd) 参数化与上下文暂态报错清理优化笔记
`

---

## 📌 核心文档速查索引

### 1. 系统契约与设计规范
- **[SPEC.md](SPEC.md)**：定义系统架构底座、万物皆文件持久化契约、Git 式 DAG 分支模型、ReAct 消息格式与工具分发协议，是整个仓库最权威的规格书。

### 2. 架构演进与专家咨询 (RFC)
- **[Phase 12 内核加固复验审计](<rfc/Phase 12 内核加固复验审计.md>)**：🏆 领域专家针对五大安全防御加固补丁与高危代码净化工程出具的终审穿透式复验裁决书（评分 98.5，准予生产基线固化 ACCEPTED），签发 Milestone Commit 晋级许可。
- **[Phase 12 全量内核防御加固与生产基线专家复验申请委托书](<rfc/Phase 12 全量内核防御加固与生产基线专家复验申请委托书.md>)**：📋 项目负责人针对五大防御加固补丁（纯文件心跳、二部图合成回执、Saga大文件/软链接拦截、HMAC票据防伪、宏解包与缓存失效）与高危代码彻底净化工程签发的复验申请委托书。
- **[Phase 12 全套工程交付终审验收裁决报告](<rfc/Phase 12 全套工程交付终审验收裁决报告.md>)**：🏆 项目负责人终审复验裁决书，全量 ACCEPTED 批准通过 Phase 12 具身常驻中枢与事件驱动自主演进系统（全量 456 项测试通过）。
- **[Phase 12 架构专家终审验收报告](<rfc/Phase 12 架构专家终审验收报告.md>)**：🏆 领域专家终审复验裁决书，准予终审通过（Conditional Pass）并签发里程碑晋级许可，附带 5 项零依赖内核级防御加固路线图。
- **[Phase 12 架构专家协助调查与终审验收委托书](<rfc/Phase 12 架构专家协助调查与终审验收委托书.md>)**：📋 项目负责人签发的专家终审验收与协查委托书，确立四大核心协查议题、代码地图与验收标准。
- **[Phase 12 架构专家咨询方案](<rfc/Phase 12 架构专家咨询方案.md>)**：🌟 领域专家针对 7×24h 具身常驻演进出具的权威咨询方案，涵盖截尾宏块、四级可逆 Saga、反合一聚类与座舱防偏靶。
- **[Phase 12 架构专家咨询申请请求书](rfc/ARCHITECTURE_RFC_PHASE12_EXPERT_CONSULTATION_REQUEST.md)**：🚀 Phase 12 战略筹备期向领域专家发起的深度咨询与前沿调研请求。
- **[Phase 11 架构专家终审验收报告](<rfc/Phase 11 架构专家验收报告.md>)**：🏆 领域专家终审复验裁决书，全量 ACCEPTED 通过 Phase 11 四大认知自治工程（分项评分 94~97 分）。
- **[Phase 10 验收裁决书与 Phase 11 架构前瞻](<rfc/Phase 10 验收与 Phase 11 架构前瞻.md>)**：🏆 领域专家终审复验裁决书与 Phase 11 认知自治蓝图。
- **[Phase 10 架构专家方案研究](<rfc/Phase 10 架构专家方案研究.md>)**：🌟 领域专家针对 Phase 10 与零构建 Web 演进出具的权威评审报告。
- **[Phase 8 & 9 专家咨询备忘录](rfc/ARCHITECTURE_RFC_PHASE8_PHASE9_CONSULTATION.md)**：涵盖 AST 顶层零 Call 纯度定理、坐标系反畸变与自主因果回溯机制。

### 3. 工程交接与运维 (Handover & Ops)
- **[HANDOVER_PHASE10_TO_PHASE11.md](handover/HANDOVER_PHASE10_TO_PHASE11.md)**：跨会话系统级事实、路径规范与架构不变性交接档案。
- **[OPS_LLAMA_SERVER_UPGRADE.md](ops/OPS_LLAMA_SERVER_UPGRADE.md)**：记录在 Tesla M40 宿主机上部署 Qwen3.8-27B 时的关键参数设定。
