# RFC: Qwen3.8 + llama.cpp 本地长程 Agent 运行时架构深度咨询备忘录 (Phase 8 & Phase 9)
**Architectural Consultation Brief: Phase 8 & Phase 9 — High-Precision Computer Use, Self-Evolving Governance & Autonomous Cognitive Exploration**

- **文档状态**：征求专家意见稿（RFC / Request for Comments — Phase 8 & Phase 9）
- **发起团队**：Qwen38-llama-cpp-harness 项目架构组（项目负责人 / 首席架构师）
- **核心哲学**：KISS 原则、奥卡姆剃刀、万物皆文件（Unix-like 磁盘权威事实）、零构建（Zero-Build）前端、极简依赖（严格锁定 4 项 Python 基础库）
- **面向专家**：Windows 操作系统底层与 UI 自动化专家、LLM 推理系统与状态恢复专家、长程自主 Agent 运行时架构师
- **项目仓库**：`Qwen38-llama-cpp-harness-windows`（基线 Commit `dd8dd3c`）

---

## 一、项目基准环境与前序交付成果

在经历 Phase 1 至 Phase 7 的演进后，仓库已构建起生产级的单机长程运行时：
1. **操作系统执行层**：纯 `ctypes` 实现 Win32 Job Object（4GB 内存配额、64 进程限制、`KILL_ON_JOB_CLOSE`），`_drain_stream` 采用 `read(4096)` 与 `deque` 双端环形队列实现常数级 $\le 512\text{ KB}$ 内存封顶，注入全矩阵非交互环境变量与 `stdin=subprocess.DEVNULL`，Git 操作收编进内核沙箱。
2. **长程上下文与状态机**：会话绑定前强制清理，JSON 语法报错结构化回执引导 Jinja 模板告警自我纠偏；多轮级联压缩下旧索引自动剥离，`<immutable_redlines>` 确保操作员红线 **100% 确定性保真**；二级纪元（Epoch）紧凑索引表控制索引 $\le 30$ 行；同槽位前缀继承使 50k 上下文摘要 Prefill 从 40 秒骤降至 $<0.5$ 秒。
3. **零构建 Web 控制台**：纯原生 ES Modules 实现事实账本看板（🎯🏛️📝⛔🚀🗂️），白盒 JSON 抽屉，Gate 2 后分发前的 `step_risky` 高危写操作断点，CSS `content-visibility: auto` 与 rAF 节流实现 200+ 节点 60 fps 丝滑渲染。
4. **质量与依赖红线**：全套 179 项单元测试 100% 绿灯，66 步长程压测指标 $\text{GCR}=100\%$、$\text{FFR}=100\%$、$\text{SCCS}=1.000\text{ turn}^{-1}$，外部依赖严格锁定为 4 项。

### 负责人硬性不可动摇之基准：
- `--spec-draft-n-max` 严格锁定为 `4`（适应 Tesla M40 28nm 显存带宽受限现实，最大化摊薄访存开销）；
- `--cache-reuse` 严格保持 `0`（绝对严禁开启，规避多模态 `mmproj` 视觉嵌入与 Token 前缀缓存复用的断言崩溃）；
- 远程模型服务端当前 Slot 1 仍在执行长时任务，服务端配置重载手册（`OPS_LLAMA_SERVER_UPGRADE.md`）保持冷备，待任务自然完成后由用户择机执行。

---

## 二、Phase 8 核心议题：桌面操作高精度对齐与技能自进化治理

系统在 Phase 8 的定位是：**从“基础运行时防护”走向“高精度现实环境交互与安全技能自举”**。

---

### 议题一（Phase 8A）：Windows 原生 UI 自动化、High-DPI 缩放与双模对准 (Native UI & High-DPI Grounding)

#### 1. 现状痛点与底层陷阱
智能体定位为本地计算机操作智能体（Computer Use Agent），依赖 `skills/screenshot.py`（`mss` 帧捕获）与 `skills/mouse.py` / `skills/keyboard.py` 操控桌面。但在实际 Windows 10/11 生产环境中遭遇以下物理挑战：
1. **High-DPI 缩放导致像素与输入坐标漂移**：
   笔记本与高分显示器普遍配置了 125%、150% 或 200% 的 DPI 缩放。`mss` 截取的是物理显存帧（如 $3840 \times 2160$），但 Win32 `SetCursorPos` / `mouse_event` 或部分被控应用在非 DPI 感知模式下使用逻辑坐标（如 $2560 \times 1440$）。多模态模型识别出的图像绝对坐标传给鼠标，点击往往落在目标几十像素之外。
2. **细小控件的视觉 Grounding 极限**：
   在单张全屏截图中，关闭按钮、小复选框等只有十几个像素大小。受限于 Qwen3.8-27B 多模态 Patch 采样率，纯视觉猜坐标在复杂密集软件（如 Visual Studio、专业 CAD、办公套件）中容易疲劳漂移。
