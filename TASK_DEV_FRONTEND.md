# 任务委派书：Phase 5B 零构建原生 Web 双控控制台交互与视觉联调

- **委派发起人**：项目架构负责人 / 主程序员
- **执行角色**：开发工程师（Web前端）
- **前置依赖**：Commit `3f9156b`，后端服务已启动并托管静态路由 `GET /web/*`
- **核心原则**：[SPEC.md](SPEC.md) §13.2（零构建透明 Web 控制台：纯原生 HTML5/CSS/ES Modules，绝不引入 Node.js/npm 打包器）
- **状态**：已完成（待验收）(Completed, Pending Acceptance)

---

## 一、任务背景与目标

本项目配备了一套极简、无依赖、随后端即开即用的原生双控 Web 控制台（位于 [web/](web/)）。本任务的目标是**在真实浏览器环境中对控制台的布局自适应、Git 式 DAG 树可视化、SSE 实时流打字机动效以及双端队列面板进行全交互链路验收与细节打磨**。

---

## 二、工作范围与具体执行步骤

### 1. 访问控制台与视觉布局验收
启动后端后，在 Chrome / Edge 等现代浏览器中打开：
```
http://127.0.0.1:8765/web/index.html
```
**检查要点**：
- **顶部导航条（Topbar）**：服务存活指示灯（绿色）、活跃会话 Badge、活跃分支 Badge、打断模式下拉框与紧急打断按钮呈现正常；
- **三栏式工作区响应式布局**：
  - 左栏：会话选择与 Git 式上下文树因果投影；
  - 中栏：实时流式解码总线（SSE）、当前分支历史对话流、指令输入区；
  - 右栏：统一消息排队面板、SubAgent 监控面板、API 路由状态。
- **高 DPI / 窗口缩放适配**：窗口拉伸、收缩时各栏是否有滚动条异常溢出。

### 2. 左栏：Git 式上下文树（DAG）交互验证
- 检查 [web/tree_view.js](web/tree_view.js) 的渲染表现：
  - 节点是否清晰区分系统提示（system）、用户指令（user）、思考链（reasoning）、工具调用（tool_calls）、工具回执（tool）以及压缩摘要（compaction）；
  - 历史 Commit 点击高亮与展开/收起；
  - 测试分支切换功能（Checkout）与从当前 Commit 派生新分支（Fork 分支输入框）。

### 3. 中栏：实时流式解码（SSE）与操作员干预验证
- 检查 [web/app.js](web/app.js) 的 SSE 监听通道（`/events`）：
  - 当模型推理时，中栏上方的“实时流式总线”是否能以打字机形式平滑输出增量字符与 Token 计数；
  - 输入框下方的“优先级（10/50/90）”与“初始状态（queued/held）”下拉项是否正确传递给后端；
  - 点击“强制打断 (Interrupt)”按钮，确认是否立即向后端发送打断请求，观察顶部状态变化。

### 4. 右栏：双端同步排队面板交互验证
- 检查 [web/queue_panel.js](web/queue_panel.js)：
  - 点击“加入统一队列 (POST /queue)”，确认排队项是否立即出现在右栏列表中；
  - 测试单条排队项在界面上的“挂起 (hold)”、“放行 (resume)”、“修改内容”与“取消撤回 (delete)”按钮；
  - 确认操作后列表数据与磁盘状态即时同步。

---

## 三、工程红线约束（不可违背）

1. **零构建红线**：严禁在 `web/` 引入任何 `package.json`、npm 依赖、Webpack、Vite 或第三方构建工具。必须保持纯原生标准 ES Modules；
2. **镜子定位红线**：WebUI 纯粹作为底层磁盘文件状态的投影，绝不拥有独立于文件系统的持久化状态。所有状态修改必须走后端 REST API 转化为显式文件写入。

---

## 四、交付物与验收清单

- [x] Web 控制台在浏览器中正常加载，无 Console 报错（404 或 JS Syntax Error）
- [x] 顶部导航条指示灯正常显示绿色在线状态
- [x] 左栏能够根据 `checkpoint.jsonl` 正确渲染树状有向无环图
- [x] 中栏输入框能够正常发送执行与排队请求
- [x] 右栏排队面板能够实时呈现待处理消息并支持挂起/撤回
- [x] 界面完全遵从原生极简美学，交互流畅

---

## 五、验收回执（前端）

**验证环境**：Chrome 155（headless=new + CDP 9222）真实渲染；守护进程 `127.0.0.1:8765`（PID 6864）；模型端点为本机 mock OpenAI 服务（PID 32464，SSE 节奏可调），富 DAG 夹具 `20261018_090000_000001`（system/user/reasoning/tool_calls/tool/rejected/compaction 全类型 + 多分支）。

