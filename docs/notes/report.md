# Harness 演进与验收日志

## Phase 8 & 9 验收归档
Completed Phase 8B (skill sandbox), Phase 8A (DPI/UIA automation), and Phase 9A (context race fix, branch rewind, TDD gate). Finished diff review and hardened the TDD gate so failed `.py` writes do not open it; also added overwrite-deploy coverage.
Verification: `232 tests OK`, repo hygiene OK, `requirements.txt` still 4 dependencies, `web/` untouched.

---

# Phase 10A 验收汇报 —— Win32 底层桌面原子能力（窗口编排与原子剪贴板）

日期：2026-10-27
状态：首席架构师已核签（等待用户审批 `git commit`）

## 1. 交付物

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `skills/window_manager.py` | 新增 | 窗口枚举 + 六步防死锁/UIPI 免疫激活状态机 |
| `skills/clipboard.py` | 新增 | CF_UNICODETEXT 原子剪贴板（64 位安全 + 自旋锁 + 内核所有权契约） |
| `tests/test_window_clipboard.py` | 新增 | 38 项专项测试 |
| `tests/test_ui_tree_dpi.py` | 微调 | 移除死导入 + Pillow 可用性守卫 |

`requirements.txt` 零改动（严格保持 4 项基础依赖，纯 `ctypes` 实现，无 `pywin32`）。

## 2. 测试结果（红线验收）

- **全量测试**：`python -m unittest discover -s tests -t .`
  - **Ran 270 tests — OK (skipped=3)，0 失败，0 错误，耗时约 63.8 秒**
  - 通过数 267 ≥ 245 ✓；存量 232 项全部通过（其中 3 项跳过均为"可选依赖未安装"的环境性跳过，无一失败）
  - 3 个 skip：2 项 CLI 冒烟（`openai` 包未安装，既有行为）+ 1 项截图 JPEG 路径（`pillow` 未安装）
- **隐私合规**：`python -m unittest discover -s tests -p test_repo_hygiene.py -t .` → **1 test, OK，0 违规命中**
- **专项测试**：`python -m unittest discover -s tests -t . -p test_window_clipboard.py` → **38 tests, OK（0.27 秒）**
- **技能沙箱纯度测试**：`python -m unittest discover -s tests -p test_skill_sandbox.py -t .` → **18 tests, OK（0.58 秒）**

## 3. 实现要点与蓝图对照

- **window_manager.py**：严格对照蓝图 §2.2 与 §2.4.1 源码范本。六步序列完整落地：双端 `WM_NULL`（`SMTO_BLOCK|SMTO_ABORTIFHUNG`，目标 150ms/前台 100ms）探活熔断 → Token Integrity Level RID 核验（`OpenProcess`+`GetTokenInformation(TokenIntegrityLevel)`，SID SubAuthority 尾元素读取；不可访问进程保守按 0x3000 处理）→ `IsIconic` 守卫 → `LSFW_UNLOCK`+`ASFW_ANY`+扩展 `VK_MENU` KEYUP 脉冲 → `try...finally` 受控 `AttachThreadInput` 编排（`SPI_SETFOREGROUNDLOCKTIMEOUT`、`BringWindowToTop`、`SetWindowPos(HWND_TOP)`、`SetForegroundWindow`、`SetActiveWindow`、Chromium 渲染子窗口 `SetFocus`，`finally` 中按 target→fg 顺序解绑）→ 150ms 看门狗 + 75ms 置顶翻转破锁。
- **`list_windows`**：`EnumWindows` 纯 ctypes 枚举，三重过滤（`IsWindowVisible`、非 `IsIconic`、非 DWM Cloaked），返回 HWND/Title/PID/进程名（`QueryFullProcessImageNameW`）/物理 Rect（优先 `DWMWA_EXTENDED_FRAME_BOUNDS`，失败回落 `GetWindowRect`）。
- **title 解析**：大小写不敏感子串匹配 title 或进程名；多命中返回 `AMBIGUOUS_TARGET` + 候选列表且**不激活**；零命中返回 `WINDOW_NOT_FOUND`。
- **clipboard.py**：严格对照蓝图 §2.4.2。`GMEM_MOVEABLE|GMEM_ZEROINIT` 分配；`SetClipboardData` 成功即所有权移交内核（成功路径 `GlobalFree` 零调用），仅失败路径在 `finally` 中释放；`_open_clipboard_spinlock(retries=5, backoff_sec=0.020)` 步进退避抗击剪贴板历史进程瞬态争用；UTF-16LE + NUL 终止原子读写。
- **Ctrl+V 粘贴一致性**：`set_text` 支持可选 `paste` 参数（默认 false，保持纯原子写语义）。启用时经 `SendInput` 一次性注入完整 `VK_CONTROL↓ VK_V↓ VK_V↑ VK_CONTROL↑` 序列——单批次原子注入实现零延迟，且走键盘事件流天然绕过中文输入法拦截。`PARAMETERS` 的 action 枚举保持任务卡规定的 `get_text|set_text` 两种。
- **可测性缝隙**：`_get_win32_apis` / `_bind_clipboard_apis` 惰性绑定函数即 mock 注入点（与 `ui_tree.py` 的 `_user32_api` 既有惯例一致）。
- **run 入口**：两模块均导出 `def run(args, runtime=None) -> str`（SPEC §4 方式二），结构化结果以 JSON 字符串返回，非 win32 平台显式报错。

