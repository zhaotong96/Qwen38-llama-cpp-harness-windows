# RFC: Qwen3.8 + llama.cpp 本地长程 Agent 运行时架构深度咨询备忘录
**Architectural Consultation Brief: Native Long-Running Agent Runtime on Windows & llama.cpp**

- **文档状态**：征求专家意见稿（RFC / Request for Comments）
- **发起团队**：Qwen38-llama-cpp-harness 项目架构组
- **核心哲学**：KISS 原则、奥卡姆剃刀（如无必要勿增实体）、万物皆文件（Unix 哲学）、明文标记排斥内存黑箱、降低人类可维护性与阅读心智负担
- **面向专家**：LLM 推理系统专家、`llama.cpp` 核心贡献者、长程 Agent 架构师、Windows 系统开发专家
- **项目仓库**：`Qwen38-llama-cpp-harness-windows`
- **核心系统栈与真实基准依赖**：
  - **推理宿主 (Model Host)**：Linux x86_64, NVIDIA Tesla M40 24GB (CUDA 13.0, Driver 580.173, Persistence-M On)
  - **推理引擎 (Engine)**：`llama.cpp` 0.6.0-dev (build 1, commit `a66a2ca`, GNU 13.3.0)
  - **主干模型 (Model)**：`JonathanColetti.Qwen3.8-27B-Uncensored-GGUF` (`Qwen3.8-27B-Uncensored-IQ4_XS.gguf`, 27.32B params, 15.30 GB, 4.25 bpw)
  - **多模态投影 (Visual)**：`mmproj-Qwen3.8-27B-Uncensored-F16.gguf` (多模态输入架构)
  - **服务参数契约 (Flags)**：`-c 128000`, `-np 2`, `-ngl 99`, `--kv-unified`, `-ctk q8_0`, `-ctv q8_0`, `--spec-type draft-mtp`, `--spec-draft-n-max 4`, `--spec-draft-n-min 1`, `--jinja`, `--chat-template-file chat_template.jinja`, `--metrics`, `--load-mode none`
  - **智能体宿主 (Agent Host)**：Windows 11 / Server, Python 3.10+ (仅 4 项零构建原生依赖: `openai`, `mss`, `pillow`, `websocket-client`), 纯原生 ES Modules Web 控制台

---

## 一、核心设计哲学与架构公理

本项目在立项之初便确立了极其严苛的工程美学与架构纪律。在向各位专家展开具体技术提问前，首先阐明贯穿本项目的五大公理：

### 1.1 KISS 与奥卡姆剃刀（Keep It Simple, Stupid & Occam's Razor）
- **公理**：“如无必要，勿增实体”。
- **架构体现**：坚决拒绝在单机/局域网 Agent 体系中盲目堆砌复杂的分布式消息总线（Kafka/RabbitMQ）、重型外部数据库（PostgreSQL/Redis）、多层黑盒 Agent 编排框架（如臃肿的图调度引擎）以及繁重的 Docker/WSL 虚拟化隔离。系统追求**最小充分机制（Minimum Sufficient Mechanism）**，以最精炼的代码和系统原生能力满足全部功能。

### 1.2 Unix 哲学：万物皆文件（Everything is a File）
- **公理**：文件是操作系统最古老、最坚韧、最具普适性的抽象。
- **架构体现**：将 Agent 运行的一切状态——任务 Checkpoint、会话指针、DAG 分支头、Tag 引用、排队项、打断控制标记——全部降维映射为文件系统中的纯文本、JSONL 或空标记文件。**文件不仅是冷存储，更是跨进程共享、跨组件协作与跨会话恢复的唯一权威事实来源（Single Source of Truth）**。

### 1.3 明文文件标记排斥内存黑箱（Explicit File Markers over In-Memory Black Boxes）
- **公理**：凡是不可见的长驻内存状态，皆是系统隐患的温床。
- **架构体现**：彻底摒弃依赖常驻内存上下文、未持久化事务栈的黑盒设计。任务启动即写 `status=running`，用户打断即写 `abort.flag`，任务排队即落盘 `{priority}_{timestamp}_{id}.json`。状态转移不仅在机器端确定，在人类端完全白盒透明——任何运维人员只需通过 `ls`、`cat`、`head` 等最基本的 CLI 工具，即可在一秒内透视系统当前的全部生命周期状态。