3. **Session 0 锁屏与黑屏探测**：
   当用户临时锁屏（`Win+L`）或远程会话断开时，截屏直接返回黑屏或抛异常，缺乏优雅的会话状态感知。

#### 2. 拟设计架构方案（恪守零新增依赖）
- **坐标 DPI 感知与归一化数学对齐**：
  在 `core/tools.py` 或 `skills/` 中，通过纯 `ctypes` 调用 `user32.SetProcessDpiAwarenessContext`（或 `Shcore.GetDpiForMonitor`），在 Python 进程启动时锁定 DPI 感知模式，并建立“物理截图像素 $\leftrightarrow$ 虚拟屏幕坐标”的确定性换算函数。
- **纯 `ctypes` 原生 Windows UI Automation (UIA) 控件树轻量提取技能（`skills/ui_tree.py`）**：
  坚决拒绝引入重型 `pywinauto`、`uiautomation` 或 Java/Node 桥接。直接通过 `ctypes` 封装 Windows 原生 COM 接口 `IUIAutomation`，仅提取当前前台激活窗口（`GetForegroundWindow`）的顶层可聚焦控件树（Class, Name, Role, BoundingRectangle）。
  输出极简的纯文本控件表（例如：`[ID: 12] Button "保存 (Save)" Rect: (450, 320, 520, 350)`）。模型既可以看图，又可以直接指定控件 ID 或精确 Rect 点击，形成“视觉 + 控件语义”双模对准。

#### 3. 请教专家的问题
1. 在 Windows 10/11 复杂的混合多显示器环境下（例如主屏 4K 150% 缩放，副屏 1080P 100% 缩放），纯通过 Win32 API（`GetWindowDpiAwarenessContext` 与 `GetDpiForWindow`）建立统一坐标映射的最优工程实践是什么？如何确保 `mouse.py` 的绝对坐标点击不受多显示器虚拟屏幕坐标原点（可能为负数）的影响？
2. 在坚持使用 Python 标准库 `ctypes`（零外部包依赖）的前提下，封装 Windows `IUIAutomation` COM 接口提取前台窗口聚焦树，有哪些已知的死锁陷阱（例如某些假死程序未响应 UIA 请求导致主线程卡死）？如何设计带有毫秒级超时的非阻塞 UIA 探测看门狗？

---

### 议题二（Phase 8B）：技能自举安全沙箱与准入契约治理 (Self-Evolving Skill Governance)

#### 1. 现状痛点与系统脆弱性
系统内置了 `add_skill.py` 与 `modify_skill.py`，允许模型根据解决问题的需要自行编写 Python 工具扩展自身能力：
- 现行实现直接将模型生成的代码写入 `skills/<name>.py` 并调用 `reload_skills()`；
- 若模型生成的代码存在致命错误（如引入了系统不存在的外部依赖包、存在死循环、破坏了 SPEC §4 规定的 `DESCRIPTION` / `PARAMETERS` / `HANDLER` 导出契约），热重载会直接污染全局工具注册表，甚至在分发器 `step_loop` 调用时引发不可逆崩溃。

#### 2. 拟设计架构方案
构建“技能自举准入预审沙箱（Skill Pre-Flight Sandbox）”：
1. **隔离暂存与子进程预运行（Dry-Run）**：
   模型新增或修改技能时，首先写入暂存目录 `.agent/skills_staging/<name>.py`；
2. **三道自动化准入检查**：
   - **AST 语法与导入依赖检查**：使用 Python 内置 `ast` 模块解析语法树，静态核验禁止引入 `requirements.txt`（4 项）之外的未声明外部包；
   - **SPEC §4 契约强制核验**：由轻量隔离脚本加载暂存文件，反射核验 `DESCRIPTION`（字符串）、`PARAMETERS`（合法 JSON Schema 字典）与 `HANDLER` / `run` 可调用契约；
   - **空跑沙箱冒烟测试**：在子进程中尝试以模拟空入参调用一次 `validate_args` 或试运行，确认无即时崩溃；
3. **原子转移与坏死自动隔离（Quarantine）**：
   三道门禁 100% 通过后，才原子移动至 `skills/` 并触发注册表热重载；若某个已生效技能在后续连续 3 轮交互中抛出未捕获异常，系统自动将其移至 `.agent/skills_disabled/` 隔离降级，确保主 ReAct 循环坚如磐石。

#### 3. 请教专家的问题
1. 在 Windows 环境下，通过子进程动态加载审查尚未受信任的 Python 脚本时，如何防止该脚本在 `import` 阶段（模块顶级作用域）执行恶意或耗时的副作用（如连接网络、弹出窗口）？
2. 针对模型自主生成的 `PARAMETERS` JSON Schema，除了 `jsonschema` 库验证外，用纯 Python 标准库验证其符合 OpenAI Function Calling 规范的最佳轻量级方案是什么？

---

## 三、Phase 9 核心议题：认知自治、因果树回退与混合状态快照复用

系统在 Phase 9 的定位是：**从“被动执行指令”走向“主动目标反思、因果回溯与推理引擎快照对齐”**。