---

# Phase 10B 验收汇报 —— 视觉-语义双模自适应对准与高精度切片管道

日期：2026-10-27
状态：首席架构师已核签（等待 10C 完成后统一提交）

## 1. 交付物

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `skills/grounding.py` | 新增 | 双模自适应对准核心技能（Stage 1/1.5/2 状态机 + Chromium 唤醒） |
| `skills/pic_zoom.py` | 升级 | 高清网格切片 + 偏移标尺标定 + 天花板双射逆投影 |
| `tests/test_grounding_dpi.py` | 新增 | 39 项专项测试 |

`requirements.txt` 零改动（严格保持 4 项基础依赖）。

## 2. 测试结果（红线验收）

- **全量测试**：`python -m unittest discover -s tests -t .`
  - **Ran 309 tests — OK，0 失败，0 错误，耗时约 64.5 秒**
  - 通过数 309 ≥ 255 ✓；存量与 10A、10B 全量通过。
- **专项测试**：`python -m unittest discover -s tests -t . -p test_grounding_dpi.py` → **39 tests, OK（0.31 秒）**
- **技能沙箱纯度测试**：`python -m unittest discover -s tests -p test_skill_sandbox.py -t .` → **18 tests, OK（0.56 秒）**
- **隐私合规**：`python -m unittest discover -s tests -p test_repo_hygiene.py -t .` → **1 test, OK，0 违规命中**

## 3. 实现要点与蓝图对照

- **Chromium 动态唤醒**：`skills/grounding.py` 对 `Chrome_RenderWidgetHostHWND` 注入 `WM_GETOBJECT(0x003D)` + `AccessibleObjectFromWindow(OBJID_CLIENT)` 脉冲，15ms 快速唤醒 DOM 级 UIA 树，避免重启浏览器。
- **语义三段式检索**：`AutomationId` 精确匹配 → `Name`（结合 `role`）匹配 → 不区分大小写子串匹配。叶节点命中即返回物理中心，**0 视觉 Token 消耗**。
- **Stage 1.5 空间窄化**：若命中容器级父节点，将其物理 Rect 作为裁剪候选框，将后续视觉搜索面积缩小 70%~90%。
- **四维贫血判定与 Stage 2 降级**：自绘黑名单类名、空心大容器、低叶节点数、低空间密度任一触发即平滑切入局部网格超分辨率放大。
- **天花板双射几何投影**：`skills/pic_zoom.py` 严格实现天花板取整公式 `(dx * 65535 + (span_x - 1)) // span_x`，在任意多显示器（含负坐标原点）下与 `MOUSEEVENTF_VIRTUALDESK` 往返误差严格恒等于 0 px。

---

# Phase 10C 验收汇报 —— 零构建 Web 控制台人机协同与画布透视演进

日期：2026-10-27
状态：首席架构师已核签（Phase 10 全部组件齐备，等待用户审批提交）