### 1.4 崇尚人类可维护性，消灭程序员阅读与运维的心智负担（Human Maintainability & Zero Cognitive Burden）
- **公理**：优秀架构的终极指标，不是代码编写时的智力炫技，而是十年后一个陌生工程师接盘时的心智轻松度。
- **架构体现**：系统前端坚决守住**零构建（Zero-Build）红线**，仅用 5 个原生 HTML/CSS/JS 文件，不引入 npm/Webpack/Vite，双击或直接静态挂载即可运行，彻底消灭 `node_modules` 依赖地狱；后端核心调度逻辑高内聚、依赖显式注入。我们希望将任何技术决策的认知负荷降到最低。

---

## 二、真实系统拓扑与端点物理基线

### 2.1 部署拓扑 (Topology)

```
┌──────────────────────────────────────────────┐       HTTP / REST + SSE
│  Agent Host (Windows 11 / Server)            │   (OpenAI-compatible /v1)    ┌─────────────────────────────────────────────┐
│  - Python 嵌入式守护进程 (core.api:8765)      │ ──────────────────────────>  │  Model Host (Linux / Tesla M40 24GB)        │
│  - 纯原生 ES Modules Web 控制台 (web/)       │                              │  - llama-server (0.6.0-dev b1-a66a2ca)      │
│  - 一切皆文件持久化驱动 (.agent/)            │ <──────────────────────────  │  - Qwen3.8-27B-Uncensored-IQ4_XS + mmproj   │
│  - 本地执行体 (PowerShell / Win32 / CDP)     │        Stream Tokens         │  - 真实参数: -c 128000 -np 2 --kv-unified   │
└──────────────────────────────────────────────┘                              │             --spec-type draft-mtp (1~4)     │
                                                                              │             -ctk q8_0 -ctv q8_0 -ngl 99     │
                                                                              │             --jinja chat_template.jinja     │
                                                                              └─────────────────────────────────────────────┘
```

> **注**：为严格符合开源代码仓库安全与隐私红线（`tests/test_repo_hygiene.py`），文中服务器地址统一定义为 `<model-host>:8080`（内部局域网实测通过 SSH 查证服务单元与进程树）。

### 2.2 生产端点现场查证基线 (Verified via SSH, `/v1/models`, `/props` & `/slots`)
经实测取证，在线生产实例配置细节如下：
- **GPU 资源占用**：Tesla M40 24GB 显存分配约 23,446 MiB（接近打满），GPU-Util 持续在 100%；
- **双槽位并发模型 (`-np 2`)**：系统初始化 2 个 Slot，共享 `--kv-unified` 显存池；当前 Slot 1 正在处理长时推理任务（`is_processing: true`），Slot 0 空闲待命；
- **MTP 投机采样生效**：引擎层明确挂载 `--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-n-min 1`，草稿令牌数为 1 到 4；
- **自定义 Jinja 模板治理**：加载自定义 `chat_template.jinja`，内置 `<think>...</think>` 思维链隔离、`<tool_call>` JSON 封装及工具连续失败告警追踪（`⚠️ SYSTEM WARNING: X consecutive tool errors detected`）。

### 2.3 核心架构不变量 (Invariants)
1. **纯文件持久化**：`.agent/tasks/<id>/checkpoint.jsonl` 以追加写入方式固化 DAG 节点；`.agent/current_session` 纯文本记录激活任务；
2. **Git 式 DAG 任务树与单向线性投影**：任务按 Git 提交模型持久化（每个节点显式指明 `parent_id`），每次向 LLM 发起推理时，根据激活分支动态重构单向线性上下文；
3. **三道防线协作式打断熔断（3-Gate Abort Pipeline）**：
   - Gate 1：Token 级流解码中断（检测到 `abort.flag` 文件即刻 `stream.close()` 并冻结输出）；
   - Gate 2：工具前置拦截（LLM 吐出工具调用后，在进入本地执行前进行标记核验，拒绝执行）；
   - Gate 3：运行中撤销（向已受阻的步骤注入纠偏补偿消息，引导重规划）。