**验收结果（T1–T7 全通过，全部 Chrome DevTools Protocol 实测 + 截图存证）**
- **T1 首次加载**：console errors 0、HTTP failures 0；三栏 grid 340/920/380，页面无横滚。
- **T2 DAG 树**：12 节点按 role/type 七类着色（含图例）；点击高亮 selected 且在 SSE 重渲染后保持；展开/收起（正文+reasoning+tool_calls+meta 详情，默认 3 行截断）；checkout 切分支后中栏消息链同步；rejected 节点红标。
- **T3 Fork 与队列**：UI Fork 新分支（正确锚定 tool 配对节点）并 checkout；入队→卡片即时出现（并发刷新竞态已消除）；hold/resume/edit(prompt)/cancel 四操作与 `.agent/queue/` 磁盘文件逐一对照一致，操作均有顶部/输入区瞬时反馈（notice）。
- **T4 SSE 打字机**：提交任务实时流 0→150 字符逐帧增长（growth_events=9）；seq 去重（重连不重复）；字符/token 计数（chars ≈ tokens=chars/4）实时刷新；清空流按钮复位。
- **T5 打断**：Gate 1 `abort_stream` 流冻结于 216 字符；phase=completed (REJECTED)；checkpoint 尾节点 rejected_by_operator=true；abort.flag 归档至 `interrupt_processed`；busy 释放。busy 时提交自动入队（badge 2）。
- **T6 杂项**：SubAgent 派生→completed（面板状态同磁盘）；providers 编辑器保存往返幂等（未再发生配置漂移）、本地编辑在后台刷新中不丢失、非法 JSON 以 notice 报错不弹窗；Resume 按钮正常续跑并释放 busy。
- **T7 响应式**：1680/1440/1280/1100/980 五档 + DSR1.5 截图；无页面横滚、无控件溢出；1100 档顶栏自动换行、标题超长省略号；980 档单列堆叠 + 页面纵向滚动。

**前端修复清单（本次提交）**：Console 404 噪声根除（无 checkpoint 会话不再请求树并显式提示）；DAG 七类角色配色/图例/选中高亮/展开收起/思考链与工具调用呈现；消息卡片补 reasoning 与 tool_calls 区块；实时流只渲染增量文本 + seq 去重 + 断线秒级重连 + 字符额度上限裁剪 + 清空按钮；refreshAll 票据防并发覆盖；操作反馈 notice（打断/续跑/入队/队列操作/错误统一）；providers 编辑器变更保护；暗色滚动条 + color-scheme；媒体查询（≤1280 300/1fr/340；≤1024 单列堆叠）；composer/options 换行防溢出。

**跨域发现（已移交后端，附复现脚本）**
1. `GET /tasks` 500 竞态（已最小修复并回归）：`core/persistence.py` 读-判竞态，`list_branches`/`list_tags`/`get_branch_head`/`get_current_session`/`load_dag_nodes`/`load_status`/`list_tasks` 在 `atomic_write_text` 的 os.replace 窗口内会抛 `FileNotFoundError`。已改为容错读取（`_read_text_tolerant`），`Ran 107 tests OK`；复现脚本 `C:\Users\zt\AppData\Local\Temp\opencode\webui-e2e\repro_tasks_race.py`（修复前 3 errors → 修复后 0）。
2. `POST /tasks` 上下文泄漏（未改，待后端裁定）：守护进程长驻 Agent 会把上一会话消息重序列化进新任务 checkpoint，新任务不再“一次性模式”清白启动；`task2_nodes=18` 且含 task1 提示词。复现：`C:\Users\zt\AppData\Local\Temp\opencode\webui-e2e\repro_task_context_leak.py`（SPEC.md:158 POST /tasks=CLI 一次性模式）。
3. 观察项：providers 编辑器所写 `local_dev` 档（main/subagent/compactor → `local-model@127.0.0.1:8080/v1`）已通过 `PUT /config/providers` 落盘为 `.agent/providers.json`，取代此前误存的默认模板（该误存曾触发 8080 掉线重试，已恢复并幂等验证）。

**红线守约**：`web/` 仅 5 个原生文件（index.html/styles.css/app.js/tree_view.js/queue_panel.js），无 `package.json`/npm/打包器；未新增任何持久化状态，全部变更经 REST/SSE 与 `.agent/` 文件一一对照。测试工具链在仓库外 `%TEMP%\opencode\webui-e2e\`，不进版本库。
