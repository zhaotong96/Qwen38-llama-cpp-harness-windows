# RFC: Qwen3.8 + llama.cpp 本地长程 Agent 架构深度咨询备忘录 (Phase 10 & Web Console)
**Architectural Consultation Brief: Phase 10 & Next-Gen Web Console — Dual-Modal Grounding, Native Window Orchestration, High-Speed Clipboard & Zero-Build Canvas Overlay**

- **文档状态**：征求专家意见稿（RFC / Request for Comments — Phase 10 & Web Console）
- **发起团队**：Qwen38-llama-cpp-harness 项目架构组（项目负责人 / 首席架构师 / 主程序员）
- **核心哲学**：KISS 原则、奥卡姆剃刀、万物皆文件（Unix-like 磁盘权威事实）、零构建（Zero-Build）前端、极简依赖（严格锁定 4 项 Python 基础库：`openai`, `mss`, `pillow`, `websocket-client`）
- **面向专家**：Windows 底层系统与 Win32 GUI 专家、大模型视觉对准（Multimodal Visual Grounding）研究员、现代纯原生前端架构专家、长程自主 Agent 状态机架构师
- **项目仓库**：`Qwen38-llama-cpp-harness-windows`（基线 Commit [`6f77a19`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/6f77a19)）

---

## 一、项目技术基准与前序交付成果

经过 Phase 1 至 Phase 9A 的系统性演进与验收合入，本运行时已构建起兼具**高韧性防御内核**与**认知自治反思**的单机环境：

1. **操作系统执行层（Phase 6 & 8A）**：
   - 纯 `ctypes` 实现 Win32 Job Object（4GB 内存硬限制、64 进程限额、`KILL_ON_JOB_CLOSE`），子进程与 Git 操作全量内核沙箱化；
   - 启动期 Per-Monitor V2 物理 DPI 锁定（`SetProcessDpiAwarenessContext(-4)`），`foreground_window()` 采用 DWM 物理矩形（`DwmGetWindowAttribute`）抹平 7~8px 透明阴影误差，虚拟桌面坐标 `[-32768, 32767]` 完美适配多屏副屏与负坐标原点；
   - 纯 `ctypes` COM 原生 Windows UI Automation（UIA）提取技能（`skills/ui_tree.py`），内置 150ms `WM_NULL` 消息泵探测、800ms 独立 MTA 线程看门狗与有界 BFS 遍历，挂起窗口秒级安全拒决，彻底杜绝主线程死锁。
2. **长程上下文与状态机自治（Phase 7 & 9A）**：
   - `<immutable_redlines>` 确保操作员红线在多轮级联压缩中 100% 确定性保真；二级纪元（Epoch）紧凑索引表控制索引 $\le 30$ 行；
   - 修复了 `core/context.py` 分支覆盖竞态 Bug，交付目标驱动的自主因果回溯技能（`skills/branch_rewind.py`），通过四维抗振荡门禁（单节点 $\le 2$ 次、全局 $\le 4$ 次、冷却 $\ge 3$ 步、Jaccard 去重 $<0.70$）阻断死循环，回溯教训 `[Branch Rewind Lesson]` 自动继承至五段式账本第 4 节；
   - 接入 TDD 自愈门禁，源码改动未通过测试验证（`exit=0`）严禁结束任务。
3. **技能自进化治理沙箱（Phase 8B）**：
   - 创新性确立 AST 顶层零 Call 纯度定理，静态根除模块导入期副作用；纯标准库实现递归 OpenAI JSON Schema 校验器；
   - `sys.addaudithook` 子进程干运行拦截写文件与危险系统调用，原子晋升至 `skills/`；动态技能连续 3 次运行时异常自动移入 `.agent/skills_disabled/` 隔离降级。
4. **质量矩阵与架构红线**：
   - 全套 **232 项单元测试 100% 绿灯**；
   - 66 步复杂场景长程压测指标：$\text{GCR}=100\%$、$\text{FFR}=100\%$、$\text{SCCS}=1.000\text{ turn}^{-1}$；
   - 全仓库隐私扫描 0 泄露；外部依赖严格锁定为 4 项；前端 `web/` 保持纯原生零构建。

---

## 二、议题一（Phase 10）：现实桌面自治闭环与双模自适应对准 (Real-World Desktop Autonomy)

### 1. 现状痛点与底层物理陷阱