4. **双层分层模型（Tiered Architecture）**：主力 Agent（Main）驱动工具链；压缩器（Compactor）在上下文触碰阈值时触发，生成中文事实摘要，以 `compaction` 伪节点替换历史。

---

## 三、向领域专家咨询的核心技术议题

在上述**极简主义、文件驱动与低心智负担**的准绳约束下，我们梳理出以下 4 个必须攻克的硬核工程议题：

---

### 议题一：`llama.cpp` 128k 统一 KV 缓存（`--kv-unified`）下的前缀复用与 DAG 投影对齐

#### 1. 架构哲学诉求
我们希望在保持“上下文不过是一组简单的明文消息列表”这一极简设定的同时，最大限度压榨 `llama-server` 强大的前缀缓存（Prompt Prefix Cache），**避免引入复杂的外部向量检索或常驻内存缓存管理对象**。

#### 2. 现状与痛点
模型宿主启用了 **`-c 128000`**、**`-ctk q8_0 -ctv q8_0`** 以及 **`--kv-unified`**。
在 Tesla M40 24GB 显存已分配 23.4GB 的极端重载环境下，显存余量已不足以容纳两个孤立的完整 128k 独立缓存。在长程任务演进中（上下文达到 30k~100k 时），**Prefill（Prompt 评估）耗时极其显著**。
- **痛点 A（统一 KV 池下的 Slot 挤占）**：在 `--kv-unified` 模式下，当 Slot 1 运行长时大上下文推理时，若 Agent 向 Slot 0 发起长文本请求，两槽位如何分配有限的 Unified KV 空间？是否会触发 Slot 间缓存的强制驱逐（Eviction）导致双方首字延迟均大幅恶化？
- **痛点 B（DAG 分支回溯破坏前缀）**：当操作员在 Web 控制台执行 `checkout <branch>`、`fork` 或回溯到某一历史检查点重试时，DAG 分支重组会导致后续 Token 发生偏移，原有 Slot 的 KV Cache 彻底失效并引发数十秒的全量重算。

#### 3. 拟请教专家的核心问题
1. **Slot 绑定与亲和性（Slot Affinity）**：`llama-server` 的 OpenAI 兼容接口（`/v1/chat/completions`）当前是否有稳定、低侵入的机制（如特定 Header 或请求字段）支持将特定 `task_id` 显式绑定到指定的 Slot，防止多任务并发轮询时 Slot 被无差别换出？
2. **前缀缓存的最佳序列化实践**：在 Jinja 模板渲染下，工具定义列表（`tools` schema）的顺序字典化、System Prompt 的微小空白符变动是否会导致 Tokenizer 切词边界突变，从而破坏 KV 缓存对齐？是否有推荐的 Token 级前缀固化规范？
3. **KV Cache 分支回滚（Save/Restore State）**：`llama.cpp` 原生 C++ API 支持 `llama_state_save_file` / `llama_state_seq_rm`。在目前的 HTTP API 层面上，社区是否有计划或已有扩展端点能实现“精确回滚到指定 Sequence 位置的 KV 缓存”，从而与我们的 DAG Checkout 完美吻合？

---

### 议题二：MTP 投机采样（`draft-mtp` 1~4）与自定义 Jinja 模板下的工具调用（Tool Call）抗破损治理

#### 1. 架构哲学诉求
依照奥卡姆剃刀，**能用原生模型内生能力解决的，决不外挂额外控制组件**。我们期望尽量依赖 Qwen3.8 的原生能力与自定义 Jinja 模板实现精准工具调用，而非引入庞大臃肿的运行时拦截器。