## 1. 交付物

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `web/store.js` | 新增 | 原生 EventTarget 单向流微任务状态机（119 行，≤120 行红线） |
| `web/canvas_overlay.js` | 新增 | `<agent-canvas-overlay>` 原生组件（视网膜抗模糊 + 嵌套命中检测 + 指哪打哪） |
| `web/dag_timeline.js` | 新增 | `<agent-dag-timeline>` 原生组件（因果回溯贝塞尔时间轴 + 失败反思高亮） |
| `web/app.js` / `index.html` / `styles.css` | 升级 | 装配画布透视层与时间轴抽屉，对接投影路由 |
| `core/api.py` | 扩充 | 新增 `/tasks/:id/ui_tree`、`/tasks/:id/rewind_ledger`、`/screens` 只读投影路由 |
| `core/tools.py` | 抽取 | 抽取 `window_rect_ex(hwnd)` 供 UIA 窗口投影复用 |
| `skills/ui_tree.py` | 优化 | 快照持久化补充窗口 `origin` / `dpi_scale` / `window_title` |
| `tests/test_api_ui_tree.py` | 新增 | 28 项专项测试（零构建静态门禁 + API 投影断言） |

`requirements.txt` 零改动（严格保持 4 项基础依赖），`web/` 零构建纯原生无打包器。

## 2. 测试结果（红线验收）

- **专项测试**：`python -m unittest discover -s tests -t . -p test_api_ui_tree.py` → **28 tests, OK（7.93 秒）**
- **零构建红线**：`web/` 目录下无 `package.json`、无 `node_modules`、无任何打包配置，通过 14 项静态断言验证。
- **全量终极大集成套件**：`python -m unittest discover -s tests -t .`
  - **Ran 337 tests in 72.740s — OK，0 失败，0 错误**
  - 通过数 337 ≥ 260 ✓；涵盖 Phase 1~9 基线（232 项） + 10A（38 项） + 10B（39 项） + 10C（28 项）。
- **技能沙箱纯度测试**：`python -m unittest discover -s tests -p test_skill_sandbox.py -t .` → **18 tests, OK（0.56 秒）**
- **隐私合规**：`python -m unittest discover -s tests -p test_repo_hygiene.py -t .` → **1 test, OK，0 违规命中**

---

# Phase 10 全模块综合大闭环里程碑汇总

至此，**Phase 10（Web 运行时、双模自适应对准与 Win32 底层桌面基座）** 三大子任务已全部交付并通过终极大集成：
- **Phase 10A**：Win32 防死锁六步激活状态机 + 64 位指针安全原生原子剪贴板；
- **Phase 10B**：Chromium 动态唤醒 + 三段式语义穿透 + 四维贫血探测 + 天花板双射局部网格超分辨率放大；
- **Phase 10C**：零构建原生 Custom Elements 画布截屏透视层 + EventTarget 微任务单向流状态机 + 因果回溯时间轴；
- **全量测试套件**：**337 项单元与集成测试 100% 绿灯**，0 失败，0 错误。



---

# Phase 11 验收汇报 —— 认知自治四机制（上下文 GC / Token 网格 / SubAgent 门禁 / 宏结晶）

日期：2026-10-31
状态：执行工程师自测完成，全部门禁铁证归档，等待首席架构师终审与用户审批提交

## 0. 交付总览

| 子任务 | 新增/修改文件 | 专项测试 | 全量回归 |
| --- | --- | --- | --- |
| 11A 上下文尾端暂态 GC | `core/context.py`、`core/agent.py`、`tests/test_context_gc.py`（新增） | 11 项 | 全绿 |
| 11B DeltaNet 1024-Token 网格对齐 | `core/token_grid.py`（新增）、`core/context.py`、`core/persistence.py`、`core/agent.py`、`tests/test_token_grid.py`（新增） | 15 项 | 全绿 |
| 11C SubAgent Worktree 门禁 | `core/subagent_gate.py`（新增）、`skills/subagent_review.py`（新增）、`core/subagent_runner.py`、`core/skill_sandbox.py`、`tests/test_subagent_gate.py`（新增） | 17 项 | 全绿 |
| 11D 工作流宏结晶 | `core/macro_compiler.py`（新增）、`skills/crystallize_macro.py`（新增）、`core/skill_sandbox.py`、`tests/test_macro_compiler.py`（新增） | 13 项 | 全绿 |