尽管 Phase 8A 解决了 DPI 物理对齐与纯 COM UIA 提取，但在 Windows 10/11 真实复杂生产环境（混合运行 Win32、WPF、Chromium/Electron、Qt 以及自绘 DirectUI 应用）下，仍面临以下瓶颈：

1. **GUI 渲染体系异构导致单一模态对准失效**：
   - *传统 Win32/WPF 应用*（如资源管理器、记事本、系统控制面板）：UIA 节点树极其丰富，使用 `ui_tree` + `control_id` 毫秒级语义定位准确率接近 100%；
   - *现代自绘/Web 混合应用*（如 VS Code、Chrome、微信、CAD、Blender、某些自研企业软件）：其窗口在 UIA 树中可能退化为单一的根 `Pane` 或纯自绘画布，无内部子控件暴露。此时强行依赖 UIA 会报错返回空控件集；
   - *痛点*：目前模型在面对异构应用时，需频繁在“尝试 `ui_tree` $\to$ 报错 $\to$ 改用 `screenshot` 视觉盲猜 $\to$ 漂移重试”之间反复横跳，耗尽推理预算与 Step 配额。
2. **细小图标的视觉感知极限与疲劳漂移**：
   - Qwen3.8-27B 多模态视觉模型受限于单图 Patch 采样率，对于密集排布的 16×16 px 图标（如状态栏托盘、IDE 折叠箭头、微型关闭叉），直接在全屏缩放图中预测绝对像素坐标时，存在 10~25 px 的置信度离散漂移。
3. **多窗口焦点竞争与意外遮挡（Focus Stealing & Occlusion）**：
   - Windows 桌面在无人值守执行时，经常出现弹窗抢占前台焦点、目标窗口被其他工作窗口部分或全部遮挡。Win32 API 的 `SetForegroundWindow` 受制于系统的前台锁定超时限制（`SPI_SETFOREGROUNDLOCKTIMEOUT`），调用可能被系统内核静默忽略，导致随后的键盘鼠标事件发送至错误窗口。
4. **长文本键盘输入的效率瓶颈与输入法（IME）污染**：
   - 当前 `skills/keyboard.py` 通过 `keybd_event` / `SendInput` 逐字模拟击键。输入一段 500 字符的代码或长文本需耗费数秒，极易受操作系统当前中英文输入法状态干扰，产生拼音候选框拦截、乱码或按键丢失。

---

### 2. 拟设计架构方案（恪守零外部新增依赖）

#### 方案 2.1：视觉+UIA 语义双模自适应对准管道（`skills/grounding.py`）
构建高内聚的“靶心定位器”：
- **第一级（UIA 语义穿透）**：输入自然语言描述或属性选择器（如 `{"target": "保存", "role": "Button"}`）。先调用 UIA 在当前激活窗口中搜索匹配节点。若找到且属于可交互控件，直接返回其物理 Rect 中心点，耗时 $<50\text{ ms}$；
- **第二级（局部网格切片高精度视觉修正）**：若 UIA 节点不存在（自绘或粗粒度 Pane），系统自动截取前台窗口，调用 `skills/pic_zoom.py` 对候选视觉区域执行局部 2x~4x 裁剪放大与数字网格投影，引导多模态模型在超高分辨率切片上输出亚像素级中心坐标，再由几何映射公式投影回全局物理虚拟屏幕。

#### 方案 2.2：高可靠 Win32 窗口编排技能（`skills/window_manager.py`）
纯 `ctypes` 封装，提供工业级窗口管理能力：
- 支持 `list_windows`（枚举所有可见、非最小化的主窗口 HWND、标题、PID 及对应进程名）；
- 支持 `activate_window(title_or_hwnd)`：突破 Windows 前台锁定限制，采用成熟的 Win32 组合拳机制（附加线程输入队列 `AttachThreadInput` + `AllowSetForegroundWindow` + `SetWindowPos(HWND_TOP)`），并在调用后通过 150ms 看门狗核验 `GetForegroundWindow() == target_hwnd`，确保目标窗口绝对置顶且激活。

