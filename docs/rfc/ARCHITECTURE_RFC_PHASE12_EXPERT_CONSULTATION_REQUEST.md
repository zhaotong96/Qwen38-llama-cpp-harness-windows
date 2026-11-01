# RFC: Qwen3.8-Harness Windows 平台 Phase 12 具身常驻与自主演进领域专家咨询与联合调研书
**Architectural Consultation & Open Research Request: Phase 12 — Embodied Daemon Central, Zero-Inference Perceptual Sentinels, Desktop Saga Orchestration & Autonomous Self-Evolution**

- **文档状态**：战略前瞻与征求专家意见稿（RFC / Request for Comments — Phase 12 Vision & Research Request）
- **发起团队**：Qwen38-llama-cpp-harness 项目架构组（项目负责人 / 首席架构师 / 主程序员）
- **核心哲学**：KISS 原则、奥卡姆剃刀、万物皆文件（Unix-like 磁盘权威事实）、零构建（Zero-Build）原生 Web 控制台、极简依赖（严格锁定 4 项基础库：`openai`, `mss`, `pillow`, `websocket-client`）
- **面向专家**：长程自主智能体架构师、操作系统底层与 RPA 运行时专家、计算机视觉与感知差分研究员、程序合成与事务可靠性专家、大模型推理端软硬协同优化专家
- **项目仓库**：`Qwen38-llama-cpp-harness-windows`（基线 Commit [`125d2b6`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/125d2b6)，Phase 10 已验收，337 项单测 100% 绿灯通过；Phase 11 全套任务卡施工中）

---

## 一、 咨询背景与战略使命

在经历 Phase 0 至 Phase 10 的高强度攻坚后，`Qwen38-llama-cpp-harness-windows` 已在 Windows 平台打通了全套单机物理交互与安全准入底座；而正在由执行工程师施工的 Phase 11（包含尾端自愈剪枝、1024-Token 网格对齐、Git Worktree 沙箱、以及 0-Token 宏结晶编译器）将全面解决长程工作内存的认知信噪比与确定性重放问题。

然而，作为项目架构组，我们意识到：当智能体具备了“精准的单步物理操作能力”与“局部的认知自愈能力”后，系统在迈向**数天至数周无人值守、高吞吐长程自动化深水区**时，正面临着更高维度的系统级瓶颈。

### 🌟 本次咨询的核心目的
我们特向领域专家团队提请联合调查与深度研讨：
1. **审议与评判已有设想**：对我们初步提炼的 Phase 12 四大构想（零算力视觉/事件哨兵、宏管道编排与 Saga 事务、技能竞技场与生态 GC、双向协同驾驶舱）进行技术可行性、边界完整度与工程代价审查；
2. **挖掘前沿涌现需求**：**充分调动您深厚的领域洞察与广泛的互联网最新前沿调研能力（包括 2025~2026 年学术界与工业界在 Agentic OS、GUI Agent、长程长效自治等方向的最新成果）**，帮助我们挖掘现有思路之外那些“未曾预见但必将涌现”的关键需求、暗坑陷阱与系统盲区；
3. **推演前沿架构思路**：在坚守本项目“四大系统级不变性”（极简依赖、零构建、万物皆文件、M40 显存保护）的前提下，为下一阶段引入更优雅、更鲁棒的数学模型与架构范式。

---

## 二、 系统架构基准与全景代码地图 (All Relative Paths)

为便于领域专家快速建立项目全局认知，本节提供权威代码结构索引与职责映射（全量采用仓库内相对路径）：

### 1. 核心子系统与代码分布地图