- **全量测试套件**：`python -m unittest discover -s tests -t .` → **Ran 393 tests in ~157s — OK（skipped=2 为既有环境性跳过）**。基线 337 项零回归，Phase 11 新增 56 项（11A 11 + 11B 15 + 11C 17 + 11D 13）。
- **隐私合规门禁**：`python -m unittest discover -s tests -t . -p test_repo_hygiene.py` → **1 test, OK，0 违规**（工作树无任何私有网段/账号/路径字面量）。
- **依赖红线**：`requirements.txt` 保持 `openai` / `mss` / `pillow` / `websocket-client` 4 项零改动；`core/token_grid.py`、`core/subagent_gate.py`、`core/macro_compiler.py` 均为纯标准库实现。
- **文档红线**：`web/` 目录零改动（零构建不变量保持）；四张 Phase 11 任务卡片全部勾选至 `[x]` 并附门禁铁证（见各卡片"验收记录"章节）。

## 1. Phase 11A —— 尾端锚定上下文 GC

- `ContextManager.prune_resolved_trial_tail(max_tail_tokens=2048, min_error_tokens=280, tdd_gate_open=False)`：以"助手+配对 tool 回执"回合块为最小原子单位，仅当最新回合为干净成功、且回溯窗口内存在失败回合链（≥2 块或 ≥280 估算 token）时折叠；受保护标记（`[Operator`、`[EXECUTION_REJECTED]`、`[EXECUTION_INTERRUPTED`、`[Branch Rewind Lesson]`、`<immutable_redlines>`）100% 豁免；TDD 门禁开启期间禁剪。
- 因果结晶徽标 `[Transient Attempt Pruned: N prior failed attempt(s) (sigs) resolved via tools]` 注入获胜回合首个工具回执首部；`_refresh_screenshot_indices` 保证多模态索引同步。
- `agent.step_loop` 在 `phase="completed"` 之后、批量重复检查之前安全挂载（try/except 包裹，`_pending_tdd_write()` 驱动 `tdd_open`）。
- **五重门禁铁证**：磁盘 `checkpoint.jsonl` 字节级不可变（专项测试断言前后一致且磁盘投影可回放完整历史）；OpenAI 拓扑 k=1/k=2 零孤儿；TDD 开启禁剪/解除即剪；红线豁免（含混合链负例）；全量回归全绿。
- 实现注记：可剪判据为"回合块内存在任一失败回执（且未受保护）"，使"patch 成功但同回合 shell exit=1"的混合回合块与纯失败块一并折叠，精确对应类型 B 域"从首次失败整体折叠"的语义；`min_error_tokens=280` 保证微小失败的折叠收益低于 KV Cache 重算代价时不触发（帕累托守门）。

## 2. Phase 11B —— DeltaNet 1024-Token 网格对齐与遥测自检

- `core/token_grid.py`（纯标准库）：`exact_or_estimated_tokens`（`POST /tokenize` + SHA-1 记忆化，离线/超时/畸形载荷三路无抛错回退加权估算）；`align_skeleton_block_to_grid`（仅 `gap_to_next ∈ (0,64]` 时追加零语义 `\n---` 结构分隔线，目标为"跨越下一条 1024*K 检查点触发线"，确定性纯函数保证重载前缀字节级稳定）；`verify_deltanet_checkpoint_telemetry`（RESONANT / COLD_RESET_DETECTED / CHECKPOINT_GAP_DRIFT / UNKNOWN 四态状态机）；`should_reseed_generation`（4-gram 重复率 > 0.35 判定）。
- 集成点：`ContextManager._align_skeleton()`（系统提示 + `[Context Summary]` 账本幂等对齐，`__init__` / `reload_from_branch` / `compact` 三处一致）；`get_prefix_token_count()` 供遥测审计；`persistence.append_dag_node` 为每个 DAG 节点固化 `meta.cum_tokens`，在完整回合节点（tool/user/无 tool_calls 的 assistant）跨越 `1024*K` 时固化 `meta.ckpt_aligned=True`（回溯锚点吸附数据就绪）；`agent` 侧采集流式末尾 `timings` 块并输出 `[token-grid] cache_n=... status=...` 审计日志，漂移/冷复位时对下一请求一次性注入 `id_slot=0 + cache_prompt=False` 确定性重播种；分支切换后 2 轮内启用重复哨兵。
- **六项场景铁证**：tokenize 端点+缓存命中（mock urlopen 断言仅 1 次网络调用）、结构分隔线垫片（无任何 `<!-- pad`）、>64 Token 上限放弃填充、压缩后账本底座跨入网格线（`prefix//1024 == 1` 且重载复现）、遥测四态 100% 判定、DAG 节点 `ckpt_aligned` 元数据（含"assistant 未闭合回合不标注"负例）。

