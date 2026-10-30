# RFC: Qwen3.8 + llama.cpp 本地长程 Agent 架构深度咨询备忘录 (Phase 11 前瞻展望)
**Architectural Consultation Brief: Phase 11 Vision — Active Context Garbage Collection, DeltaNet Inference Co-Design, Hierarchical SubAgent Governance & Autonomous Macro Crystallization**

- **文档状态**：战略前瞻与征求专家意见稿（RFC / Request for Comments — Phase 11 Vision）
- **发起团队**：Qwen38-llama-cpp-harness 项目架构组（项目负责人 / 首席架构师 / 主程序员）
- **核心哲学**：KISS 原则、奥卡姆剃刀、万物皆文件（Unix-like 磁盘权威事实）、零构建（Zero-Build）前端、极简依赖（严格锁定 4 项 Python 基础库：`openai`, `mss`, `pillow`, `websocket-client`）
- **面向专家**：大模型推理引擎与 KV Cache 状态恢复专家、长程自主 Agent 状态机与认知记忆研究员、操作系统与 RPA 宏编排专家、系统可靠性架构师
- **项目仓库**：`Qwen38-llama-cpp-harness-windows`（基线 Commit [`6f77a19`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/6f77a19) + Phase 10 就绪底座）

---

## 一、技术演化全景与 Phase 11 战略拐点

经过 Phase 1 至 Phase 10 的系统性攻坚，本运行时已在 Windows 单机物理环境下打通了操作系统的全链路执行能力：
- **执行与沙箱内核**：Win32 Job Object（4GB 限制、`KILL_ON_JOB_CLOSE`）、Per-Monitor V2 物理锁定、纯 ctypes COM UIA 看门狗、6 步防挂起窗口激活、64 位原子剪贴板；
- **治理与认知自愈**：AST 顶层零 Call 纯度定理与技能准入沙箱、三振隔离机制、四维抗振荡自主因果回溯（`branch_rewind`）、五段式账本第 4 节红线保真；
- **现实桌面闭环**：Chromium 辅助树动态唤醒、双模视觉-语义对准、高清网格切片几何双射、零构建 Web Canvas 锐利透视控制台。

### 🌟 战略矛盾转移（The Core Paradigm Shift）
当单机单步操作的“执行层阻抗”被基本消除后，系统在数百步、多任务长程深水区的核心矛盾发生了根本性升维：
> **从“单步能否点准、窗口能否切对”，升维至：在长达数十轮、数百步的复杂因果演进中，如何保持工作内存（Prompt）注意力绝对纯净？如何让客户端与底层混合循环推理引擎（DeltaNet）实现纳秒级缓存共振？如何让子智能体产出具备硬性契约审查？以及如何将重复摸索的 GUI 经验结晶为零 Token 消耗的确定性宏？**

---

## 二、议题一：上下文暂态自愈剪枝与自适应注意力保洁 (Active Context Garbage Collection)

### 1. 现状痛点与认知污染
在当前长程 ReAct 循环中，智能体面对复杂任务时常常经历局部试错。例如：修改代码时因模块拼写错、参数不匹配连续报错 2~3 次，在第 4 次修正成功。
- **磁盘 vs 内存的认知混淆**：目前系统将所有中间试错的错误调用及其冗长异常栈（Traceback）完整保存在向模型发送的 `self.messages` 消息序列中；
- **注意力污染（Attention Drift）**：冗长的历史报错不仅消耗数百至上千宝贵 Token，更形成了密集的“负向示例（Negative Demonstrations）”。大模型具有很强的上下文诱导敏感性，历史中的报错片段极易诱发模型在后续步骤中复现相似的语法或逻辑幻觉；
- **研发备忘录共鸣**：正如项目笔记所指出的——*“重复的报错留在上下文中没有意义。如果一旦通过了就不再保留报错信息了。从上下文中把报错都剃出去，别污染上下文给模型上压力”*。

### 2. 拟设计架构方案：权威 DAG 与投影工作内存“双轨制”
1. **磁盘权威不可篡改（Authoritative DAG Immutable）**：
   磁盘上的 `checkpoint.jsonl` 与分支历史完整保留每一次试错节点，保证 100% 事后白盒审计与因果追溯能力；
2. **工作内存垃圾回收（Context Garbage Collection / Ephemeral Pruning）**：
   在内存层的 `ContextManager` 中引入**“暂态试错折叠管道”**：
   - 当某个技术动作最终成功（如工具返回 `exit=0` 或有效执行结果）时，GC 管道自动扫描上溯该局部的试错链；
   - 将先前的连续报错轮次从向模型发送的活跃视口中安全剥离，折叠为一条紧凑的因果结晶徽标：  
     `[Transient Attempt Pruned: 2 prior attempts (ModuleNotFoundError, SyntaxError) resolved by fixing import path]`；
3. **OpenAI 消息拓扑严格保真**：
   严格保证 User / Assistant / Tool 角色的交替拓扑合法性，杜绝因剪枝引发的协议校验失败。