#### 2. 现状与痛点
服务器生产实例通过启动参数明确启用了多令牌预测投机采样：
```bash
--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-n-min 1
```
同时配合加载了自定义 `chat_template.jinja`，其关键结构包含：
```jinja
{{- '<|im_start|>assistant\n<think>\n' + reasoning_content + '\n</think>\n\n' + content }}
...
{{- '<tool_call>\n{"name": ' }}{{- tc_name | tojson }}{{- ', "arguments": ' }}{{- _args }}{{- '}\n</tool_call>' }}
```
智能体运行高度依赖严格结构化的函数调用（Function Calling）：
模型在输出长篇自然语言思维链（`<think>`）时，MTP 草稿头预测加速收益极佳；
但在输出紧凑格式的 JSON 工具调用（转义双引号 `\"`、路径斜杠 `\\`、括号嵌套闭合）时，**草稿头单次预测 1~4 个 Token 的跳字机制，极易在关键语法符号处发生跳跃式错位或提前截断**，导致参数无法通过 `core.validator` 的严格 Schema 校验。

#### 3. 拟请教专家的核心问题
1. **MTP 与 GBNF 语法约束的碰撞机制**：如果在向 `llama-server` 发起工具调用时注入 GBNF 语法（Grammar-based Sampling）以 100% 锁死 JSON Schema，MTP 的草稿头预测是否会被 GBNF 频繁 Reject 导致性能不升反降，甚至在特定终止符边界触发死循环？
2. **Jinja 原生工具模板下 MTP 的稳定性**：针对 Qwen3.8-27B 搭配自定义 Jinja 模板的生产场景，专家建议对工具调用区域做怎样的特殊标记隔离？是否有办法在生成 `<tool_call>` 区间时动态暂时降级/禁用 MTP？
3. **MTP 最小安全参数调优**：针对思维链 CoT 与 Agent 工具调用混合型负载，`--spec-draft-n-max 4` 是否偏大？是否有实测验证过的最佳折衷档位（例如限制为 2）？

---

### 议题三：Windows 本地执行体与 3-Gate 熔断的进程树安全（Process Tree Sandboxing）

#### 1. 架构哲学诉求
坚决排斥在 Windows 开发机上强行捆绑庞大的 Docker Desktop、Linux 虚拟机或复杂的特权驱动。我们追求**利用 Windows NT 内核最原生、最低心智负担的系统原生机制**实现百分之百干净的执行体生命周期管理。

#### 2. 现状与痛点
智能体运行在 Windows 宿主上，通过 `skills/` 执行任意本地能力：
- `skills/shell.py`：调用 `powershell.exe` / `cmd.exe` 执行命令行任务；
- `skills/chrome_dom.py`：通过 Chrome DevTools Protocol (CDP) 驱动无头/有头浏览器。

**痛点所在**：
- **孤儿进程泄漏（Orphan Processes）**：
  在 Windows 下，当 Agent 触发 Gate 1（用户按下打断）或 Gate 3（超时取消）时，Python 调用 `subprocess.Popen.terminate()` 或 `kill()` **仅能杀死宿主 Shell 进程自身，而由该 Shell 派生出的子进程（例如编译命令 `cmake.exe`、测试程序、后台子任务）将沦为孤儿进程脱壳存活**，继续锁定工作区文件、占用网络端口，破坏状态一致性。
- **Session 0 穿透屏障**：
  当守护进程以 Windows 服务形式或通过无头自动化拉起时，涉及 GUI、剪贴板、桌面截图（`screenshot.py`）时会受到 Windows 严格的 Session 0 隔离阻断。

#### 3. 拟请教专家的核心问题
1. **Win32 Job Objects（作业对象）的落地标准**：为了在 Python 层彻底实现“打断时树状连根拔起”，是否应当将每个 Task 或每个 Skill 的执行强制绑定到配置了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Win32 Job Object？在 Windows 11 / Server 平台上是否有已知句柄泄漏或权限陷阱？
2. **纯原生无依赖沙箱方案**：在坚持“不引入重型 Docker、WSL 或第三方虚拟化依赖”的前提下，专家推荐如何对 Agent 的原生 Shell 执行进行最小必要权限隔离与超时看门狗设计？

---

### 议题四：长程任务中分层 Compactor 摘要的“事实衰减”与上下文保真度

#### 1. 架构哲学诉求
我们拒绝引入需要复杂调优、维护成本极高的向量数据库（Vector DB）和多跳图知识库黑箱。压缩后的历史必须依然是一段**人类可读、程序员一眼看懂、且能完美写入 `checkpoint.jsonl` 的纯正 Markdown/JSON 文本**。