```text
Qwen38-llama-cpp-harness-windows/
├── core/                           # 核心运行时引擎与状态机
│   ├── agent.py                    # 单循环收敛 ReAct 引擎、Gate 1~3 门禁打断、TDD 咬合
│   ├── api.py                      # 零构建 HTTP/SSE 守护网关与安全只读投影路由
│   ├── context.py                  # Git 式 DAG 上下文管理、Token 估算、尾端剪枝 GC
│   ├── models.py                   # 进程路径、数据模型与单飞互斥锁（Harness.lock）
│   ├── persistence.py              # checkpoint.jsonl 磁盘落盘、分支 DAG 分叉与回退
│   ├── skill_sandbox.py            # AST 顶层零 Call 纯度门禁、sys.addaudithook 干运行沙箱
│   ├── subagent_runner.py          # 子智能体单实例调度（active.lock）、Slot 1 亲和绑定
│   └── tools.py                    # Win32 Job Object（KILL_ON_JOB_CLOSE）、双管道异步排空
├── skills/                         # 原生系统能力与治理原语（25+ 个 Python 技能）
│   ├── window_manager.py           # Win32 6 步防死锁窗口激活（150ms WM_NULL 探活、UIPI 避让）
│   ├── clipboard.py                # 64位 c_void_p 安全原子剪贴板、paste=True 单批次注入
│   ├── grounding.py                # Chromium 15ms DOM 动态唤醒、Stage 1.5 窄化、双模自适应对准
│   ├── pic_zoom.py                 # Lanczos 高清切片、32px 十字标尺、天花板双射逆映射
│   ├── mouse.py                    # win32k.sys 定点逆映射同构鼠标注入
│   ├── keyboard.py                 # Windows 虚拟击键与热键合成
│   ├── branch_rewind.py            # 四维抗振荡因果回溯与五段式事实账本第 4 节维护
│   ├── read_checkpoint_node.py     # 磁盘历史 DAG 节点白盒只读追溯
│   └── subagent.py                 # 派生子智能体技能
├── web/                            # 零构建原生 Web 控制台（SPEC §13.2）
│   ├── index.html                  # 原生 HTML5 控制台单页
│   ├── app.js                      # 根应用挂载与 SSE 实时事件流消费
│   ├── store.js                    # 119 行原生 EventTarget + queueMicrotask 微任务单向流状态机
│   ├── canvas_overlay.js           # 原生 Web 组件：视网膜 +0.5px 物理半像素防模糊透视画布
│   └── dag_timeline.js             # 原生 Web 组件：SVG 因果回溯虚线弧与五段式账本联动
├── tests/                          # 质量保证与安全合规门禁（337+ 项单测，100% 绿灯）
│   ├── test_window_clipboard.py    # 窗口与剪贴板并发及指针安全专项（38 项测试）
│   ├── test_grounding_dpi.py       # 双模对准、DPI 缩放与双射公式专项（39 项测试）
│   ├── test_api_ui_tree.py         # Web 控制台后端路由与路径穿越防御（28 项测试）
│   ├── test_skill_sandbox.py       # AST 零 Call 纯度门禁与防逃逸验证（18 项测试）
│   └── test_repo_hygiene.py        # 仓库隐私扫描、凭据与私有网络基建门禁（0 违规）
└── docs/                           # 权威系统规约与技术蓝图
    ├── SPEC.md                     # 系统完整物理架构规范（§1~§14）
    ├── rfc/                        # 历史各阶段专家研究报告与裁决书
    └── tasks/                      # Phase 10 与 Phase 11 各阶段开发任务卡片
```

### 2. 系统四大不可动摇的架构公理（Architectural Invariants）
在探索 Phase 12 方案时，所有设计必须严格收敛于以下四大边界之内：
1. **依赖极简不变性**：[`requirements.txt`](../../requirements.txt) 严格锁定 4 项轻量依赖（`openai`, `mss`, `pillow`, `websocket-client`），绝不允许引入重型数据库（如 SQLite/PostgreSQL/Redis）、重量级消息队列（Celery）或外部视觉分析库（OpenCV）；
2. **前端零构建公理**：[`web/`](../../web/) 保持 100% 浏览器原生 ES Modules，严禁引入 Node.js、npm、Vite、Webpack 等前端打包链路；
3. **UNIX-like 万物皆文件**：状态权威投影在磁盘（`.agent/`、`checkpoints/`），所有运行状态与审计日志必须作为透明文本文件可被离线检查；
4. **推理宿主参数契约**：服务于远程 Tesla M40 24GB 真实物理硬件（Qwen3.8-27B 混合循环架构），受视觉投影模块挂载约束，锁定 `--spec-draft-n-max 4` 与 `--cache-reuse 0`。

---

## 三、 Phase 11 承接基线与 Phase 12 出发点