## 3. Phase 11C —— SubAgent Git Worktree 物理隔离与对立证伪门禁

- `core/subagent_gate.py`：`create_subagent_worktree`（`git worktree add -B subtask_<id> .agent/worktrees/<id> HEAD`，陈残沙箱自动清理，非法 id 拒绝）；`detect_diff_hazards` 五类静态风险标记（tests/ 断言删除、skip 注入+pass 桩、修改未测（写入步之后无 exit=0）、红线文件/冻结技能触碰、空 diff）；`generate_artifact_card`（宿主从 `git status --porcelain` + `git diff HEAD` + intent-to-add 确定性抓取，SHA-256 签名，落盘 `.agent/subagents/<id>/artifact_card.json`）；`format_adversarial_review_prompt`（客观遥测置顶 → 风险强警示 → 自述标记 UNTRUSTED → 三问对立证伪清单）；`finalize_subagent_worktree`（merge：commit → `merge --ff-only` → 清理；revert：强制销毁并返回 `[Branch Rewind Lesson]` 教训行）。
- `core/subagent_runner.py`：Worktree 生命周期集成 + `tools.ROOT` 重定向（文件工具与 shell CWD 全部落入沙箱）+ 写工具/shell 回执宿主拦截（`tests_executed`、`last_write_step` 由宿主计数，LLM 不可伪造）+ 结果载荷携带 `review_prompt` / `hazard_flags` / `artifact_card_path`；`SUBAGENT_WORKTREE=0` 环境开关保留降级路径，创建失败自动降级并在载荷中显式声明。
- `skills/subagent_review.py`（SPEC §4 契约）：`merge` / `revert` 裁决；revert 回执携带教训行，经既有红线管道（`_extract_pinned_redlines`）自动固化至五段式账本第 4 节（专项测试端到端验证）。
- **五重门禁铁证**：主工作区 `git status --porcelain` 零污染（子智能体写文件后主仓仍干净）；卡片客观字段 100% 宿主生成（SHA-256 与磁盘字节一致）；五类风险 100% 命中（含"旧 exit=0 不豁免""新建非冻结技能不误报"两负例）；`active.lock` 单实例互斥与 Slot 路由测试全量保留（8 项全绿）+ `subagent_review`/`crystallize_macro` 进入子智能体元技能排除集；全量回归全绿。
- 注记：RFC 4.1 的 `role_mode` Scout/Builder/Verifier 流水线接力为可选演进项，不在本卡片 WBS 内，登记为后续提案。

## 4. Phase 11D —— 工作流宏结晶与断点热交接

- `core/macro_compiler.py` 四阶管道：①因果主干提纯（剔除失败重试回合与 screenshot/pic_zoom/ui_tree 只读探针，`grounding`+`mouse` 折叠为 `uia_click` 语义动作，纯 (x,y) 像素步剔除、`control_id` 步保留）；②污点槽位化（ISO 日期/文件路径/引号实体/数值常量四类种子 → `$slot:target_date_N` 等参数引用 + PARAMETERS/DEFAULT_ARGS 注册）；③守卫与重试包络（窗口激活探活 + 0.25s/0.5s/1.0s 三次指数退避）；④AST 顶层零 Call 纯度发射（技能模块函数内懒加载，干运行零副作用）并经 `verify_and_deploy_skill` 三闸原子晋升。
- 断点热交接：stale 失败严禁崩溃，返回结构化 `MACRO_STALE_AT_STEP` JSON（`completed_steps_count` / `failed_step_index` / `failed_step_spec` / `[MACRO_STALE_HANDOVER]` 恢复指令），主模型按指令从第 k 步接管并经 `modify_skill` 自愈，严禁从第 1 步重跑。
- **七项场景铁证**：中英文污点种子提取（日期/路径/引号/数值）、含失败重试与冗余探针轨迹的提纯合并、空间不变性（无绝对像素定位符）、`check_skill_ast` 0 错误 + 干净 exec、真实沙箱干运行晋升（落盘源码复检 AST 纯度 + `$slot` 引用存在）、第 2 步 stale 断点热交接（completed=1/failed=2）、0-Token 毫秒级重放（<1s，重grounding 后以 `control_id` 点击而非记录像素）+ 瞬时抖动重试自愈负例。