---

### 议题三（Phase 9A）：目标驱动的自主因果回退与树状探索 (Autonomous DAG Rewind)

#### 1. 现状痛点
在 50~100 步的复杂长程自主任务中（如大型代码库重构或故障排查），模型偶发会采纳一条错误的技术路线，导致连续数步报错。
目前系统的 DAG 分支树（`checkpoint.jsonl`、`branches/`）机制极其完备，但**创建分支与回退操作仅暴露在 Web 控制台供人类操作员手动点击**；模型自身在陷入死胡同时，只能在被污染的历史消息后继续追加“修补性”对话，极易耗尽推理预算或发生上下文漂移。

#### 2. 拟设计架构方案
1. **自主回退原生技能（`skills/branch_rewind.py`）**：
   为智能体提供原生回溯工具：
   ```python
   def run(args: dict, runtime: dict) -> str:
       # args: {"target_node_id": "n_000015", "new_branch_name": "fix_alt_path", "reason": "..."}
   ```
   当模型自我诊断判定现有路径不可行时，可主动发起回溯指令。引擎自动在该历史节点上新建分支并切换当前活跃会话，丢弃分支点之后的错误对话，将记忆状态干净地还原至决策分叉点；
2. **测试驱动的目标自愈闭环（TDD Verification Gate）**：
   在模型触发 `task_done` 之前，强制要求其运行工作区关联的单元测试套件。若测试红灯，禁止直接结束任务，强制进入反思回溯循环。

#### 3. 请教专家的问题
1. 从 Agent 认知架构（Cognitive Architectures）角度，赋予模型“自主回退分支”能力后，如何设计死循环防护？如何防止模型在同一个分支点来回振荡创建数十个无意义的平行分支？
2. 在自主回溯后，如何将“前一个失败分支的核心教训”以最小的 Token 成本传递给新分支的事实账本第 4 节（避免在新分支中重复踩坑）？

---

### 议题四（Phase 9B）：服务端 DeltaNet 状态快照 (-ctxcp 64) 毫秒级恢复联动

#### 1. 背景与硬件契约
当前远程 Linux 模型服务器（Tesla M40 24GB，Qwen3.8-27B-IQ4_XS + mmproj）因 Slot 1 正在执行长时任务而处于冷备状态。待该任务自然结束后，用户将执行已在仓库就绪的 [`OPS_LLAMA_SERVER_UPGRADE.md`](OPS_LLAMA_SERVER_UPGRADE.md)。
升级后的 `llama-server` 将包含以下关键启动参数：
```bash
--no-cache-idle-slots \
--cache-ram 16384 \
--ctx-checkpoints 64 \
--checkpoint-min-step 1024 \
--spec-draft-n-max 4 \
--kv-unified
```
这将开启宿主机 16 GiB 内存池，为每个槽位缓存多达 64 个混合注意力快照（含 Qwen3.8 的 48 层 DeltaNet 循环状态），最小快照间距仅 1024 Token。

#### 2. 拟设计架构方案
在客户端发生 DAG 分支切换或调用 `branch_rewind` 时，我们期望彻底终结长达数十秒的冷启动 Prefill：
- 目前分支切换后，因为前缀 Token 序列发生变动，客户端发往 `llama-server` 会触发从头重新 Prefill；
- 结合 `llama-server` 最新的快照机制，客户端是否可以通过特定的 HTTP Header（如 `X-Sequence-Checkpoint` 或 OpenAI 兼容请求的 `extra_body` 字段），提示服务端直接利用已保存在 16GB 宿主 RAM 中的某个历史快照恢复 DeltaNet 状态？

#### 3. 请教专家的问题
1. 在 `llama.cpp`（0.6.0-dev 及后续版本）的最新 C++ 实现中，当服务端配置了 `--ctx-checkpoints` 与 `--checkpoint-min-step` 时，HTTP `/v1/chat/completions` 是否已有现成或推荐的传参协议来显式引导槽位加载指定检查点？
2. 在 Qwen3.8 的混合架构中，DeltaNet 状态在从 RAM 快照恢复后，其隐藏层循环状态（Recurrent State）与标准 Attention 的 KV Cache 是否能保证比特级完全一致？是否有已知的浮点漂移或 Token 边界对准隐患？

---

## 四、专家评审交付指引

请专家针对 Phase 8 与 Phase 9 的设计构想提供权威的系统级评审意见，重点聚焦于：
1. **纯标准库实现的可行性边界**：对于 Win32 UIA 控件树提取与技能预审沙箱，给出最小充分机制的纯 Python 代码实现指导，坚决守住“零新增第三方依赖”红线；
2. **防死锁与抗振荡工程推导**：对自主因果回溯（`branch_rewind`）的反向振荡抑制给出严密的机制设计；
3. **优先级与演进矩阵评估**：对 Phase 8A $\to$ Phase 8B $\to$ Phase 9A $\to$ Phase 9B 的落地依赖关系与风险点给出前瞻性评估。