#### 2. 现状与痛点
我们的系统设计了 `ContextManager.compact()`：
当线性 Token 估算突破设定阈值（例如 32k 或更大比例）时，挂起当前主循环，触发 Compactor 客户端执行 `_summarize()`，将前半段历史浓缩为中文事实备忘录，并重构 DAG：
```
[Node 0: System Prompt] 
       ↓
[Node K: Compaction Node (包含此前 1~K-1 步的结构化中文事实摘要)]
       ↓
[Node K+1: 最新用户纠偏与当前轮次]
```
在 128k 超大物理窗口下，当一个复杂任务跨越数十步、发生 3 次以上的级联压缩（Summary of Summary）时，原始提示词中的**绝对文件路径、关键变量定义、操作员明确否决过的历史决策（Negative Constraints）**极易发生语义退化或被模型自行“泛化抹平”，导致后续 Agent 遗忘历史红线而重蹈覆辙。

#### 3. 拟请教专家的核心问题
1. **结构化提取（Fact Schema）vs 自由文本摘要**：专家建议 Compactor 输出自由 Markdown 摘要，还是强制采用分段式结构化抽取模板（例如必须显式保留：`[已确认的事实]`, `[修改过的文件]`, `[被否决的方案]`, `[待完成的子目标]`）？
2. **混合检索补偿（Dense + Disk DAG）**：在拥有 128k 超大窗口的前提下，是应该激进推迟 Compaction 触发阈值（例如推迟到 64k/96k），还是采用“摘要 + 按需索引原始 DAG 节点（`read_checkpoint_node`）”的混合回溯模式？

---

## 四、附录：真实服务定义与模板代码摘录

### 附录 A：服务器实际 `llama-server.service` 服务单元定义
```ini
[Unit]
Description=Llama.cpp Server
After=network.target nvidia-persistenced.service

[Service]
User=llama
Group=llama
SupplementaryGroups=video render
WorkingDirectory=/home/model-runner/JonathanColetti.Qwen3.8-27B-Uncensored-GGUF

ExecStart=/usr/local/bin/llama-server \
  --model Qwen3.8-27B-Uncensored-IQ4_XS.gguf \
  --mmproj mmproj-Qwen3.8-27B-Uncensored-F16.gguf \
  --spec-type draft-mtp \
  --spec-draft-n-max 4 \
  --spec-draft-n-min 1 \
  -ngl 99 \
  -c 128000 \
  -np 2 \
  -ctk q8_0 \
  -ctv q8_0 \
  --kv-unified \
  --jinja \
  --chat-template-file chat_template.jinja \
  --metrics \
  --load-mode none \
  --host 0.0.0.0 \
  --port 8080

ExecStartPre=+/usr/sbin/sysctl vm.swappiness=10
LimitMEMLOCK=infinity
Restart=on-failure
RestartSec=5
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
```

### 附录 B：自定义 `chat_template.jinja` 核心工具渲染与报错追踪逻辑摘录
```jinja
{# 工具响应与连续错误追踪计数逻辑 #}
{%- if not _is_code_or_grep and (_strong_error or (_weak_error and not _weak_suppressed)) %}
    {%- set ns2.consecutive_failures = ns2.consecutive_failures + 1 %}
{%- else %}
    {%- set ns2.consecutive_failures = 0 %}
{%- endif %}

{{- '\n<tool_response>\n' + content }}
{%- if ns2.consecutive_failures >= 2 %}
    {{- '\n\n⚠️ SYSTEM WARNING: ' ~ ns2.consecutive_failures ~ ' consecutive tool errors detected. Your previous approach is incorrect. You MUST use a fundamentally different approach or corrected arguments.' }}
{%- elif ns2.consecutive_failures == 1 %}
    {{- '\n\n⚠️ SYSTEM WARNING: The previous tool call returned an error. Diagnose the failure and retry with completely corrected arguments.' }}
{%- endif %}
{{- '\n</tool_response>' }}
```

---
*本文档由 Qwen38-llama-cpp-harness-windows 项目架构负责人编制。期待各位专家的真知灼见！*