### 3. 请教领域专家的问题 (Questions for Experts)
> 1. **Prompt 剪枝与 KV Cache 前缀命中的帕累托最优权衡**：
>    在依赖前缀复用（Prefix Caching）的推理服务端，对历史上下文进行中间段剪枝或改写，不可避免地会击穿该修改点之后的 KV Cache，引发局部重算。在 30~100 步的长程任务中，从数学期望与工程经验来看，“容忍局部 KV Cache 重算以换取模型注意力的高度纯净与总 Token 压缩”，与“绝对维持前缀不动但承受错误噪音污染”相比，在何种步数规模或错误长度下剪枝具有压倒性收益？
> 2. **自适应剪枝触发的确定性启发式规则**：
>    除简单的 `exit=0` 之外，针对多轮复杂的连续修改（如涉及跨工具写文件、打补丁、跑测试），工业级认知 Agent 中有哪些严谨的状态机判据来确定一个“局部试错事务（Local Trial Transaction）”已彻底闭环、可以安全执行上下文 GC？

---

## 三、议题二：推理端软硬协同 —— DeltaNet 1024-Token 边界网格对齐 (Inference-Harness Co-Design)

### 1. 物理现实与硬件契约
根据 [`OPS_LLAMA_SERVER_UPGRADE.md`](../ops/OPS_LLAMA_SERVER_UPGRADE.md)，远程模型宿主（Tesla M40 24GB）采用升级后的启动配置：
`--ctx-checkpoints 64 --checkpoint-min-step 1024 --cache-ram 16384 --no-cache-idle-slots`。
该配置开启了 16 GiB 宿主机内存池，每隔 1,024 个 Token 为 Qwen3.8 的 48 层 DeltaNet 隐藏循环状态（Recurrent State）与 16 层注意力 KV Cache 保存一次内存检查点。

### 2. 现状失配与算力损耗
目前客户端在生成 Prompt 时，Token 长度完全随对话内容自由增长（例如第 15 步总长 14,321 Token，第 16 步总长 15,108 Token）。
- 当客户端触发 Phase 9A 的 `branch_rewind` 或分支切换时，回溯目标节点所在的 Token 位置极大概率落在两个 1,024 物理快照的“夹缝”中（例如目标位于第 13,800 Token，最近的前置检查点在 13,312）；
- 服务端虽然可以秒级恢复 13,312 处的快照，但仍需从 13,312 重新前向 Prefill 计算近 500 个 Token 才能到达目标位置。

### 3. 拟设计架构方案：结构化提示词网格垫片（Token Grid Alignment）
1. **关键语义段落网格化排版**：
   在系统提示词结束处、五段式事实账本结束处、以及二级纪元（Epoch）压缩摘要输出时，客户端计算当前的预估 Token 偏移量；
2. **确定性语义垫片（Semantic Padding / Anchor Blocks）**：
   设计无害的语义闭合块（如结构化元数据留白、版本签名注释），引导长期固定的“骨架前缀”长度精准收敛在 $1,024 \times K$ 的整倍数边界上；
3. **因果快照双向校准探针**：
   在客户端建立轻量级的 Token 边界预估模型，使每次创建重要 DAG 节点（如大阶段里程碑、分支决策分叉点）时，优先对齐服务端物理快照存储点。

### 3. 请教领域专家的问题 (Questions for Experts)
> 1. **Qwen3.8 混合架构检查点恢复的数值一致性**：
>    Qwen3.8 采用线性循环层（DeltaNet）与局部自注意力（Attention）的混合拓扑。在从 RAM 宿主快照恢复 DeltaNet 隐藏状态并前向推理数万 Token 的深层长程分支中，循环层是否存在因定点/半精度累积导致的数值漂移（State Drift）？有无推荐的轻量级隐状态因果一致性自检算法？
> 2. **Token 物理对齐的有效边界设计**：
>    在 Jinja 模板与 ChatML 格式限制下，通过填充结构化注释（如 `<!-- pad: N tokens -->`）实现 1024 整数倍对齐，是否会被模型的自注意力机制误判为有效语义而产生不良诱导？在开源长文本引擎生态中，有无更原生、更透明的 Token 对齐协议？

---

## 四、议题三：分层子智能体结构化产物契约与隔离审查门禁 (Hierarchical SubAgent Artifact Contract)

### 1. 现状局限
目前系统严格维持 `AGENTS.md` 规定的“单 SubAgent 串行执行（Single-SubAgent Execution）”物理红线，子智能体通过 `active.lock` 互斥。然而：
- 子智能体任务完成后，仅向父智能体回传一段 $\le 2000$ 字符的自由文本总结；
- **产物不可控**：子智能体若在过程中对工作区文件产生了破坏性改动（但未在文本中如实交代），父智能体无法感知；
- **缺乏审查与回滚门禁**：父智能体在主循环中只能盲目接受子任务结果，缺乏像代码审查（Code Review）一样的合入决策权。

### 2. 拟设计架构方案：工作区 Git 临时沙箱与产物卡片审查
1. **临时分支环境隔离**：
   子智能体被派生时，系统自动在后台为其创建独立的临时任务工作区分支（`subtask_<uuid>`），子智能体的一切修改均记录在隔离环境中；