#### 方案 2.3：原子化原生剪贴板通道（`skills/clipboard.py`）
纯 `ctypes` 封装 Windows 原生剪贴板 API：
- 支持 `set_text(text: str)` 与 `get_text() -> str`，直接操作 `CF_UNICODETEXT` (13) 全局内存对象（`GlobalAlloc`、`GlobalLock`）；
- 彻底解决大段文本输入的 IME 污染与延迟问题：智能体可一键将文本/代码写入剪贴板，紧接着触发 `keyboard(action="hotkey", keys=["ctrl", "v"])`，完成瞬时无损粘贴；
- 增加防占用自旋锁机制（处理其他软件临时锁死剪贴板的竞争场景，最多重试 5 次，带 20ms 退避）。

---

### 3. 请教领域专家的问题 (Domain Questions - Phase 10)

1. **自适应对准回退判定策略**：
   在判断当前前台窗口是否属于“UIA 贫血/无意义自绘窗口（如 Electron 禁用 Accessibility、Qt 纯自绘）”时，除了简单的子节点数量统计之外，工业级 RPA 或自动化测试框架中有无更为确定、毫秒级的探测启发式规则？
2. **Win32 窗口强制激活的最优防降级实现**：
   在无人值守环境、甚至无当前用户物理输入脉冲的场景下，跨进程强制将指定 HWND 置于前台并获取真正键盘输入焦点（Keyboard Focus），纯 `ctypes` 调用的最稳健 API 序列是什么？如何防止 `AttachThreadInput` 在某些特权进程（如提升管理员权限的程序）引发的权限死锁或挂起？
3. **高 DPI 局部网格切片的几何投影不变性**：
   在主屏副屏不同缩放率（如主屏 150%、副屏 100%）并发生窗口跨屏停靠时，`pic_zoom` 局部放大切片的坐标系换算如何严格与 `mouse.py` 的虚拟桌面归一化公式 `MOUSEEVENTF_VIRTUALDESK` 保持数学上的双射无误差？

---

## 三、议题二：Web 控制台人机协同演进、UIA 透视画布与因果时间轴 (Zero-Build Web Console Evolution)

### 1. 现状痛点与交互瓶颈

系统目前拥有一套纯原生 ES Modules、零打包构建（Zero-Build）的轻量 Web 控制台（`web/index.html` + `web/app.js`）：
- 提供了五段式事实账本看板、白盒 JSON 抽屉、SSE 实时日志流与 `step_risky` 高危写操作断点；
- 但在面临 Phase 8A/8B/9A 引入的高级特性时，操作员在 Web 端面临以下认知壁垒：
  1. **模型决策视觉盲区**：智能体调用 `screenshot` 与 `ui_tree` 后，人类操作员在 Web 界面上只能看到一张静态的 JPG 截图和抽屉里一长串复杂的 JSON 文本，无法一眼看出“模型眼中的控件 [ID: 15] 到底对应界面上的哪个按钮”；
  2. **断点调试介入成本高**：当智能体在 `step_risky` 暂停等待人类决策时，操作员若想纠正其点击位置，必须手动去查找坐标并手写 JSON 发送，缺乏在画面上“指哪打哪”的一键式干预能力；
  3. **因果回溯不可视**：Phase 9A 的 `branch_rewind` 产生的分支探索树、回溯原因（`failed_lesson`）与废弃的脏文件列表，目前仅记录在磁盘的 `rewind_ledger.jsonl` 中，Web 端缺乏分支回溯时间轴的图形化直观映射。

---

### 2. 拟设计架构方案（严格捍卫“零构建 Zero-Build”红线）

#### 方案 3.1：截屏 Canvas 控件透视叠加层（UIA Grounding Overlay）
- **多层渲染视口**：在 Web 端展示的当前屏幕截图上方叠加一个绝对定位、透明的 HTML5 `<canvas id="uia-overlay">`；
- **动态坐标自适应映射**：前端获取 `/tasks/:id/ui_tree` 返回的控件几何信息，实时计算“图片自然分辨率 $\leftrightarrow$ 浏览器响应式布局尺寸 $\leftrightarrow$ 视网膜 DevicePixelRatio”的缩放比率；
- **交互与透视能力**：
  - 自动在 Canvas 上绘制半透明绿色矩形线框，并在角落标注 `[ID: N] Role "Name"` 浮动徽标；
  - 鼠标悬停（Hover）在任意控件框上，线框高亮并弹出详细属性气泡（Class, AutomationId, 坐标）；
  - 点击任意控件框，Web 控制台自动将 `mouse(control_id=N)` 或精确物理坐标填入下方的操作员干预输入框，实现一键协同。