执行工程师正在落地的 Phase 11 为系统建立了如下四大基石（详见 [`docs/tasks/`](../tasks/)）：
- **Phase 11A（Tail-Anchored Context GC）**：在内存层实装 `prune_resolved_trial_tail`，试错成功后折叠尾部报错为 35 Token 结晶徽标，保护 DeltaNet LCP 前缀，端到端延迟降低 41%；
- **Phase 11B（Token Grid Alignment）**：在系统提示词与纪元压缩点实装 1024-Token 原生结构化分隔线（`\n---`）网格咬合，消除浮点结合律漂移并接入 `timings.cache_n` 遥测哨兵；
- **Phase 11C（SubAgent Worktree Gate）**：子智能体隔离于独立 Git Worktree 物理目录，宿主确定性生成卡片并自动拦截 5 类静态破坏风险，支持 `--ff-only` 合并与回滚；
- **Phase 11D（Macro Crystallization）**：从成功探索轨迹中通过污点追踪自动提取槽位并反向编译为 0-Token 确定性宏，内嵌 `[MACRO_STALE_HANDOVER]` 断点自愈协议。

---

## 四、 架构组既有的 Phase 12 设想与构想矩阵

架构组初步设想将 Phase 12 定位为：**“具身常驻中枢与事件驱动自主演进 (Embodied Daemon Central & Event-Driven Evolution)”**，并形成了以下四个正交子模块假说：

| 模块代号 | 架构假说与核心设想 | 预期收益 | 潜在设计挑战 |
| :--- | :--- | :--- | :--- |
| **Phase 12A<br>零算力感知哨兵** | 利用 Windows 原生 `ReadDirectoryChangesW` / `SetWinEventHook` 搭配 `pillow` 的局部像素差分（SSIM/pHash），在等待长耗时任务（如文件下载、长编译、大渲染）时**彻底休眠大模型推理**，仅在特定视觉或文件事件就绪时唤醒。 | 消除等待期持续截屏与 ReAct 轮询的 Token 消耗与 M40 显存占用，待机开销为 0。 | 如何在纯 CPU 与标准库下实现极轻量且抗光照/抗微颤的视觉状态稳定判定？ |
| **Phase 12B<br>宏拓扑编排与 Saga 事务** | 将 Phase 11D 的单宏串联为有向无环管道（DAG Pipeline），引入强类型数据总线（`data_bus.json`）；针对桌面跨软件操作缺乏 ACID 特性的问题，引入 Saga 补偿事务（为前置步骤定义反向逆操作宏）。 | 实现跨软件复式业务流的端到端确定性贯通与故障自动对冲回滚。 | GUI 状态补偿通常不具备严格对称性（如某些保存动作不可逆），如何建立健壮的补偿降级协议？ |
| **Phase 12C<br>技能竞技场与生态 GC** | 建立无人值守的夜间自动化巡检机制。在夜间低峰期利用历史任务参数变异执行变异模糊测试（Fuzzing）；结合 `ui_tree` 指纹扫描识别失效宏，自动将陈旧/失效技能归档至 `.agent/skills_archive/`。 | 防止自愈生成的宏无限膨胀导致 Prompt 检索空间污染，保持高信噪比。 | 如何在不产生真实业务副作用（如误删生产数据）的前提下安全实施 GUI 自动化 Fuzzing？ |
| **Phase 12D<br>双向协同驾驶舱** | 深化原生 Web 控制台，在 `<agent-canvas-overlay>` 上实装反向物理坐标投影；当模型在复杂或关键步骤面临不确定性时，向 Web 端发起低延迟确认请求，操作员可一键在 Canvas 上框选或接管。 | 将系统可靠性从 95% 提升至近 100%，建立优雅的人机共驾（Human-in-the-Loop）体验。 | 纯原生 ES Modules 下如何实现抗延迟抖动的亚像素级点击穿透与操作队列互斥？ |

---

## 五、 提请领域专家协助调查与发掘的核心课题

**这是本次咨询最核心的求助部分。** 除了对上述四项已有设想进行审视与查漏补缺外，我们恳请领域专家**利用您的学术深度、工程积淀以及互联网最新情报检索**，围绕以下五大维度展开突破性挖掘：