2. **标准结构化产物交付卡片（Artifact Delivery Card）**：
   子智能体完成任务时，必须生成符合严格 JSON Schema 的标准化交付卡片：
   - `files_modified`: 变动文件清单与 Git Diff 补丁；
   - `tests_executed`: 自动化测试执行结果与退出码；
   - `risk_assessment`: 影响范围与破坏性等级评定；
   - `checkpoint_anchor`: 子任务分多分枝的最终回溯锚点；
3. **父级双阶段合并/驳回门禁（Fast-Forward or Revert Gate）**：
   - 父智能体接收到结构化产物卡片后，在主上下文执行专属的“审查决策轮（Review Step）”；
   - 若审查通过：触发无冲突快进合并（Fast-Forward Merge），将成果合入主任务上下文；
   - 若审查不通过：一键触发 Revert 丢弃子任务分支，并自动将子任务失败原因沉淀至五段式账本第 4 节，主环境纤毫不染。

### 3. 请教领域专家的问题 (Questions for Experts)
> 1. **单实例互斥下的多角色流水线编排**：
>    在严格遵守“同一时刻全系统至多一个 SubAgent 活跃”的前提下，如何设计最优的单向任务流水线（如 `代码调研 Agent` $\to$ 完成释放 $\to$ `开发 Agent` $\to$ 完成释放 $\to$ `测试验证 Agent`）？各子智能体之间如何通过磁盘文件进行无损且防篡改的产物移交？
> 2. **父智能体审查能力的 Prompt 架构设计**：
>    对于 10B~30B 量级的主干模型，如何设计专用的审计提示词模板，使其能够从子智能体回传的 Diff 与测试报告中敏锐捕捉潜在破坏性风险，而不会产生盲目信任（Over-Trust）或无理驳回？

---

## 五、议题四：复杂桌面工作流“宏结晶”与自举编译 (Macro Crystallization & Enterprise Workflows)

### 1. 现状局限
目前智能体操控复杂桌面工作流（例如：打开内部系统网页 $\to$ 登录下载报表 $\to$ 打开本地 Excel 清洗数据 $\to$ 导入 SQL 数据库 $\to$ 邮件通知）完全依靠大模型在 ReAct 循环中逐个 Token 预测。
- 单个常规业务流需消耗 40~60 个交互 Step，单次执行耗时数分钟；
- 推理成本高昂，且偶发在第 35 步因网络或界面抖动中断。

### 2. 拟设计架构方案：从轨迹蒸馏到声明式技能结晶
1. **成功轨迹自捕获（Trajectory Auto-Capture）**：
   当智能体在 DAG 某条分支上成功完成多步骤桌面流程并通过 TDD 验收后，系统分析该分支上的工具调用链（`chrome_dom`、`window_manager`、`grounding`、`clipboard`、`shell`）；
2. **确定性 Python 宏代码合成（Macro Synthesis）**：
   运行时将这一串离散的 ReAct 动作编译合成一个高内聚、带容错重试的独立 Python 宏脚本；
3. **通过 Phase 8B 技能沙箱自动升级为原生 Skill**：
   将该宏脚本交由 `core/skill_sandbox.py` 进行 AST 顶层零 Call 纯度核验、JSON Schema 校验与 `sys.addaudithook` 干运行测试，原子升级部署至 `skills/macro_<name>.py`；
4. **0-Token 极速重放与自愈退避**：
   在后续面对同类任务时，智能体直接调用该结晶宏（数秒内完成，0 模型推理 Token 开销）；仅当宏执行报错时，才自动退避唤醒大模型 ReAct 循环进行自愈介入。

### 3. 请教领域专家的问题 (Questions for Experts)
> 1. **GUI 离散动作流向确定性代码编译的反向抽象**：
>    在复杂的动态桌面环境下，如何从一次成功的特定参数执行轨迹中，提取出通用的“变量插值槽位（如日期、文件名、过滤条件）”与“鲁棒重试条件”？工业级 RPA 在从轨迹自动生成健壮代码方面有哪些成熟的形式化方法？
> 2. **宏技能的生命周期与陈旧失效检测**：
>    当被控软件界面改版或网页 DOM 结构调整导致结晶宏失效时，系统如何实现最小成本的自动化失效探测，并触发重新自适应探索？

---

## 六、系统不可动摇之工程红线与架构约束

任何前瞻规划与专家建议，必须完全受控于以下系统既定公理：
1. **Python 运行依赖绝不膨胀**：`requirements.txt` 严守 **4 项基础依赖**，全系统调用必须依托标准库 `ctypes` 实现；
2. **Web 前端纯原生零构建（Zero-Build）**：绝不引入任何打包编译工具链，开箱即用；
3. **推理模型端硬件约束不可动摇**：`--spec-draft-n-max 4` 锁定，`--cache-reuse 0` 锁定；
4. **单 SubAgent 串行执行约束**：同一时刻全系统至多保留一个活跃 SubAgent 进程。