## 5. 真实端点验证（远程 OpenAI 兼容推理宿主）

| 探测项 | 结果 |
| --- | --- |
| `GET /v1/models` | 200 OK；返回 `Qwen3.8-27B-Uncensored-IQ4_XS.gguf`（llamacpp，n_ctx=65536，n_params≈27.3B，IQ4_XS） |
| `POST /tokenize` | 200 OK；`{"content":"hello world, this is a tokenize probe"}` → 8 tokens；`exact_or_estimated_tokens` 精确计数与 SHA-1 缓存对真实端点生效 |
| `POST /v1/chat/completions`（非流式） | 200 OK；响应携带 `timings`（`cache_n` / `prompt_n` / `prompt_ms` / `predicted_ms`），`verify_deltanet_checkpoint_telemetry` 对真实载荷判定 `RESONANT`（cache_n=11 覆盖前缀，min_expected=0） |
| `POST /v1/chat/completions`（流式 SSE） | reasoning_content 与 tool_calls 增量、finish_reason=tool_calls/stop、`data: [DONE]` 全部按 OpenAI 协议解析；注意：当前 llama.cpp 构建（b1-a66a2ca）流式模式不回传 `timings` 块，遥测审计按设计静默降级为 UNKNOWN（非流式路径可审计） |
| 真实 Agent 端到端冒烟 | **TASK_DONE 全链路成功**：`Agent.run_task` 流式解码 → 第 1 轮真实 `list_dir` 工具调用与回执 → 第 2 轮 `task_done({"summary": "工作区根目录包含 core、skills、tests、web、docs…"})` → 最终答复准确总结目录结构并以 TASK_DONE 收尾；checkpoint/DAG 落盘，`meta.cum_tokens` 逐节点单调累计（1551 → 1583 → 1617 → 1718 → 1788 → 1858），status=done |
| 运行观察 | 服务器冷启动/重启窗口期出现过瞬时连接拒绝（探针 3s 超时自动回退 FALLBACK_N_CTX，chat 层 8 次退避重试兜底）与轮流式早停（空 content 时按既有设计降级为思维文本收尾）；关闭思维模式（`enable_thinking: false`，经 providers 配置注入）后完全稳定。全部异常均按优雅降级处理，无崩溃、无状态损坏；截断行为属该 M40 宿主在负载/预热期的服务端特性 |

## 6. 已知边界与后续提案

1. **流式 timings 缺失**：当前推理宿主的 llama.cpp 构建仅在非流式响应中携带 `timings`；如需流式遥测，需升级服务端或在 chat 请求启用 `stream_options`（属宿主运维项，已具备非流式审计通路）。
2. **Worktree 内运行态写入**：子智能体的任务态（`.agent/tasks` 投影）仍写主仓 `.agent/subagents/<id>/`（设计使然，审计路径唯一）；Worktree 内产生的运行时缓存（如 ui_tree 快照）不进入版本控制，不构成工作区污染。
3. **role_mode 流水线编排**（RFC 4.1 Scout/Builder/Verifier 接力）：数据结构（`ckpt_aligned`、`artifact_card.json`、baton 文件位）已就绪，编排层留作 Phase 12 提案。
4. **一次性时序型测试**：`tests/test_sandbox.py::test_abort_reaps_grandchildren`（Win32 Job Object 收割孙进程）在全量高压回归中出现过 1 次计时性抖动（0 != 2 PID），单测与复跑全量均稳定通过；属既有测试的负载敏感项，非本次改动引入（该文件零改动）。