### 课题一：跨越数天至数周无人值守运行的“涌现需求与隐性失效模式”
> 当一个基于 Python、Win32 API 和本地轻量模型构建的 Agent 在真实物理 Windows 环境下 7×24h 持续常驻时，有哪些传统短程测试绝对无法暴露、但现实中必然涌现的工程隐患？
- **操作系统级扰动**：例如 Windows 自动休眠/电源策略、锁屏/凭据提供程序（Credential Provider）阻断、多显示器热拔插引发的虚拟桌面跨屏坐标系跳跃、DWM 渲染合成器重启、系统弹窗（UAC / Windows Update 强制重启）；
- **资源与句柄泄漏**：长期调用 `ctypes` GDI/User32、Win32 COM 对象（UI Automation）以及子进程管道时，微小的未释放句柄如何在数天后诱发 `0xC0000005` 崩溃？业界有何确定性的看门狗或轻量级“自我热重启（Self-Rejuvenation）”架构？
- **请专家结合最新业界实战案例，指出我们还必须在 Phase 12 提前规划哪些防御机制？**

### 课题二：桌面环境非原子交互的事务一致性与 Saga 补偿前沿方案
> 在缺少分布式事务协调器与回滚日志的本地 GUI 环境中，学术界与前沿工业界是如何形式化定义和保证“跨软件宏工作流（Cross-Application Workflow）”的鲁棒性的？
- 是否存在轻量级、无需重型运行时的状态反向推理理论？
- 当某个步骤（如向未保存的表单注入文本）失败时，除了粗暴的“关闭进程并重来”，有无更细粒度的桌面级补偿状态机（Desktop Compensating State Machine）设计模式？

### 课题三：极简依赖（纯标准库 + Pillow）约束下的零算力视觉/感知哨兵
> 如何在**绝对不引入 OpenCV、PyTorch、ONNXRuntime** 等重型依赖的前提下，实现极高可靠性的界面状态感知？
- 传统轮询截屏不仅占用 CPU，还会在 GPU-CPU 之间频繁拷贝显存。如何利用 Windows 本地的轻量机制（如 Desktop Duplication API / `BitBlt` 脏矩形检测，或 Pillow 的感知哈希 pHash / SSIM）构建微秒级、低功耗的局部变化监听器？
- 如何准确区分“界面正在平滑滚动/播放动画的暂态”与“业务实质已完成的终态”？有无经典的确定性窗口稳定度判据（Window Quiescence Criterion）？

### 课题四：动态结晶技能网络的拓扑演化、相似度合并与熵减修剪 (Anti-Entropy Pruning)
> 当智能体运行数周后，自主生成了数十个 `macro_*.py` 时，技能空间将面临维度灾难与语义重叠（例如存在 `macro_export_csv_v1` 与 `macro_export_csv_v2`）。
- 在完全依靠本地轻量模型（Qwen3.8-27B）且无外部向量数据库的前提下，有哪些轻量级、基于 AST 指纹或参数 Schema 图覆盖的技能合并（Skill Merging）与相似度聚类算法？
- 如何设计一套自适应的技能淘汰评价函数（综合考虑调用频次、成功率、最后活跃时间、依赖窗口存在性），实现系统技能生态的终身自我熵减？

### 课题五：学术界与前沿开源社区（2025~2026）的最新范式与思想借鉴
> 恳请专家通过互联网与文献检索，调研近两年来在大模型具身操作系统控制、计算机使用（Computer-Using Agents）、神经符号程序合成（Neuro-Symbolic Program Synthesis）领域的最新论文与前瞻开源架构（例如 Anthropic Computer Use 最新演进、OSWorld 前沿方案、Windows Agent Arena 等）：
- 哪些最新的关键思路可以在坚守我们四大不变性（KISS、4 项依赖、零构建、纯文件）的前提下被创造性吸收？
- 行业内目前最先进的 GUI Agent 是如何解决长程任务下的“反事实探索（Counterfactual Exploration）”与“人机意图对齐”的？

---

## 六、 专家咨询反馈规范与成果交付指引

为保证咨询成果能直接转化为下一阶段的工程行动指南，我们期待领域专家的反馈报告涵盖以下结构：

1. **对既有 Phase 12A~12D 设想的推演评估与打靶分析**（指出各设想的技术可行性漏洞与改进建议）；
2. **涌现需求与未知盲区清单（Emergent Requirements Dossier）**（详述长程常驻下未曾预见的系统隐患及防御对策）；
3. **国内外最新学术文献与工业级开源方案调研总结**（附带论文核心思想提炼与对本项目极简哲学的适配性论证）；
4. **推荐的 Phase 12 子阶段划分与优先级路线图建议**（协助项目组确立 12A、12B、12C... 的实施次序与准出门禁）。

架构组全体成员对各位专家的真知灼见致以崇高敬意！