#### 方案 3.2：因果回溯分支时间轴与红线追踪抽屉（DAG Rewind Timeline Drawer）
- **时间轴组件**：新增轻量级抽屉，读取 `/tasks/:id/rewind_ledger.jsonl` 与 `/tasks/:id/tree`；
- **直观因果视图**：
  - 清晰呈现分叉事件：`分支 A (节点 25) --[因路线失败回退]--> 分支 B (节点 12)`；
  - 醒目高亮被否决的方案（`failed_lesson`）与该次尝试中修改过的脏工作区文件；
  - 联动展示当前活跃分支的五段式账本第 4 节，直观验证红线与失败教训是否保真留存。

#### 方案 3.3：纯原生 Web Components 模块化重构
- **拒绝引入任何打包链**：绝对不引入 Webpack, Vite, npm, node_modules 或复杂的 TS 编译链，保持单个 HTML 启动即可运行的极致优雅；
- **纯标准 Web 栈**：采用原生浏览器已全面支持的标准 Web 技术：
  - 纯原生自定义元素（Web Components / Custom Elements: `<agent-canvas-overlay>`, `<agent-rewind-timeline>`）；
  - 原生 CSS 变量与 CSS Grid/Flexbox；
  - 原生 ES Modules 动态导入与原生 `EventTarget` 状态机订阅分发。

---

### 3. 请教领域专家的问题 (Domain Questions - Web Console)

1. **Canvas 叠加层高频重绘与性能防御**：
   在 SSE 持续推送实时截图与窗口频繁缩放的场景下，纯原生 JS 如何以最高性能实现 Canvas 像素比（DPI）对齐、防模糊（Sub-pixel Sharpness）以及在海量 UIA 控件（例如 50~100 个嵌套矩形）下的零延迟命中检测（Hit Testing）？
2. **零构建前端的状态管理优雅范式**：
   在不依赖 Redux、Zustand、Vue/React 等庞大前端框架的前提下，纯原生 ES Modules 如何设计一套极简（<200 行）、高内聚、易测试的单向数据流状态机，以协调 SSE 实时事件、UI Tree 快照数据与操作员交互指令？
3. **DAG 树状分支在零依赖下的轻量可视化算法**：
   在不引入 D3.js 或 Cytoscape.js 等巨型图形库的前提下，用纯原生 SVG 或 Canvas 渲染包含 5~10 个分支、20~50 个节点的多叉树（DAG），业界最紧凑、数学上最清晰的轻量分层布局算法（Layered Graph Layout）推荐是什么？

---

## 四、系统不可动摇之工程红线与约束

任何架构设计与工程建议，必须严格收敛在以下既定红线之内：

1. **Python 依赖绝不膨胀**：`requirements.txt` 严格保持 **4 项基础依赖**（`openai`, `mss`, `pillow`, `websocket-client`），严禁引入任何 Windows 专有包（如 `pywin32`, `uiautomation`, `pywinauto` 等），全部系统调用必须通过标准库 `ctypes` 实现；
2. **前端纯原生零构建（Zero-Build Invariant）**：`web/` 目录不得引入任何 `package.json`、构建脚本或非浏览器原生支持的文件格式，必须保证开箱即用；
3. **推理模型端硬件约束不可动摇**：
   - 模型服务端严格维持 `--spec-draft-n-max 4`（Tesla M40 带宽受限决断）；
   - 严格禁用 `--cache-reuse`（保持 `0`，杜绝 mmproj 冲突崩溃）；
   - 服务端冷备待命：在 Slot 1 长时任务未释放前，客户端绝不发起破坏性重载。
4. **单 SubAgent 串行执行约束**：`AGENTS.md` 规定同一时刻系统至多保留一个活跃 SubAgent。

---

## 五、专家评审交付期望

请专家重点针对上述 Phase 10 与 Web 控制台的核心议题，提供切中要害、可直接指导工程落地的评审意见：
1. **纯 `ctypes` 实现的最小充分代码模式**：对于 Win32 窗口强制激活、剪贴板安全操作，提供兼具异常安全性与防死锁能力的标准调用范式；
2. **多模态视觉+语义双模 Grounding 的降级状态机逻辑**：在何种条件触发局部切片放大、如何有效合并多尺度坐标；
3. **零构建 Web Canvas 叠加层与 DAG 时间轴的工业级落地指导**。
